"""Lo que se puede probar del servicio del sandbox sin base de datos (ADR-078).

La logica con transacciones esta en `tests/integration/test_sandbox_isolation.py`;
aqui, las reglas puras y las validaciones que corren *antes* de tocar la base.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from app.models.base import Base
from app.services import sandbox as svc

AHORA = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


class TestConfigPublicable:
    def test_no_lleva_las_claves_del_tenant_y_conserva_las_del_destino(self) -> None:
        origen = {
            "rag_top_k": 12,
            "clinical": {"professional_contact_ids": ["sandbox"]},
            "feature_flags": {"enable_clinical": False},
        }
        destino = {
            "rag_top_k": 5,
            "clinical": {"professional_contact_ids": ["real"]},
            "marketing": {"operator_contact_ids": ["m"]},
            "feature_flags": {"enable_clinical": True},
        }

        resultado = svc._config_publicable(origen, destino)

        assert resultado == {
            "rag_top_k": 12,
            "clinical": {"professional_contact_ids": ["real"]},
            "marketing": {"operator_contact_ids": ["m"]},
            "feature_flags": {"enable_clinical": True},
        }

    def test_si_el_destino_no_tiene_una_clave_del_tenant_tampoco_la_deja_el_origen(self) -> None:
        resultado = svc._config_publicable({"clinical": {"x": 1}, "rag_top_k": 3}, {"rag_top_k": 5})

        assert resultado == {"rag_top_k": 3}

    @pytest.mark.parametrize(("origen", "destino"), [(None, None), ({}, None), (None, {})])
    def test_tolera_configs_vacios(self, origen: Any, destino: Any) -> None:
        assert svc._config_publicable(origen, destino) == {}

    def test_no_muta_lo_que_recibe(self) -> None:
        origen = {"rag_top_k": 1, "clinical": {"a": 1}}
        destino = {"clinical": {"b": 2}}

        svc._config_publicable(origen, destino)

        assert origen == {"rag_top_k": 1, "clinical": {"a": 1}}
        assert destino == {"clinical": {"b": 2}}


class TestConfigVisible:
    def test_oculta_las_claves_del_tenant(self) -> None:
        config = {"rag_top_k": 5, "clinical": {}, "marketing": {}, "feature_flags": {"a": True}}

        assert svc._config_visible(config) == {"rag_top_k": 5}

    def test_lo_que_se_devuelve_se_puede_reenviar_sin_que_lo_rechace(self) -> None:
        """El GET y el PUT son simetricos: nada de lo que muestra el primero revienta al segundo."""
        visible = svc._config_visible({"rag_top_k": 5, "feature_flags": {"a": True}})

        assert not [k for k in visible if k in svc.CLAVES_DEL_TENANT]

    def test_none_es_un_config_vacio(self) -> None:
        assert svc._config_visible(None) == {}


class TestAgentePrincipal:
    def test_es_el_primer_activo(self) -> None:
        agentes = [
            {"name": "viejo", "is_active": False},
            {"name": "uno", "is_active": True},
            {"name": "dos", "is_active": True},
        ]

        assert svc._agente_principal(agentes)["name"] == "uno"  # type: ignore[index]

    @pytest.mark.parametrize("agentes", [[], [{"name": "x", "is_active": False}]])
    def test_sin_activos_no_hay_principal(self, agentes: list[dict[str, Any]]) -> None:
        assert svc._agente_principal(agentes) is None


class TestSerializacion:
    def test_los_agentes_sobreviven_a_un_viaje_por_json(self) -> None:
        fila = {
            "name": "Asistente",
            "system_prompt": "Hola",
            "welcome_message": None,
            "model": "gpt-4o",
            "temperature": 0.3,
            "max_tokens": 1024,
            "training_mode": False,
            "similarity_threshold": 0.8,
            "handoff_message": None,
            "config": {"rag_top_k": 5},
            "is_active": True,
            "created_at": AHORA,
        }

        recuperada = svc._agentes_de_json(svc._agentes_a_json([fila]))

        assert recuperada == [fila]

    def test_las_respuestas_recuperan_el_uuid_o_el_none(self) -> None:
        autor = uuid.uuid4()
        guardadas = [
            {
                "shortcut": "/a",
                "title": "a",
                "content": "c",
                "category": None,
                "created_by": str(autor),
            },
            {"shortcut": "/b", "title": "b", "content": "c", "category": None, "created_by": None},
        ]

        recuperadas = svc._respuestas_de_json(guardadas)

        assert recuperadas[0]["created_by"] == autor
        assert recuperadas[1]["created_by"] is None

    def test_a_json_convierte_uuid_y_fechas_y_deja_lo_demas(self) -> None:
        identificador = uuid.uuid4()

        assert svc._a_json(identificador) == str(identificador)
        assert svc._a_json(AHORA) == AHORA.isoformat()
        assert svc._a_json({"a": 1}) == {"a": 1}


class TestValidacionesPreviasALaBase:
    """Fallan antes de abrir ninguna transaccion: no hace falta una base."""

    @staticmethod
    def _sin_base(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
        """Si el servicio llegara a abrir una sesion, lo anota (y revienta)."""
        abierta: list[bool] = []

        def _no(*_: Any, **__: Any) -> None:
            abierta.append(True)
            raise AssertionError("no deberia abrir una sesion")

        monkeypatch.setattr(svc, "tenant_session", _no)
        return abierta

    @pytest.mark.parametrize("campo", ["name", "model", "temperature", "max_tokens", "is_active"])
    async def test_un_campo_obligatorio_en_null_se_rechaza(
        self, monkeypatch: pytest.MonkeyPatch, campo: str
    ) -> None:
        abierta = self._sin_base(monkeypatch)

        with pytest.raises(svc.CampoNoAnulableError) as error:
            await svc.actualizar_config_sandbox(uuid.uuid4(), {campo: None, "system_prompt": "x"})

        assert error.value.campos == [campo]
        assert abierta == []

    async def test_se_nombran_todos_los_campos_nulos_a_la_vez(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._sin_base(monkeypatch)

        with pytest.raises(svc.CampoNoAnulableError) as error:
            await svc.actualizar_config_sandbox(uuid.uuid4(), {"name": None, "model": None})

        assert error.value.campos == ["model", "name"]

    @pytest.mark.parametrize("campo", ["system_prompt", "welcome_message", "handoff_message"])
    async def test_los_textos_si_admiten_null_para_borrarlos(
        self, monkeypatch: pytest.MonkeyPatch, campo: str
    ) -> None:
        """El `null` pasa la validacion y llega a la base (que aqui se corta a proposito)."""
        abierta = self._sin_base(monkeypatch)

        with pytest.raises(AssertionError, match="no deberia abrir"):
            await svc.actualizar_config_sandbox(uuid.uuid4(), {campo: None})

        assert abierta == [True]

    @pytest.mark.parametrize("clave", ["clinical", "marketing", "feature_flags"])
    async def test_las_claves_del_tenant_se_rechazan_antes_de_tocar_nada(
        self, monkeypatch: pytest.MonkeyPatch, clave: str
    ) -> None:
        abierta = self._sin_base(monkeypatch)

        with pytest.raises(svc.ClaveDelTenantError) as error:
            await svc.actualizar_config_sandbox(
                uuid.uuid4(), {"config": {clave: {}, "rag_top_k": 1}}
            )

        assert error.value.claves == [clave]
        assert abierta == []


class TestDefinicionDeTablas:
    """Erratas y derivas que, sin esto, solo saldrian al reiniciar un sandbox real."""

    @pytest.mark.parametrize("nombre", [*svc._TABLAS_DE_PRUEBA, *svc._TABLAS_CLONADAS])
    def test_cada_tabla_del_reset_existe_y_tiene_client_id(self, nombre: str) -> None:
        tabla = Base.metadata.tables[nombre]

        assert "client_id" in tabla.c

    def test_el_reset_no_toca_el_consumo_de_tokens(self) -> None:
        """Si se vaciaran, `POST /reset` en bucle saltaria el tope `SANDBOX_TOKEN_BUDGET`."""
        todas = {*svc._TABLAS_DE_PRUEBA, *svc._TABLAS_CLONADAS}

        assert "token_budgets" not in todas
        assert "token_usage_logs" not in todas

    def test_las_tablas_no_se_repiten_ni_se_solapan(self) -> None:
        todas = [*svc._TABLAS_DE_PRUEBA, *svc._TABLAS_CLONADAS]

        assert len(todas) == len(set(todas))

    def test_lo_que_no_se_clona_es_un_subconjunto_de_lo_del_tenant(self) -> None:
        assert set(svc._CLAVES_QUE_NO_SE_CLONAN) <= set(svc.CLAVES_DEL_TENANT)
        assert (
            "feature_flags" not in svc._CLAVES_QUE_NO_SE_CLONAN
        )  # el sandbox se comporta como prod
