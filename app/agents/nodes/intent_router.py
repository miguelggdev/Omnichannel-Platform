"""Nodo de routing semantico: clasifica el intent del mensaje entrante.

Contrato: `specs/sprint-06-langgraph.md` §4.

Siempre usa el modelo barato (`OPENAI_FALLBACK_MODEL`, gpt-4o-mini), tambien
cuando el presupuesto esta en verde: clasificar en siete categorias no mejora con
un modelo grande y es la llamada que mas se repite (una por mensaje).

Regla del sprint: **solo se enruta a agentes habilitados del tenant**. Los
intents que dependen de un agente (`rag_query`, `scheduling`) solo se ofrecen al
clasificador si `config.enabled_agents` los incluye, y si el modelo devuelve uno
que no esta disponible se reencamina:

- hay RAG habilitado -> `rag_query` (que puede terminar en handoff por falta de
  contexto, que es el comportamiento correcto);
- no hay RAG habilitado -> `human_request`, o sea, a un humano. Sin agente que
  conteste, inventar una respuesta seria peor que transferir.

Las excepciones del LLM NO se atrapan: suben a `app/tasks/ai_processor.py`, que
reintenta y, agotados los intentos, escala la conversacion a un humano. Atrapar
aqui convertiria un fallo de OpenAI en una respuesta generica al contacto.
"""

import logging
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.agents.nodes._llm import extract_usage, get_chat_model
from app.agents.nodes._state import ConversationState
from app.agents.nodes._tenant import get_agent_settings
from app.core.config import get_settings
from app.middleware.token_budget import TokenBudgetGuard

logger = logging.getLogger(__name__)

# Intents que no dependen de ningun agente: siempre disponibles.
BASE_INTENTS: tuple[str, ...] = (
    "greeting",
    "farewell",
    "human_request",
    "complaint",
    "unknown",
)

# Intents que solo existen si el tenant tiene habilitado el agente que los atiende.
AGENT_INTENTS: dict[str, str] = {
    "rag": "rag_query",
    "scheduling": "scheduling",
}

INTENT_DESCRIPTIONS: dict[str, str] = {
    "greeting": "Saludo o apertura de conversacion, sin pregunta concreta.",
    "farewell": "Despedida, agradecimiento de cierre o confirmacion final.",
    "rag_query": "Pregunta sobre el negocio: horarios, precios, servicios, politicas.",
    "scheduling": "Pide una cita, una reserva, o cambiar o cancelar una ya agendada.",
    "complaint": "Queja, reclamo o expresion clara de molestia con el servicio.",
    "human_request": "Pide explicitamente hablar con una persona o un agente humano.",
    "unknown": "No encaja con ninguna de las categorias anteriores.",
}

OPERATION = "intent_routing"


class IntentClassification(BaseModel):
    """Salida estructurada del clasificador de intents.

    Attributes:
        intent: Categoria detectada.
        confidence: Confianza de la clasificacion (0.0 - 1.0).
    """

    intent: Literal[
        "greeting",
        "farewell",
        "rag_query",
        "scheduling",
        "complaint",
        "human_request",
        "unknown",
    ] = Field(description="Intent detectado en el mensaje del usuario")
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confianza de la clasificacion, entre 0.0 y 1.0",
    )


def available_intents(enabled_agents: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Calcula los intents que se le ofrecen al clasificador para un tenant.

    Args:
        enabled_agents: Agentes habilitados del tenant.

    Returns:
        Intents base mas los de cada agente habilitado, en orden estable.
    """
    habilitados = set(enabled_agents)
    extra = tuple(intent for agente, intent in AGENT_INTENTS.items() if agente in habilitados)
    return BASE_INTENTS + extra


def build_system_prompt(intents: tuple[str, ...]) -> str:
    """Arma el prompt de clasificacion con las categorias disponibles.

    Args:
        intents: Intents que el clasificador puede devolver.

    Returns:
        Prompt de sistema listo para el LLM.
    """
    categorias = "\n".join(
        f"- {intent}: {INTENT_DESCRIPTIONS[intent]}"
        for intent in intents
        if intent in INTENT_DESCRIPTIONS
    )
    return (
        "Eres un clasificador de intents. Analiza el mensaje del usuario y "
        "clasifica su intencion en UNA de estas categorias:\n\n"
        f"{categorias}\n\n"
        "Si el usuario pide explicitamente hablar con un humano, usa 'human_request'.\n"
        "Si el mensaje es una queja o un reclamo, usa 'complaint'.\n"
        "Si no estas seguro, usa 'unknown'."
    )


def _unwrap(resultado: Any) -> tuple[IntentClassification | None, Any]:
    """Separa la clasificacion y la respuesta cruda del structured output.

    `with_structured_output(..., include_raw=True)` devuelve un dict con
    `parsed`/`raw`/`parsing_error`; sin `include_raw`, el modelo parseado a secas.
    Se aceptan ambas formas para no depender de la version de LangChain.

    Args:
        resultado: Lo que devolvio `ainvoke()`.

    Returns:
        Tupla `(clasificacion | None, mensaje_crudo | None)`.
    """
    if isinstance(resultado, dict):
        parsed = resultado.get("parsed")
        return (parsed if isinstance(parsed, IntentClassification) else None), resultado.get("raw")
    if isinstance(resultado, IntentClassification):
        return resultado, None
    return None, None


def _resolve(intent: str, disponibles: tuple[str, ...]) -> str:
    """Reencamina un intent que el tenant no tiene habilitado.

    Args:
        intent: Intent devuelto por el clasificador.
        disponibles: Intents habilitados para el tenant.

    Returns:
        El mismo intent si esta disponible; si no, `rag_query` cuando hay agente
        RAG, o `human_request` cuando no hay ningun agente que pueda atenderlo.
    """
    if intent in disponibles:
        if intent == "unknown" and "rag_query" not in disponibles:
            # `unknown` acaba en RAG por el routing del grafo; sin RAG, a humano.
            return "human_request"
        return intent

    destino = "rag_query" if "rag_query" in disponibles else "human_request"
    logger.info("Intent %s no habilitado para el tenant; se reencamina a %s", intent, destino)
    return destino


async def intent_routing_node(state: ConversationState) -> dict[str, Any]:
    """Clasifica el intent del mensaje con structured output.

    Args:
        state: Estado del grafo; usa `client_id`, `conversation_id` y `message`.

    Returns:
        Dict parcial con `intent` e `intent_confidence`.
    """
    client_id = state["client_id"]
    mensaje = state.get("message") or {}
    texto = (mensaje.get("text") or "").strip()

    settings = await get_agent_settings(UUID(client_id))
    disponibles = available_intents(settings.enabled_agents)

    if not texto:
        # Mensajes de solo media (imagen, audio) llegan sin texto: no hay nada
        # que clasificar y gastar una llamada al LLM no lo cambia.
        logger.info("Mensaje sin texto en %s; intent=unknown sin invocar al LLM", client_id)
        return {"intent": _resolve("unknown", disponibles), "intent_confidence": 0.0}

    modelo = get_settings().OPENAI_FALLBACK_MODEL
    llm = get_chat_model(modelo, temperature=0.0)
    structured = llm.with_structured_output(IntentClassification, include_raw=True)

    resultado = await structured.ainvoke(
        [
            {"role": "system", "content": build_system_prompt(disponibles)},
            {"role": "user", "content": texto},
        ]
    )
    clasificacion, crudo = _unwrap(resultado)

    if clasificacion is None:
        logger.warning("El clasificador no devolvio un intent parseable para %s", client_id)
        return {"intent": _resolve("unknown", disponibles), "intent_confidence": 0.0}

    if crudo is not None:
        prompt_tokens, completion_tokens = extract_usage(crudo)
        await TokenBudgetGuard.record_usage(
            client_id=client_id,
            conversation_id=state.get("conversation_id"),
            model=modelo,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            operation=OPERATION,
        )

    intent = _resolve(clasificacion.intent, disponibles)
    logger.info(
        "Intent detectado para %s: %s (confianza %.2f)",
        state.get("conversation_id"),
        intent,
        clasificacion.confidence,
    )
    return {"intent": intent, "intent_confidence": clasificacion.confidence}
