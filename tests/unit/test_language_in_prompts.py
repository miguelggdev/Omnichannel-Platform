"""La instruccion de idioma llega al prompt de cada nodo que genera texto (Sprint 14b).

Criterio de aceptacion del spec: "El agente responde en el idioma del contacto".
Cada nodo arma su propio prompt, asi que cada uno necesita su prueba: si alguno
olvida `con_idioma()`, ese agente contesta en el idioma del prompt, no del contacto.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents.nodes import clinical as clinical_mod
from app.agents.nodes import financial as financial_mod
from app.agents.nodes import marketing as marketing_mod
from app.agents.nodes import rag_query as rag_mod
from app.agents.nodes import scheduling as scheduling_mod
from app.agents.nodes._tenant import AgentSettings
from tests.unit.agent_doubles import FakeChatModel, RespuestaLLM, estado
from tests.unit.test_clinical_agent import CLIENT_ID, _nodo
from tests.unit.test_rag_node import FakeRAGService, _chunk, _preparar

INSTRUCCION_EN = "Responde siempre en ingles (en)."
INSTRUCCION = "Responde siempre en"


def _ajustes(*agentes: str) -> AsyncMock:
    return AsyncMock(return_value=SimpleNamespace(enabled_agents=agentes, model="gpt-4o"))


class TestRag:
    async def test_el_idioma_se_agrega_al_prompt_de_sistema_del_tenant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        modelo, _, _ = _preparar(
            monkeypatch,
            FakeRAGService(chunks=[_chunk(0.9)]),
            settings=AgentSettings(model="gpt-4o", system_prompt="Eres el asistente de Sol."),
        )

        await rag_mod.rag_query_node(estado(detected_language="en"))

        sistema = modelo.llamadas[0][0]
        assert sistema["role"] == "system"
        assert sistema["content"] == f"Eres el asistente de Sol.\n\n{INSTRUCCION_EN}"

    async def test_sin_prompt_del_tenant_la_instruccion_va_sola(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        modelo, _, _ = _preparar(monkeypatch, FakeRAGService(chunks=[_chunk(0.9)]))

        await rag_mod.rag_query_node(estado(detected_language="fr"))

        assert modelo.llamadas[0][0] == {
            "role": "system",
            "content": "Responde siempre en frances (fr).",
        }

    async def test_sin_idioma_el_mensaje_no_cambia(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Compatibilidad: sin `detected_language` no aparece ningun mensaje de sistema nuevo."""
        modelo, _, _ = _preparar(monkeypatch, FakeRAGService(chunks=[_chunk(0.9)]))

        await rag_mod.rag_query_node(estado())

        assert [m["role"] for m in modelo.llamadas[0]] == ["user"]


class TestAgentesConTools:
    async def test_financiero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(financial_mod, "get_agent_settings", _ajustes("rag", "financial"))
        monkeypatch.setattr(financial_mod, "construir_contexto", AsyncMock(return_value="ctx"))
        con_tools = AsyncMock(return_value="ok")
        monkeypatch.setattr(financial_mod, "responder_con_tools", con_tools)

        await financial_mod.financial_node(
            {"client_id": CLIENT_ID, "message": {"text": "x"}, "detected_language": "en"}  # type: ignore[typeddict-item]
        )

        assert con_tools.await_args.kwargs["system_prompt"].endswith(f"\n\n{INSTRUCCION_EN}")

    async def test_marketing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(marketing_mod, "get_agent_settings", _ajustes("rag", "marketing"))

        class _Sesion:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *_: Any) -> None:
                return None

        monkeypatch.setattr(marketing_mod, "tenant_session", lambda _: _Sesion())
        monkeypatch.setattr(marketing_mod, "es_operador_de_marketing", AsyncMock(return_value=True))
        con_tools = AsyncMock(return_value="ok")
        monkeypatch.setattr(marketing_mod, "responder_con_tools", con_tools)

        await marketing_mod.marketing_node(
            estado(client_id=CLIENT_ID, channel="whatsapp", detected_language="de")  # type: ignore[arg-type]
        )

        assert con_tools.await_args.kwargs["system_prompt"].endswith(
            "\n\nResponde siempre en aleman (de)."
        )

    async def test_clinico(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llamadas = _nodo(monkeypatch, agentes=("rag", "clinical"), profesional=True)

        await clinical_mod.clinical_agent_node(
            estado(client_id=CLIENT_ID, channel="whatsapp", detected_language="pt")  # type: ignore[arg-type]
        )

        assert llamadas["responder"]["system_prompt"].endswith(
            "\n\nResponde siempre en portugues (pt)."
        )

    async def test_agendamiento(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            scheduling_mod,
            "_tenant_scheduling_context",
            AsyncMock(return_value=("Consulta", "America/Bogota")),
        )
        modelo = _ModeloQueEnlazaTools(RespuestaLLM("listo"))
        monkeypatch.setattr(scheduling_mod, "get_chat_model", lambda *a, **k: modelo)
        monkeypatch.setattr(scheduling_mod.TokenBudgetGuard, "record_usage", AsyncMock())

        await scheduling_mod.scheduling_node(estado(detected_language="it"))  # type: ignore[arg-type]

        sistema = modelo.llamadas[0][0]["content"]
        assert sistema.endswith("\n\nResponde siempre en italiano (it).")


class _ModeloQueEnlazaTools(FakeChatModel):
    """`FakeChatModel` con `bind_tools()`, como el modelo que usa el agendamiento."""

    def bind_tools(self, tools: Any) -> "_ModeloQueEnlazaTools":
        return self
