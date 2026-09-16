"""Nodo de envio de la respuesta al contacto.

Contrato: `specs/sprint-06-langgraph.md` §8.

No invoca al LLM: la respuesta viene generada por `rag_query`. Su unico trabajo
extra es cubrir los intents que se contestan sin modelo (`greeting`, `farewell`)
y los casos en que el grafo llega hasta aqui sin texto.

El saludo usa `agent_configs.welcome_message` si el tenant configuro uno; es la
personalizacion mas visible del bot y no tiene sentido ignorarla.
"""

import logging
from typing import Any
from uuid import UUID

from app.agents.nodes._delivery import deliver_message
from app.agents.nodes._state import ConversationState
from app.agents.nodes._tenant import get_agent_settings

logger = logging.getLogger(__name__)

DEFAULT_GREETING = "Hola! En que puedo ayudarte hoy?"
DEFAULT_FAREWELL = "Hasta luego! Si necesitas algo mas, no dudes en escribirme."
DEFAULT_FALLBACK = "Disculpa, no entendi tu mensaje. Podrias reformularlo?"


async def _texto_por_intent(client_id: UUID, intent: str | None) -> str:
    """Devuelve la respuesta fija que corresponde a un intent sin generacion.

    Args:
        client_id: Tenant propietario, para leer su mensaje de bienvenida.
        intent: Intent detectado.

    Returns:
        Texto a enviar.
    """
    if intent == "greeting":
        settings = await get_agent_settings(client_id)
        return settings.welcome_message or DEFAULT_GREETING
    if intent == "farewell":
        return DEFAULT_FAREWELL
    return DEFAULT_FALLBACK


async def respond_node(state: ConversationState) -> dict[str, Any]:
    """Envia la respuesta al contacto y la registra en la conversacion.

    Args:
        state: Estado del grafo; usa `client_id`, `conversation_id`,
            `contact_id`, `channel`, `intent` y `response_text`.

    Returns:
        Dict parcial con el `response_text` realmente enviado.
    """
    client_id = UUID(state["client_id"])
    conversation_id = UUID(state["conversation_id"])
    contact_id = UUID(state["contact_id"])
    channel = state["channel"]

    texto = (state.get("response_text") or "").strip()
    if not texto:
        texto = await _texto_por_intent(client_id, state.get("intent"))

    await deliver_message(
        client_id=client_id,
        conversation_id=conversation_id,
        contact_id=contact_id,
        channel=channel,
        text=texto,
    )
    logger.info("Respuesta enviada en la conversacion %s", conversation_id)
    return {"response_text": texto}
