"""Tests de `app/agents/nodes/scheduling.py` — sin LLM real, sin red.

`get_chat_model` y las tools se sustituyen por dobles: lo que se prueba aquí es
el flujo del nodo (decidir tool, ejecutarla, generar la respuesta final, o
escalar si el agendamiento no está disponible), no el modelo ni la API de
Google (esas viven en `test_calendar_service.py` y `test_calendar_tools.py`).
"""

import uuid
from typing import Any

import pytest
from googleapiclient.errors import HttpError

from app.agents.nodes import scheduling as modulo
from app.services.calendar import CalendarCredentialsError, SchedulingNotConfiguredError
from tests.unit.agent_doubles import FakeSession, parchear_tenant_session

# ─── Dobles ──────────────────────────────────────────────────────────────────


class _FakeAgentConfig:
    """Sustituto mínimo de `AgentConfig` con lo que lee `_tenant_scheduling_context`."""

    def __init__(self, config: dict[str, Any] | None) -> None:
        self.config = config


class _FakeServiceType:
    """Sustituto mínimo de `ServiceType`."""

    def __init__(self, name: str = "Consulta", description: str | None = None, duration_minutes: int = 60) -> None:
        self.name = name
        self.description = description
        self.duration_minutes = duration_minutes


class _FakeAIMessage:
    """Sustituto de `AIMessage`: sin `usage_metadata` para no tocar TokenBudgetGuard."""

    def __init__(self, content: str = "", tool_calls: list[dict[str, Any]] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeLLM:
    """Sustituto de `ChatOpenAI`: sirve respuestas de una cola compartida."""

    def __init__(self, cola: list[_FakeAIMessage]) -> None:
        self._cola = cola

    def bind_tools(self, tools: list[Any]) -> "_FakeLLM":
        """Ignora las tools (no hace falta un LLM real para probar el nodo)."""
        return self

    async def ainvoke(self, messages: list[dict[str, Any]]) -> _FakeAIMessage:
        """Devuelve la siguiente respuesta programada."""
        return self._cola.pop(0)


class _FakeTool:
    """Sustituto de una tool de `calendar_tools.py`."""

    def __init__(
        self, name: str, resultado: str = "ok", excepcion: Exception | None = None
    ) -> None:
        self.name = name
        self._resultado = resultado
        self._excepcion = excepcion
        self.llamada: dict[str, Any] | None = None

    async def ainvoke(self, args: dict[str, Any], config: dict[str, Any]) -> str:
        """Registra la llamada y devuelve el resultado (o lanza la excepción configurada)."""
        self.llamada = {"args": args, "config": config}
        if self._excepcion is not None:
            raise self._excepcion
        return self._resultado


def _parchear_llm(monkeypatch: pytest.MonkeyPatch, respuestas: list[_FakeAIMessage]) -> None:
    cola = list(respuestas)
    monkeypatch.setattr(modulo, "get_chat_model", lambda model, temperature=0.0: _FakeLLM(cola))


def _estado(
    client_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    texto: str = "quiero una cita",
) -> dict[str, Any]:
    return {
        "client_id": str(client_id or uuid.uuid4()),
        "conversation_id": str(conversation_id or uuid.uuid4()),
        "message": {"text": texto},
        "model_to_use": "gpt-4o",
    }


# ─── _tenant_scheduling_context ──────────────────────────────────────────────


class TestTenantSchedulingContext:
    """Arma el texto de service types y resuelve el timezone del tenant."""

    async def test_sin_service_types(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin tipos activos, el texto lo dice explícitamente."""
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[[], None]))

        texto, _timezone = await modulo._tenant_scheduling_context(uuid.uuid4())

        assert texto == "No hay tipos de servicio configurados."

    async def test_lista_los_service_types_activos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cada tipo aparece con nombre, descripción y duración."""
        tipo = _FakeServiceType(name="Corte", description="Corte de cabello", duration_minutes=30)
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[[tipo], None]))

        texto, _timezone = await modulo._tenant_scheduling_context(uuid.uuid4())

        assert "Corte" in texto
        assert "30 minutos" in texto

    async def test_timezone_del_tenant_manda_sobre_el_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`config.scheduling.timezone` se respeta si el tenant lo configuró."""
        config = _FakeAgentConfig({"scheduling": {"timezone": "UTC"}})
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[[], config]))

        _texto, timezone = await modulo._tenant_scheduling_context(uuid.uuid4())

        assert timezone == "UTC"

    async def test_sin_config_cae_al_timezone_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin `agent_configs` activo, se usa el default del módulo de calendario."""
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[[], None]))

        _texto, timezone = await modulo._tenant_scheduling_context(uuid.uuid4())

        assert timezone == modulo.DEFAULT_TIMEZONE


# ─── scheduling_node ──────────────────────────────────────────────────────────


class TestSchedulingNode:
    """`scheduling_node`: decide, ejecuta tools y genera la respuesta final."""

    async def test_respuesta_directa_sin_tool_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Una pregunta de aclaración del LLM no necesita ejecutar ninguna tool."""
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[[], None]))
        _parchear_llm(monkeypatch, [_FakeAIMessage(content="¿Para qué servicio quieres la cita?")])

        resultado = await modulo.scheduling_node(_estado())

        assert resultado == {
            "response_text": "¿Para qué servicio quieres la cita?",
            "intent": "scheduling",
        }

    async def test_ejecuta_la_tool_y_genera_respuesta_final(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con un tool_call, se ejecuta la tool y una segunda llamada redacta la respuesta."""
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[[], None]))
        _parchear_llm(
            monkeypatch,
            [
                _FakeAIMessage(
                    tool_calls=[
                        {"name": "check_availability", "args": {"date": "2026-09-21"}, "id": "1"}
                    ]
                ),
                _FakeAIMessage(content="Tienes disponible a las 10am."),
            ],
        )
        tool = _FakeTool("check_availability", resultado="Horarios: 10:00 - 11:00")
        monkeypatch.setattr(modulo, "_TOOLS_BY_NAME", {"check_availability": tool})

        client_id = uuid.uuid4()
        conversation_id = uuid.uuid4()
        resultado = await modulo.scheduling_node(
            _estado(client_id=client_id, conversation_id=conversation_id)
        )

        assert resultado == {"response_text": "Tienes disponible a las 10am.", "intent": "scheduling"}
        assert tool.llamada["args"] == {"date": "2026-09-21"}
        assert tool.llamada["config"] == {
            "configurable": {"client_id": str(client_id), "conversation_id": str(conversation_id)}
        }

    async def test_tool_no_reconocida_no_rompe_el_nodo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un nombre de tool que no existe se reporta como texto, no como crash."""
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[[], None]))
        _parchear_llm(
            monkeypatch,
            [
                _FakeAIMessage(tool_calls=[{"name": "tool_inventada", "args": {}, "id": "1"}]),
                _FakeAIMessage(content="No pude completar esa acción."),
            ],
        )
        monkeypatch.setattr(modulo, "_TOOLS_BY_NAME", {})

        resultado = await modulo.scheduling_node(_estado())

        assert resultado["response_text"] == "No pude completar esa acción."

    @pytest.mark.parametrize(
        "excepcion",
        [
            SchedulingNotConfiguredError("sin calendario"),
            CalendarCredentialsError("sin service account"),
            HttpError(resp=type("Resp", (), {"reason": "x", "status": 500})(), content=b"boom"),
        ],
    )
    async def test_falla_de_agendamiento_escala_a_humano(
        self, monkeypatch: pytest.MonkeyPatch, excepcion: Exception
    ) -> None:
        """Sin calendario configurado (o la API de Google caída), se escala en vez de crashear."""
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[[], None]))
        _parchear_llm(
            monkeypatch,
            [_FakeAIMessage(tool_calls=[{"name": "create_appointment", "args": {}, "id": "1"}])],
        )
        tool = _FakeTool("create_appointment", excepcion=excepcion)
        monkeypatch.setattr(modulo, "_TOOLS_BY_NAME", {"create_appointment": tool})

        resultado = await modulo.scheduling_node(_estado())

        assert resultado == {
            "requires_handoff": True,
            "handoff_reason": modulo.SCHEDULING_ERROR_REASON,
        }
