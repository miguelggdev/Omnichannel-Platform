"""Tests del re-ranking del RAG con cross-encoder (Sprint 12, Dev B).

Ninguno carga el modelo real: el `.onnx` no viaja en el repo ni se descarga en
CI, y el punto de estos tests es justamente que la ausencia del modelo **no**
rompa nada. El motor se sustituye por un doble.
"""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import reranker as rr


@dataclass
class _Chunk:
    """Lo minimo que el reranker mira de un resultado del RAG."""

    content: str
    marca: str = ""


def _chunks(*textos: str) -> list[_Chunk]:
    return [_Chunk(content=t, marca=t) for t in textos]


def _limpiar_cache() -> None:
    """Limpia el cache del motor si todavia no lo sustituyo un monkeypatch."""
    limpiar = getattr(rr._cargar_motor, "cache_clear", None)
    if limpiar is not None:
        limpiar()


@pytest.fixture(autouse=True)
def _sin_cache():
    """El motor esta cacheado por proceso; se limpia entre tests."""
    _limpiar_cache()
    yield
    _limpiar_cache()


class _MotorFalso:
    """Motor que puntua segun un mapa texto -> puntaje."""

    def __init__(self, puntajes: dict[str, float]) -> None:
        self.puntajes = puntajes
        self.llamadas: list[tuple[str, list[str]]] = []


def _usa_motor(monkeypatch: pytest.MonkeyPatch, puntajes: dict[str, float]) -> _MotorFalso:
    """Sustituye el motor y la inferencia por dobles deterministas."""
    motor = _MotorFalso(puntajes)
    monkeypatch.setattr(rr, "_cargar_motor", lambda: motor)

    def _puntuar(_motor: Any, query: str, textos: list[str]) -> list[float]:
        motor.llamadas.append((query, textos))
        return [motor.puntajes.get(t, 0.0) for t in textos]

    monkeypatch.setattr(rr, "_puntuar", _puntuar)
    return motor


class TestDegradacion:
    """Sin modelo, el RAG tiene que seguir funcionando."""

    async def test_sin_ruta_configurada_no_hay_motor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(rr, "get_settings", lambda: SimpleNamespace(RERANKER_MODEL_PATH=""))

        assert rr._cargar_motor() is None
        assert rr.disponible() is False

    async def test_modelo_ausente_no_levanta_excepcion(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Ruta configurada pero sin archivos: se degrada, no se explota."""
        monkeypatch.setattr(
            rr, "get_settings", lambda: SimpleNamespace(RERANKER_MODEL_PATH=str(tmp_path))
        )

        assert rr._cargar_motor() is None

    async def test_sin_motor_devuelve_el_orden_de_los_embeddings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rr, "_cargar_motor", lambda: None)
        candidatos = _chunks("a", "b", "c", "d")

        resultado = await rr.rerank("consulta", candidatos, top_k=2)

        assert [c.marca for c in resultado] == ["a", "b"]

    async def test_si_la_inferencia_falla_devuelve_el_orden_original(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rr, "_cargar_motor", lambda: _MotorFalso({}))

        def _explota(*_args, **_kwargs):
            raise RuntimeError("el modelo se corrompio")

        monkeypatch.setattr(rr, "_puntuar", _explota)
        candidatos = _chunks("a", "b", "c")

        resultado = await rr.rerank("consulta", candidatos, top_k=2)

        assert [c.marca for c in resultado] == ["a", "b"]


class TestReordenamiento:
    async def test_reordena_por_puntaje_y_recorta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _usa_motor(monkeypatch, {"malo": 0.1, "regular": 0.5, "buenisimo": 0.9})
        candidatos = _chunks("malo", "regular", "buenisimo")

        resultado = await rr.rerank("consulta", candidatos, top_k=2)

        assert [c.marca for c in resultado] == ["buenisimo", "regular"]

    async def test_le_pasa_la_query_y_todos_los_candidatos(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        motor = _usa_motor(monkeypatch, {})
        await rr.rerank("cuanto cuesta el plan", _chunks("a", "b", "c"), top_k=1)

        query, textos = motor.llamadas[0]
        assert query == "cuanto cuesta el plan"
        assert textos == ["a", "b", "c"]

    async def test_un_solo_candidato_no_invoca_el_modelo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        motor = _usa_motor(monkeypatch, {})

        resultado = await rr.rerank("consulta", _chunks("unico"), top_k=5)

        assert [c.marca for c in resultado] == ["unico"]
        assert motor.llamadas == []

    async def test_lista_vacia(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _usa_motor(monkeypatch, {})

        assert await rr.rerank("consulta", [], top_k=5) == []

    async def test_no_bloquea_el_event_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """La inferencia es sincrona; tiene que correr en un executor.

        Si `_puntuar()` se llamara derecho desde la corrutina, el `sleep(0)` de
        la tarea testigo no podria avanzar mientras dura.
        """
        import time

        monkeypatch.setattr(rr, "_cargar_motor", lambda: _MotorFalso({}))

        def _lento(_motor: Any, _query: str, textos: list[str]) -> list[float]:
            time.sleep(0.15)
            return [0.0] * len(textos)

        monkeypatch.setattr(rr, "_puntuar", _lento)

        testigo = []

        async def _late() -> None:
            for _ in range(5):
                await asyncio.sleep(0.01)
                testigo.append(1)

        await asyncio.gather(rr.rerank("q", _chunks("a", "b"), top_k=1), _late())

        assert len(testigo) == 5, "el event loop quedo parado durante la inferencia"


class TestConfigDelTenant:
    def test_defaults_sin_configuracion(self) -> None:
        assert rr.config_del_tenant(None) == (True, rr.DEFAULT_INITIAL_TOP_K)
        assert rr.config_del_tenant({}) == (True, rr.DEFAULT_INITIAL_TOP_K)

    def test_el_tenant_puede_apagarlo(self) -> None:
        usar, _ = rr.config_del_tenant({"rag": {"use_reranking": False}})

        assert usar is False

    def test_el_tenant_puede_cambiar_los_candidatos(self) -> None:
        _, inicial = rr.config_del_tenant({"rag": {"initial_top_k": 50}})

        assert inicial == 50

    @pytest.mark.parametrize(
        "basura", [{"rag": {"initial_top_k": "muchos"}}, {"rag": {"initial_top_k": 0}}, {"rag": []}]
    )
    def test_un_jsonb_con_basura_degrada_al_default(self, basura: dict) -> None:
        """La columna la edita el tenant; un valor raro no puede tumbar el RAG."""
        _, inicial = rr.config_del_tenant(basura)

        assert inicial == rr.DEFAULT_INITIAL_TOP_K

    def test_un_booleano_no_cuenta_como_entero(self) -> None:
        """`isinstance(True, int)` es True en Python; no debe colarse."""
        _, inicial = rr.config_del_tenant({"rag": {"initial_top_k": True}})

        assert inicial == rr.DEFAULT_INITIAL_TOP_K
