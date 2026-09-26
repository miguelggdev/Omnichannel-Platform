"""Tests del bucle de tool calling compartido por los agentes (Sprint 12, BUG-045)."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents.nodes import _agent_tools as at


class _LLM:
    """Modelo falso: la primera llamada pide tools, la segunda redacta."""

    def __init__(self, tool_calls: list[dict[str, Any]]) -> None:
        self.tool_calls = tool_calls
        self.mensajes: list[list[dict[str, str]]] = []

    def bind_tools(self, _tools: list[Any]) -> "_LLM":
        return self

    async def ainvoke(self, mensajes: list[dict[str, str]]) -> Any:
        self.mensajes.append(mensajes)
        if len(self.mensajes) == 1:
            return SimpleNamespace(content="", tool_calls=self.tool_calls)
        return SimpleNamespace(content="respuesta final", tool_calls=[])


class _Tool:
    def __init__(self, name: str, efecto: Any) -> None:
        self.name = name
        self.ainvoke = AsyncMock(side_effect=efecto)


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> _LLM:
    modelo = _LLM([{"name": "rota", "args": {}}, {"name": "sana", "args": {}}])
    monkeypatch.setattr(at, "get_chat_model", lambda *_a, **_k: modelo)
    monkeypatch.setattr(at.TokenBudgetGuard, "record_usage", AsyncMock())
    return modelo


async def test_el_fallo_de_una_tool_no_tumba_el_turno(llm: _LLM) -> None:
    """Antes la excepcion subia a ai_processor, que reintentaba el turno entero
    y volvia a ejecutar las tools que ya habian escrito."""
    rota = _Tool("rota", ValueError("args mal formados"))
    sana = _Tool("sana", lambda *_a, **_k: "hecho")

    texto = await at.responder_con_tools(
        state={"client_id": "c", "message": {"text": "hola"}},
        tools=[rota, sana],
        system_prompt="sistema",
        modelo="gpt-4o",
        operacion="prueba",
    )

    assert texto == "respuesta final"
    sana.ainvoke.assert_awaited_once()
    resultados = llm.mensajes[1][-1]["content"]
    assert "'rota' no pudo completarse (ValueError)" in resultados
    assert "hecho" in resultados


async def test_un_fallo_del_llm_si_se_propaga(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los reintentos de ai_processor son para los fallos del modelo."""

    class _Caido(_LLM):
        async def ainvoke(self, mensajes: list[dict[str, str]]) -> Any:
            raise RuntimeError("OpenAI 503")

    monkeypatch.setattr(at, "get_chat_model", lambda *_a, **_k: _Caido([]))

    with pytest.raises(RuntimeError, match="503"):
        await at.responder_con_tools(
            state={"client_id": "c", "message": {"text": "hola"}},
            tools=[],
            system_prompt="sistema",
            modelo="gpt-4o",
            operacion="prueba",
        )
