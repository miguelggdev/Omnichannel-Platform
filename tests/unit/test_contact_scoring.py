"""Tests del scoring predictivo de contactos (Sprint 12, Dev B).

El calculo es una funcion pura sobre `MetricasDelContacto`, asi que casi todo
esto corre sin PostgreSQL. Solo `cargar_metricas()` y `recalcular_score()`
necesitan una sesion, y se les pasa un doble.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import contact_scoring as cs
from app.services.contact_scoring import MetricasDelContacto, calcular_score

AHORA = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _metricas(**over) -> MetricasDelContacto:
    base = {
        "ultima_actividad": None,
        "entrantes": 0,
        "salientes": 0,
        "conversiones": 0,
    }
    base.update(over)
    return MetricasDelContacto(**base)


class TestPesos:
    """El reparto de pesos tiene que seguir sumando 1."""

    def test_los_pesos_suman_uno(self) -> None:
        assert round(sum(cs.PESOS.values()), 6) == 1.0

    def test_no_queda_componente_de_sentimiento(self) -> None:
        """El sentimiento se saco a proposito: no hay de donde leerlo."""
        assert "sentiment" not in cs.PESOS

    def test_recencia_y_frecuencia_pesan_igual(self) -> None:
        """El spec les da el mismo peso; el reparto no debe romper eso."""
        assert cs.PESOS["recency"] == cs.PESOS["frequency"]
        assert cs.PESOS["engagement"] == cs.PESOS["conversion"]


class TestRecencia:
    @pytest.mark.parametrize(
        ("dias", "esperado"),
        [(0, 100.0), (1, 100.0), (3, 80.0), (7, 80.0), (20, 50.0), (60, 20.0), (200, 0.0)],
    )
    def test_tramos(self, dias: int, esperado: float) -> None:
        assert cs._score_recencia(AHORA - timedelta(days=dias), AHORA) == esperado

    def test_sin_actividad_da_cero(self) -> None:
        assert cs._score_recencia(None, AHORA) == 0.0

    def test_actividad_en_el_futuro_no_da_negativo(self) -> None:
        """Un reloj torcido o un import con fechas malas no debe romper el tramo."""
        assert cs._score_recencia(AHORA + timedelta(days=5), AHORA) == 100.0


class TestFrecuencia:
    @pytest.mark.parametrize(
        ("entrantes", "esperado"),
        [(0, 0.0), (1, 30.0), (4, 30.0), (5, 60.0), (10, 80.0), (25, 100.0)],
    )
    def test_tramos(self, entrantes: int, esperado: float) -> None:
        assert cs._score_frecuencia(entrantes) == esperado


class TestEngagement:
    def test_sin_salientes_es_neutral_no_cero(self) -> None:
        """Nunca le escribimos: no se le puede castigar por no responder."""
        assert cs._score_engagement(entrantes=0, salientes=0) == 50.0

    def test_nunca_responde(self) -> None:
        assert cs._score_engagement(entrantes=0, salientes=10) == 0.0

    def test_responde_la_mitad(self) -> None:
        assert cs._score_engagement(entrantes=5, salientes=10) == 50.0

    def test_responde_mas_de_lo_que_le_mandamos_topa_en_cien(self) -> None:
        assert cs._score_engagement(entrantes=40, salientes=10) == 100.0


class TestConversion:
    @pytest.mark.parametrize(
        ("conversiones", "esperado"), [(0, 0.0), (1, 20.0), (3, 60.0), (5, 100.0), (99, 100.0)]
    )
    def test_tramos_y_tope(self, conversiones: int, esperado: float) -> None:
        assert cs._score_conversion(conversiones) == esperado


class TestScoreCompleto:
    def test_contacto_muerto_da_el_minimo_posible(self) -> None:
        """Sin actividad ninguna: solo aporta el engagement neutral."""
        score = calcular_score(_metricas(), AHORA)

        assert score == pytest.approx(50.0 * cs.PESOS["engagement"], abs=0.01)

    def test_contacto_ideal_da_cien(self) -> None:
        score = calcular_score(
            _metricas(
                ultima_actividad=AHORA,
                entrantes=30,
                salientes=30,
                conversiones=5,
            ),
            AHORA,
        )

        assert score == 100.0

    def test_el_score_nunca_se_sale_del_rango(self) -> None:
        casos = [
            _metricas(),
            _metricas(ultima_actividad=AHORA, entrantes=999, salientes=1, conversiones=999),
            _metricas(ultima_actividad=AHORA - timedelta(days=5000), entrantes=1),
        ]
        for metricas in casos:
            assert 0.0 <= calcular_score(metricas, AHORA) <= 100.0

    def test_mas_actividad_nunca_baja_el_score(self) -> None:
        """Propiedad basica: el score es monotono en la actividad del contacto."""
        flojo = calcular_score(
            _metricas(ultima_actividad=AHORA - timedelta(days=60), entrantes=2, salientes=4), AHORA
        )
        activo = calcular_score(
            _metricas(ultima_actividad=AHORA - timedelta(days=1), entrantes=20, salientes=4), AHORA
        )

        assert activo > flojo

    def test_el_formato_sirve_para_el_filtro_score_min(self) -> None:
        """`segmentation.score_min` castea `metadata->>'score'` solo si matchea
        `^-?[0-9]+(\\.[0-9]+)?$`. Un score con notacion cientifica no entraria."""
        import re

        score = calcular_score(_metricas(ultima_actividad=AHORA, entrantes=7, salientes=9), AHORA)

        assert re.match(r"^-?[0-9]+(\.[0-9]+)?$", str(score))


class _ResultadoUno:
    def __init__(self, fila) -> None:
        self._fila = fila

    def one(self):
        return self._fila


class _SesionFalsa:
    def __init__(self, fila=None) -> None:
        self._fila = fila
        self.ejecutadas: list = []

    async def execute(self, stmt=None, *args, **kwargs):
        self.ejecutadas.append(stmt)
        return _ResultadoUno(self._fila)


class TestCargarMetricas:
    @pytest.mark.asyncio
    async def test_una_sola_consulta(self) -> None:
        """Las cuatro metricas salen en un SELECT, no en cinco."""
        fila = SimpleNamespace(ultima_actividad=AHORA, entrantes=3, salientes=6, conversiones=2)
        sesion = _SesionFalsa(fila)

        metricas = await cs.cargar_metricas(sesion, uuid4(), uuid4(), AHORA)

        assert len(sesion.ejecutadas) == 1
        assert metricas == MetricasDelContacto(
            ultima_actividad=AHORA, entrantes=3, salientes=6, conversiones=2
        )

    @pytest.mark.asyncio
    async def test_nulos_de_la_base_se_vuelven_cero(self) -> None:
        fila = SimpleNamespace(
            ultima_actividad=None, entrantes=None, salientes=None, conversiones=None
        )

        metricas = await cs.cargar_metricas(_SesionFalsa(fila), uuid4(), uuid4(), AHORA)

        assert metricas == MetricasDelContacto(
            ultima_actividad=None, entrantes=0, salientes=0, conversiones=0
        )


class TestRecalcularScore:
    @pytest.mark.asyncio
    async def test_escribe_con_jsonb_set_y_no_reescribe_el_metadata_entero(self) -> None:
        """Un UPDATE que pisara `metadata` completo borraria lo que el tenant
        guarda ahi por la API del CRM."""
        fila = SimpleNamespace(ultima_actividad=AHORA, entrantes=10, salientes=10, conversiones=1)
        sesion = _SesionFalsa(fila)

        score, momento = await cs.recalcular_score(sesion, uuid4(), uuid4(), AHORA)

        assert momento == AHORA
        assert 0.0 <= score <= 100.0

        sql = str(sesion.ejecutadas[-1]).lower()
        assert "jsonb_set" in sql, "el metadata debe actualizarse por clave, no entero"
        assert "update contacts" in sql

    @pytest.mark.asyncio
    async def test_filtra_por_client_id_ademas_de_rls(self) -> None:
        fila = SimpleNamespace(ultima_actividad=None, entrantes=0, salientes=0, conversiones=0)
        sesion = _SesionFalsa(fila)

        await cs.recalcular_score(sesion, uuid4(), uuid4(), AHORA)

        sql = str(sesion.ejecutadas[-1]).lower()
        assert "client_id" in sql
