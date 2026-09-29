"""Tests del agente clinico: tools, nodo, routing y privacidad del log (Sprint 13, Dev B)."""

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-testing-only-minimum-32-chars")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-minimum-32-characters-long")

from app.agents.graph import build_conversation_graph, route_after_intent, route_after_sentiment
from app.agents.middleware import logging_middleware as lm
from app.agents.nodes import clinical as nodo
from app.agents.nodes._tenant import AgentSettings
from app.agents.nodes.intent_router import (
    IntentClassification,
    available_intents,
    build_system_prompt,
)
from app.agents.tools import clinical_tools as ct
from app.agents.tools.clinical_tools import CLINICAL_TOOLS
from tests.unit.agent_doubles import FakeSession, estado, parchear_agent_settings

CLIENT_ID = str(uuid.uuid4())
PROFESIONAL = str(uuid.uuid4())
CONFIG = {
    "configurable": {
        "client_id": CLIENT_ID,
        "contact_id": PROFESIONAL,
        "conversation_id": str(uuid.uuid4()),
    }
}
SIN_CONTACTO = {"configurable": {"client_id": CLIENT_ID, "contact_id": None}}


def _sesion(monkeypatch: pytest.MonkeyPatch, sesion: FakeSession) -> FakeSession:
    @asynccontextmanager
    async def _cm(client_id: Any, user_id: Any = None) -> Any:
        yield sesion

    monkeypatch.setattr(ct, "tenant_session", _cm)
    return sesion


@pytest.fixture
def catalogo_vacio(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    """Sin catalogo oficial cargado: rige el subconjunto de referencia."""
    return _sesion(monkeypatch, FakeSession(resultados=[None] * 8))


@pytest.mark.usefixtures("catalogo_vacio")
class TestCodificacion:
    async def test_cie10_hipertension_esencial(self) -> None:
        """Criterio 8 del spec."""
        resultado = await ct.code_cie10.ainvoke(
            {"diagnosis": "Hipertensión esencial"}, config=CONFIG
        )

        assert any(m["code"] == "I10" for m in resultado["matches"])

    async def test_cie10_sin_coincidencia_no_propone_un_codigo(self) -> None:
        """ADR-072: el spec caia a un LLM y devolvia un codigo inventado."""
        resultado = await ct.code_cie10.ainvoke(
            {"diagnosis": "sindrome inexistente xyz"}, config=CONFIG
        )

        assert resultado["matches"] == []
        assert "No propongas un codigo" in resultado["note"]

    async def test_cups_consulta_primera_vez(self) -> None:
        """Criterio 9 del spec."""
        resultado = await ct.code_cups.ainvoke(
            {"procedure": "Consulta de primera vez por medicina general"}, config=CONFIG
        )

        assert any(m["code"] == "890201" for m in resultado["matches"])

    async def test_cups_sin_coincidencia(self) -> None:
        assert (await ct.code_cups.ainvoke({"procedure": "cirugia rara"}, config=CONFIG))[
            "matches"
        ] == []

    async def test_buscar_cie10_por_capitulo(self) -> None:
        resultado = await ct.search_cie10.ainvoke(
            {"query": "aguda", "category": "J"}, config=CONFIG
        )

        assert resultado["total"] > 0
        assert all(r["code"].startswith("J") for r in resultado["results"])

    async def test_buscar_cie10_por_rango(self) -> None:
        resultado = await ct.search_cie10.ainvoke(
            {"query": "diarrea", "category": "A00-B99"}, config=CONFIG
        )

        assert [r["code"] for r in resultado["results"]] == ["A09"]

    async def test_buscar_cups_por_grupo(self) -> None:
        resultado = await ct.search_cups.ainvoke(
            {"query": "consulta", "group": "89"}, config=CONFIG
        )

        assert resultado["total"] == 2
        assert (await ct.search_cups.ainvoke({"query": "consulta", "group": "87"}, config=CONFIG))[
            "total"
        ] == 0


_RIPS_OK: dict[str, Any] = {
    "patient_document_type": "CC",
    "patient_document_number": "1234567890",
    "service_date": "2025-01-15",
    "service_type": "consulta",
    "rips_type": "AC",
    "diagnosis_codes": [{"code": "I10", "description": "HTA", "type": "principal"}],
    "purpose_code": "05",
}


class TestRips:
    async def test_sin_diagnostico_principal(self) -> None:
        """Criterio del spec: RIPS requiere al menos un diagnostico principal."""
        resultado = await ct.create_rips_record.ainvoke(
            {**_RIPS_OK, "diagnosis_codes": [{"code": "J06.9", "type": "relacionado"}]},
            config=CONFIG,
        )

        assert resultado["success"] is False
        assert "principal" in resultado["error"].lower()

    async def test_la_finalidad_es_obligatoria(self) -> None:
        """El default `"01"` del spec era 'atencion del parto'."""
        args = {k: v for k, v in _RIPS_OK.items() if k != "purpose_code"}

        with pytest.raises(Exception, match="purpose_code"):
            await ct.create_rips_record.ainvoke(args, config=CONFIG)

    async def test_sin_profesional_identificado(self) -> None:
        resultado = await ct.create_rips_record.ainvoke(_RIPS_OK, config=SIN_CONTACTO)

        assert resultado["success"] is False
        assert "profesional" in resultado["error"]

    async def test_documento_invalido(self) -> None:
        resultado = await ct.create_rips_record.ainvoke(
            {**_RIPS_OK, "patient_document_type": "ZZ"}, config=CONFIG
        )

        assert resultado["success"] is False
        assert "Tipo de documento" in resultado["error"]

    async def test_sin_consentimiento_no_guarda(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Criterio 11: el tool lo exige en codigo, no solo el prompt."""
        sesion = _sesion(monkeypatch, FakeSession(resultados=[None, None]))

        resultado = await ct.create_rips_record.ainvoke(_RIPS_OK, config=CONFIG)

        assert resultado["error"] == "consent_required"
        assert sesion.added == []

    async def test_borrador_con_consentimiento(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Criterio 10: registro con diagnostico principal, tipo RIPS y finalidad."""
        consentimiento = SimpleNamespace(
            granted_at=datetime(2025, 1, 10, tzinfo=timezone.utc), consent_type="verbal"
        )
        sesion = _sesion(monkeypatch, FakeSession(resultados=[None, consentimiento, None, None]))

        resultado = await ct.create_rips_record.ainvoke(_RIPS_OK, config=CONFIG)

        assert resultado["success"] is True
        assert resultado["status"] == "draft"
        assert resultado["main_diagnosis"] == "I10"
        assert "unverified_codes" not in resultado
        assert len(sesion.added) == 1

    async def test_avisa_de_los_codigos_fuera_del_catalogo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        consentimiento = SimpleNamespace(
            granted_at=datetime(2025, 1, 10, tzinfo=timezone.utc), consent_type="verbal"
        )
        _sesion(monkeypatch, FakeSession(resultados=[None, consentimiento, None, None]))

        resultado = await ct.create_rips_record.ainvoke(
            {**_RIPS_OK, "diagnosis_codes": [{"code": "S72.00", "type": "principal"}]},
            config=CONFIG,
        )

        assert resultado["unverified_codes"] == ["S72.00"]
        assert "listado oficial" in resultado["warning"]

    async def test_registrar_consentimiento(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sesion = _sesion(monkeypatch, FakeSession(resultados=[None]))

        resultado = await ct.register_patient_consent.ainvoke(
            {"document_type": "CC", "document_number": "1234567890", "consent_type": "verbal"},
            config=CONFIG,
        )

        assert resultado["registered"] is True
        (consentimiento,) = sesion.added
        assert str(consentimiento.registered_by_contact_id) == PROFESIONAL

    async def test_registrar_consentimiento_sin_profesional(self) -> None:
        resultado = await ct.register_patient_consent.ainvoke(
            {"document_type": "CC", "document_number": "1234567890", "consent_type": "verbal"},
            config=SIN_CONTACTO,
        )

        assert resultado["success"] is False

    async def test_historial_sin_notas_ni_documento(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fila = SimpleNamespace(
            id=uuid.uuid4(),
            service_date=datetime(2025, 1, 15).date(),
            rips_type="AC",
            diagnosis_codes=[{"code": "I10", "description": "HTA", "type": "principal"}],
            status="draft",
            structured_notes={"subjective": "SECRETO"},
        )
        _sesion(monkeypatch, FakeSession(resultados=[[fila]]))

        resultado = await ct.get_patient_history.ainvoke(
            {"document_type": "CC", "document_number": "1234567890"}, config=CONFIG
        )

        assert resultado["total_count"] == 1
        assert resultado["records"][0]["main_diagnosis"] == "I10"
        assert "SECRETO" not in str(resultado)
        assert "1234567890" not in str(resultado)

    async def test_historial_sin_profesional(self) -> None:
        resultado = await ct.get_patient_history.ainvoke(
            {"document_type": "CC", "document_number": "1234567890"}, config=SIN_CONTACTO
        )

        assert resultado["success"] is False


class _LlmFalso:
    def __init__(self, contenido: str) -> None:
        self.contenido = contenido
        self.bound: dict[str, Any] = {}

    def bind(self, **kwargs: Any) -> "_LlmFalso":
        self.bound = kwargs
        return self

    async def ainvoke(self, mensajes: list[dict[str, str]]) -> Any:
        return SimpleNamespace(
            content=self.contenido, usage_metadata={"input_tokens": 30, "output_tokens": 10}
        )


class TestExtraccionDeEntidades:
    async def test_limpia_lo_que_devuelve_el_modelo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        salida = {
            "symptoms": [{"text": "tos", "normalized": "tos", "negated": False}, "basura", {}],
            "diagnoses": [{"text": "no fiebre", "negated": True}],
            "inventada": [{"text": "x"}],
        }
        llm = _LlmFalso(json.dumps(salida))
        monkeypatch.setattr(ct, "get_chat_model", lambda *_a, **_k: llm)
        registrar = AsyncMock()
        monkeypatch.setattr(ct.TokenBudgetGuard, "record_usage", registrar)

        resultado = await ct.extract_medical_entities.ainvoke(
            {"text": "Paciente con tos, niega fiebre"}, config=CONFIG
        )

        entidades = resultado["entities"]
        assert set(entidades) == set(ct.CATEGORIAS_ENTIDADES)
        assert entidades["symptoms"] == [{"text": "tos", "normalized": "tos", "negated": False}]
        assert entidades["diagnoses"][0]["negated"] is True
        assert "inventada" not in entidades
        assert llm.bound == {"response_format": {"type": "json_object"}}

    async def test_el_consumo_se_contabiliza_en_el_presupuesto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El spec llamaba a OpenAI por fuera de `TokenBudgetGuard`."""
        monkeypatch.setattr(ct, "get_chat_model", lambda *_a, **_k: _LlmFalso("{}"))
        registrar = AsyncMock()
        monkeypatch.setattr(ct.TokenBudgetGuard, "record_usage", registrar)

        await ct.extract_medical_entities.ainvoke({"text": "dictado"}, config=CONFIG)

        kwargs = registrar.await_args.kwargs
        assert kwargs["client_id"] == CLIENT_ID
        assert (kwargs["prompt_tokens"], kwargs["completion_tokens"]) == (30, 10)
        assert kwargs["operation"] == "clinical_entities"

    async def test_json_invalido(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ct, "get_chat_model", lambda *_a, **_k: _LlmFalso("no es json"))
        monkeypatch.setattr(ct.TokenBudgetGuard, "record_usage", AsyncMock())

        resultado = await ct.extract_medical_entities.ainvoke({"text": "dictado"}, config=CONFIG)

        assert resultado["success"] is False

    @pytest.mark.parametrize("texto", ["", "   ", "x" * 8001])
    async def test_dictado_vacio_o_enorme_no_llega_al_modelo(
        self, monkeypatch: pytest.MonkeyPatch, texto: str
    ) -> None:
        def _no(*_a: Any, **_k: Any) -> None:
            raise AssertionError("no debia llamar al LLM")

        monkeypatch.setattr(ct, "get_chat_model", _no)

        resultado = await ct.extract_medical_entities.ainvoke({"text": texto}, config=CONFIG)

        assert resultado["success"] is False


class TestSeguridadDeLasTools:
    def test_el_llm_no_puede_rellenar_identificadores_internos(self) -> None:
        """`client_id`/`contact_id`/`conversation_id` llegan por config, no por argumentos."""
        for herramienta in CLINICAL_TOOLS:
            argumentos = set(herramienta.args)
            assert not argumentos & {"client_id", "contact_id", "conversation_id", "config"}, (
                herramienta.name
            )

    def test_nombres_unicos(self) -> None:
        nombres = [t.name for t in CLINICAL_TOOLS]
        assert len(nombres) == len(set(nombres))


def _nodo(monkeypatch: pytest.MonkeyPatch, *, agentes: tuple[str, ...], profesional: bool) -> dict:
    llamadas: dict[str, Any] = {}
    parchear_agent_settings(
        monkeypatch,
        nodo,
        AgentSettings(model="gpt-4o", enabled_agents=agentes),
    )

    @asynccontextmanager
    async def _cm(client_id: Any) -> Any:
        yield None

    monkeypatch.setattr(nodo, "tenant_session", _cm)

    async def _es(session: Any, client_id: Any, contact_id: Any) -> bool:
        llamadas["contacto"] = contact_id
        return profesional

    monkeypatch.setattr(nodo, "es_profesional_clinico", _es)

    async def _responder(**kwargs: Any) -> str:
        llamadas["responder"] = kwargs
        return "respuesta del agente"

    monkeypatch.setattr(nodo, "responder_con_tools", _responder)
    return llamadas


class TestNodo:
    async def test_no_habilitado(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llamadas = _nodo(monkeypatch, agentes=("rag",), profesional=True)

        resultado = await nodo.clinical_agent_node(estado(client_id=CLIENT_ID))

        assert "no esta habilitado" in resultado["response_text"]
        assert "responder" not in llamadas

    async def test_un_cliente_no_llega_a_las_tools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mismo hueco que BUG-045: el grafo es el mismo que atiende a los clientes."""
        llamadas = _nodo(monkeypatch, agentes=("rag", "clinical"), profesional=False)

        resultado = await nodo.clinical_agent_node(
            estado(client_id=CLIENT_ID, contact_id=str(uuid.uuid4()))
        )

        assert resultado["response_text"] == nodo.MENSAJE_NO_AUTORIZADO
        assert "responder" not in llamadas
        # Ni menciona pacientes ni registros.
        assert "paciente" not in resultado["response_text"].lower()

    async def test_el_profesional_usa_las_tools_con_temperatura_cero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llamadas = _nodo(monkeypatch, agentes=("rag", "clinical"), profesional=True)

        resultado = await nodo.clinical_agent_node(
            estado(client_id=CLIENT_ID, contact_id=PROFESIONAL, model_to_use="gpt-4o-mini")
        )

        assert resultado == {"response_text": "respuesta del agente", "intent": "clinical"}
        assert llamadas["contacto"] == PROFESIONAL
        pedido = llamadas["responder"]
        assert pedido["tools"] is CLINICAL_TOOLS
        assert pedido["temperatura"] == 0.0
        assert pedido["modelo"] == "gpt-4o-mini"
        assert "NUNCA sugieras diagnosticos" in pedido["system_prompt"]


class TestRoutingEIntent:
    def test_el_intent_solo_se_ofrece_si_el_tenant_habilito_el_agente(self) -> None:
        assert "clinical" not in available_intents(("rag",))
        assert "clinical" in available_intents(("rag", "clinical"))

    def test_el_clasificador_conoce_la_categoria(self) -> None:
        prompt = build_system_prompt(available_intents(("rag", "clinical")))

        assert "- clinical:" in prompt
        assert IntentClassification(intent="clinical", confidence=0.9).intent == "clinical"

    def test_route_after_intent(self) -> None:
        assert route_after_intent(estado(intent="clinical")) == "clinical"

    def test_route_after_sentiment_sigue_al_intent(self) -> None:
        assert route_after_sentiment(estado(intent="clinical")) == "clinical"

    def test_el_enojo_sostenido_sigue_mandando_a_humano(self) -> None:
        assert (
            route_after_sentiment(estado(intent="clinical", requires_handoff=True))
            == "human_handoff"
        )

    def test_el_grafo_tiene_el_nodo_y_termina_en_respond(self) -> None:
        grafo = build_conversation_graph()

        assert "clinical" in grafo.nodes
        assert ("clinical", "respond") in grafo.edges
        assert build_conversation_graph().compile() is not None


class TestLogSinContenidoClinico:
    """`agent_action_logs` lo lee cualquier admin/supervisor y no esta cifrado."""

    def test_el_resumen_de_entrada_omite_lo_dictado(self) -> None:
        resumen = lm._build_input_summary(
            estado(intent="clinical", message={"text": "paciente Ana Perez con hipertension"}),
            "clinical",
        )

        assert "Ana Perez" not in resumen
        assert lm.CONTENIDO_OMITIDO in resumen
        assert "intent: clinical" in resumen

    def test_el_resumen_de_salida_omite_la_respuesta(self) -> None:
        resumen = lm._build_output_summary(
            {"intent": "clinical", "response_text": "Registro de Ana Perez creado"},
            "clinical",
            sensible=True,
        )

        assert "Ana Perez" not in resumen
        assert "intent: clinical" in resumen

    def test_el_resto_de_los_intents_se_registra_como_siempre(self) -> None:
        resumen = lm._build_input_summary(
            estado(intent="rag_query", message={"text": "horario de atencion"}), "rag_query"
        )

        assert "horario de atencion" in resumen

    def test_un_nodo_que_fija_el_intent_clinico_en_su_resultado(self) -> None:
        assert lm._es_sensible(estado(), {"intent": "clinical"}) is True
        assert lm._es_sensible(estado(), {"intent": "financial"}) is False

    async def test_el_wrapper_no_persiste_el_dictado(self, monkeypatch: pytest.MonkeyPatch) -> None:
        registrado: dict[str, Any] = {}

        async def _registrar(**kwargs: Any) -> None:
            registrado.update(kwargs)

        monkeypatch.setattr(lm, "_registrar", _registrar)

        async def _nodo_falso(state: Any) -> dict[str, Any]:
            return {"response_text": "Registro de Ana Perez", "intent": "clinical"}

        envuelto = lm.logged_node("clinical", "tool_call")(_nodo_falso)
        await envuelto(
            estado(
                client_id=CLIENT_ID,
                conversation_id=str(uuid.uuid4()),
                message={"text": "dictado de Ana Perez"},
            )
        )

        assert "Ana Perez" not in registrado["input_summary"]
        assert "Ana Perez" not in registrado["output_summary"]
