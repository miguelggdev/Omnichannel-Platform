"""Tests del nodo de routing semantico (Sprint 6).

Contrato: `specs/sprint-06-langgraph.md` §4. El LLM se sustituye por un doble:
lo que se verifica aqui es el contrato del nodo (que ofrece solo los intents
habilitados, que reencamina los que no lo estan y que contabiliza el consumo),
no la calidad de clasificacion de OpenAI.
"""

from typing import Any

import pytest

from app.agents.nodes import intent_router as nodo
from app.agents.nodes._tenant import AgentSettings
from app.agents.nodes.intent_router import IntentClassification, available_intents
from tests.unit.agent_doubles import (
    FakeChatModel,
    RespuestaLLM,
    estado,
    parchear_agent_settings,
    parchear_chat_model,
)


def _respuesta(intent: str, confidence: float = 0.9, tokens: int = 0) -> dict[str, Any]:
    """Arma lo que devuelve `with_structured_output(..., include_raw=True)`.

    Args:
        intent: Intent que devuelve el clasificador.
        confidence: Confianza asociada.
        tokens: Tokens de entrada a reportar en la respuesta cruda.

    Returns:
        Dict con `parsed` y `raw`, como LangChain.
    """
    return {
        "parsed": IntentClassification(intent=intent, confidence=confidence),
        "raw": RespuestaLLM(input_tokens=tokens, output_tokens=tokens),
    }


def _preparar(
    monkeypatch: pytest.MonkeyPatch,
    respuesta: Any,
    agentes: tuple[str, ...] = ("rag",),
) -> tuple[FakeChatModel, list[dict[str, Any]]]:
    """Deja el nodo listo con LLM falso, config fija y consumo capturado.

    Args:
        monkeypatch: Fixture de pytest.
        respuesta: Lo que devolvera el LLM.
        agentes: Agentes habilitados del tenant.

    Returns:
        Tupla `(modelo_falso, registros_de_consumo)`.
    """
    modelo = FakeChatModel(respuesta)
    parchear_chat_model(monkeypatch, nodo, modelo)
    parchear_agent_settings(
        monkeypatch, nodo, AgentSettings(model="gpt-4o", enabled_agents=agentes)
    )

    registros: list[dict[str, Any]] = []

    async def _record(**kwargs: Any) -> None:
        registros.append(kwargs)

    monkeypatch.setattr(nodo.TokenBudgetGuard, "record_usage", _record)
    return modelo, registros


class TestClasificacion:
    """El nodo devuelve el intent y la confianza del clasificador."""

    @pytest.mark.parametrize(
        "intent",
        ["greeting", "farewell", "human_request", "complaint", "rag_query"],
    )
    async def test_devuelve_el_intent_clasificado(
        self, monkeypatch: pytest.MonkeyPatch, intent: str
    ) -> None:
        """Un intent habilitado pasa tal cual al estado."""
        _preparar(monkeypatch, _respuesta(intent, 0.87))

        resultado = await nodo.intent_routing_node(estado())

        assert resultado["intent"] == intent
        assert resultado["intent_confidence"] == pytest.approx(0.87)

    async def test_mensaje_sin_texto_no_invoca_al_llm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un mensaje de solo media se clasifica como unknown sin gastar tokens."""
        modelo, registros = _preparar(monkeypatch, _respuesta("greeting"))

        resultado = await nodo.intent_routing_node(estado(message={"media_url": "http://x/a.jpg"}))

        assert resultado["intent"] == "unknown"
        assert resultado["intent_confidence"] == 0.0
        assert modelo.llamadas == []
        assert registros == []

    async def test_respuesta_no_parseable_cae_en_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si el structured output no trae `parsed`, no se inventa un intent."""
        _preparar(monkeypatch, {"parsed": None, "raw": RespuestaLLM()})

        resultado = await nodo.intent_routing_node(estado())

        assert resultado["intent"] == "unknown"
        assert resultado["intent_confidence"] == 0.0

    async def test_acepta_el_modelo_sin_include_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Si LangChain devuelve el modelo pelado, el nodo tambien lo entiende."""
        _preparar(monkeypatch, IntentClassification(intent="farewell", confidence=0.5))

        resultado = await nodo.intent_routing_node(estado())

        assert resultado["intent"] == "farewell"


class TestAgentesHabilitados:
    """Solo se enruta a agentes habilitados del tenant."""

    def test_scheduling_no_se_ofrece_si_el_agente_esta_apagado(self) -> None:
        """Sin agente de agendamiento, `scheduling` no es una opcion."""
        assert "scheduling" not in available_intents(("rag",))

    def test_scheduling_se_ofrece_si_el_agente_esta_encendido(self) -> None:
        """Con el agente habilitado, el intent aparece."""
        assert "scheduling" in available_intents(("rag", "scheduling"))

    async def test_intent_de_agente_apagado_se_reencamina_a_rag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si el modelo devuelve `scheduling` sin agente, no se enruta alli."""
        _preparar(monkeypatch, _respuesta("scheduling"), agentes=("rag",))

        resultado = await nodo.intent_routing_node(estado(message={"text": "Quiero una cita"}))

        assert resultado["intent"] == "rag_query"

    async def test_sin_rag_las_consultas_van_a_un_humano(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin agente RAG no hay quien conteste: se transfiere."""
        _preparar(monkeypatch, _respuesta("rag_query"), agentes=("scheduling",))

        resultado = await nodo.intent_routing_node(estado())

        assert resultado["intent"] == "human_request"

    async def test_sin_rag_el_unknown_tambien_va_a_un_humano(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`unknown` termina en RAG por el routing del grafo; sin RAG, a humano."""
        _preparar(monkeypatch, _respuesta("unknown"), agentes=("scheduling",))

        resultado = await nodo.intent_routing_node(estado())

        assert resultado["intent"] == "human_request"

    async def test_el_prompt_solo_lista_los_intents_disponibles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El clasificador no ve categorias que el tenant no tiene habilitadas."""
        modelo, _ = _preparar(monkeypatch, _respuesta("greeting"), agentes=("rag",))

        await nodo.intent_routing_node(estado())

        system_prompt = modelo.llamadas[0][0]["content"]
        assert "rag_query" in system_prompt
        assert "scheduling" not in system_prompt


class TestConsumo:
    """El nodo contabiliza lo que gasta."""

    async def test_registra_el_consumo_con_el_modelo_barato(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La clasificacion siempre usa el modelo barato y lo registra asi."""
        modelo_falso, registros = _preparar(monkeypatch, _respuesta("greeting", tokens=40))
        state = estado()

        await nodo.intent_routing_node(state)

        assert len(registros) == 1
        assert registros[0]["model"] == "gpt-4o-mini"
        assert registros[0]["operation"] == "intent_routing"
        assert registros[0]["prompt_tokens"] == 40
        assert registros[0]["conversation_id"] == state["conversation_id"]
        assert modelo_falso.structured_con == (IntentClassification, True)

    async def test_el_error_del_llm_no_se_atrapa(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un fallo de OpenAI sube a la tarea de Celery, que reintenta."""

        class ModeloQueFalla(FakeChatModel):
            async def ainvoke(self, messages: Any) -> Any:
                raise RuntimeError("openai caido")

        modelo = ModeloQueFalla(None)
        parchear_chat_model(monkeypatch, nodo, modelo)
        parchear_agent_settings(monkeypatch, nodo, AgentSettings(model="gpt-4o"))

        with pytest.raises(RuntimeError, match="openai caido"):
            await nodo.intent_routing_node(estado())
