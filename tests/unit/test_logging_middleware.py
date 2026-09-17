"""Tests de `app/agents/middleware/logging_middleware.py::logged_node`.

`AgentLogger`/`tenant_session` se sustituyen por dobles: estos tests verifican
el contrato del decorator (cuándo loguea, qué le pasa a `AgentLogger.log_action`,
que un fallo al loguear no tumbe la respuesta), no la escritura real en Postgres.
"""

import uuid
from typing import Any, ClassVar

import pytest

from app.agents.middleware import logging_middleware as modulo
from app.agents.middleware.logging_middleware import (
    _build_input_summary,
    _build_output_summary,
    _extract_details,
    logged_node,
)

# ─── Dobles ──────────────────────────────────────────────────────────────────


class _FakeAgentLogger:
    """Sustituto de `AgentLogger` que solo registra la llamada a `log_action`."""

    instancias: ClassVar[list["_FakeAgentLogger"]] = []

    def __init__(self, session: Any, client_id: uuid.UUID) -> None:
        self.session = session
        self.client_id = client_id
        self.llamada: dict[str, Any] | None = None
        _FakeAgentLogger.instancias.append(self)

    async def log_action(self, **kwargs: Any) -> None:
        self.llamada = kwargs


def _parchear(monkeypatch: pytest.MonkeyPatch, falla: bool = False) -> None:
    _FakeAgentLogger.instancias = []
    monkeypatch.setattr(modulo, "AgentLogger", _FakeAgentLogger)

    class _FakeTenantSession:
        async def __aenter__(self) -> str:
            if falla:
                raise ConnectionError("postgres caido")
            return "sesion-falsa"

        async def __aexit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr(modulo, "tenant_session", lambda _client_id: _FakeTenantSession())


def _estado(client_id: uuid.UUID | None = None, conversation_id: uuid.UUID | None = None) -> dict[str, Any]:
    return {
        "client_id": str(client_id or uuid.uuid4()),
        "conversation_id": str(conversation_id or uuid.uuid4()),
        "message": {"text": "hola"},
    }


# ─── Resúmenes ────────────────────────────────────────────────────────────────


class TestResumenes:
    """Los helpers de resumen leen los campos reales de `ConversationState`."""

    def test_input_summary_usa_mensaje_e_intent(self) -> None:
        """Con mensaje e intent, ambos aparecen en el resumen."""
        estado = {"message": {"text": "quiero una cita"}, "intent": "scheduling"}

        resumen = _build_input_summary(estado, "scheduling")  # type: ignore[arg-type]

        assert "quiero una cita" in resumen
        assert "scheduling" in resumen

    def test_input_summary_sin_nada_usa_placeholder(self) -> None:
        """Un estado vacío no debe producir un resumen vacío."""
        resumen = _build_input_summary({}, "rag_query")  # type: ignore[arg-type]

        assert resumen == "[rag_query input]"

    def test_output_summary_solo_incluye_campos_presentes(self) -> None:
        """Solo los campos que el nodo realmente devolvió aparecen en el resumen."""
        resultado = {"intent": "rag_query", "intent_confidence": 0.9}

        resumen = _build_output_summary(resultado, "intent_routing")

        assert "intent: rag_query" in resumen
        assert "intent_confidence: 0.9" in resumen

    def test_output_summary_sin_campos_conocidos_usa_placeholder(self) -> None:
        """Un resultado sin ninguno de los campos esperados usa el placeholder."""
        resumen = _build_output_summary({"algo_irrelevante": 1}, "respond")

        assert resumen == "[respond output]"

    def test_extract_details_solo_copia_las_claves_conocidas(self) -> None:
        """`details` no debe convertirse en un volcado completo del resultado."""
        resultado = {
            "requires_handoff": True,
            "handoff_reason": "insufficient_context",
            "response_text": "un texto que no debe ir a details",
        }

        details = _extract_details(resultado)

        assert details == {"requires_handoff": True, "handoff_reason": "insufficient_context"}


# ─── logged_node ──────────────────────────────────────────────────────────────


class TestLoggedNode:
    """`logged_node`: cuándo loguea, qué le pasa a `AgentLogger`, y el best-effort."""

    async def test_sin_client_id_corre_el_nodo_sin_loguear(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin `client_id`/`conversation_id`, el nodo corre pero no hay a dónde loguear."""
        _parchear(monkeypatch)

        async def nodo(estado: Any) -> dict[str, Any]:
            return {"response_text": "ok"}

        envuelto = logged_node("respond", "response")(nodo)
        resultado = await envuelto({"message": {"text": "hola"}})

        assert resultado == {"response_text": "ok"}
        assert _FakeAgentLogger.instancias == []

    async def test_registra_el_exito_con_duracion_y_resumen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un nodo exitoso queda registrado con status success y el resumen del output."""
        _parchear(monkeypatch)

        async def nodo(estado: Any) -> dict[str, Any]:
            return {"intent": "rag_query", "intent_confidence": 0.8}

        envuelto = logged_node("intent_routing", "decision")(nodo)
        estado = _estado()

        resultado = await envuelto(estado)

        assert resultado == {"intent": "rag_query", "intent_confidence": 0.8}
        assert len(_FakeAgentLogger.instancias) == 1
        llamada = _FakeAgentLogger.instancias[0].llamada
        assert llamada["node_name"] == "intent_routing"
        assert llamada["status"] == "success"
        assert llamada["duration_ms"] >= 0
        assert "intent: rag_query" in llamada["output_summary"]

    async def test_nodo_que_falla_se_registra_como_error_y_relanza(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un nodo que lanza excepción queda registrado como error, y la excepción sigue subiendo."""
        _parchear(monkeypatch)

        async def nodo(estado: Any) -> dict[str, Any]:
            raise ValueError("algo salio mal")

        envuelto = logged_node("rag_query", "query")(nodo)

        with pytest.raises(ValueError, match="algo salio mal"):
            await envuelto(_estado())

        llamada = _FakeAgentLogger.instancias[0].llamada
        assert llamada["status"] == "error"
        assert llamada["error_message"] == "algo salio mal"
        assert "traceback" in llamada["details"]

    async def test_fallo_al_loguear_no_tumba_la_respuesta(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si Postgres esta caido para escribir el log, el nodo igual responde."""
        _parchear(monkeypatch, falla=True)

        async def nodo(estado: Any) -> dict[str, Any]:
            return {"response_text": "respuesta al contacto"}

        envuelto = logged_node("respond", "response")(nodo)

        resultado = await envuelto(_estado())

        assert resultado == {"response_text": "respuesta al contacto"}

    async def test_tokens_y_modelo_se_leen_del_resultado_si_estan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un nodo que reporta `_tokens_used`/`_model_used` los propaga al log."""
        _parchear(monkeypatch)

        async def nodo(estado: Any) -> dict[str, Any]:
            return {"response_text": "ok", "_tokens_used": 123, "_model_used": "gpt-4o"}

        envuelto = logged_node("respond", "response")(nodo)

        await envuelto(_estado())

        llamada = _FakeAgentLogger.instancias[0].llamada
        assert llamada["tokens_used"] == 123
        assert llamada["model_used"] == "gpt-4o"

    async def test_sin_tokens_ni_modelo_quedan_en_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La mayoria de los nodos de Sprint 6 no reportan tokens: 0 y None, no un KeyError."""
        _parchear(monkeypatch)

        async def nodo(estado: Any) -> dict[str, Any]:
            return {"response_text": "ok"}

        envuelto = logged_node("respond", "response")(nodo)

        await envuelto(_estado())

        llamada = _FakeAgentLogger.instancias[0].llamada
        assert llamada["tokens_used"] == 0
        assert llamada["model_used"] is None
