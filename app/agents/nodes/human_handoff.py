"""Nodo de escalacion a un agente humano.

Contrato: `specs/sprint-06-langgraph.md` §9. Es terminal: despues de este nodo el
grafo termina y la conversacion queda en manos de una persona.

Hace cuatro cosas, en este orden:

1. Pasa la conversacion a `waiting_human`.
2. Deja el motivo y las metricas del handoff en `conversations.metadata.handoff`.
3. Avisa al contacto de la transferencia (y guarda ese mensaje en el historial).
4. Encola el aviso al equipo humano, si el modulo de notificaciones ya existe.

Desviacion sobre la spec: la spec crea una `InternalNote` con el motivo, pero
`internal_notes.author_id` es NOT NULL y apunta a `users` — una nota generada por
el bot no tiene autor, y firmar con un admin cualquiera seria atribuirle algo que
no escribio. Hasta que exista un usuario de sistema por tenant (o la columna sea
nullable), el motivo y las metricas van a `conversations.metadata.handoff`, que
es igual de consultable y no falsea el dato. Registrado en MEMORY.md.

El orden tambien es deliberado: primero se marca `waiting_human`, despues se
avisa. Si el envio al contacto falla, la conversacion ya esta en la bandeja del
equipo; al reves, el contacto sabria de una transferencia que no ocurrio.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.agents.nodes._delivery import deliver_message
from app.agents.nodes._notifications import enqueue_notification
from app.agents.nodes._state import ConversationState
from app.agents.nodes._tenant import get_agent_settings
from app.core.database import tenant_session
from app.models.conversation import Conversation

logger = logging.getLogger(__name__)

HANDOFF_STATUS = "waiting_human"

HANDOFF_MESSAGES: dict[str, str] = {
    "insufficient_context": (
        "No tengo suficiente informacion para responder tu consulta. Te estoy "
        "transfiriendo con un agente humano que podra ayudarte mejor."
    ),
    "budget_exceeded": (
        "Te estoy transfiriendo con un agente humano para atenderte personalmente."
    ),
    "human_request": "Entendido, te transfiero con un agente humano ahora mismo.",
    "complaint": (
        "Lamento la situacion. Te transfiero con un agente especializado para resolver tu caso."
    ),
}

DEFAULT_HANDOFF_REASON = "insufficient_context"


def _handoff_text(reason: str, configurado: str | None) -> str:
    """Elige el texto que se le envia al contacto.

    Args:
        reason: Motivo del handoff.
        configurado: `agent_configs.handoff_message` del tenant, si lo definio.

    Returns:
        Mensaje de transferencia.
    """
    if configurado:
        return configurado
    return HANDOFF_MESSAGES.get(reason, HANDOFF_MESSAGES[DEFAULT_HANDOFF_REASON])


def _handoff_metadata(state: ConversationState, reason: str) -> dict[str, Any]:
    """Arma el registro del handoff que se guarda en la conversacion.

    Args:
        state: Estado del grafo.
        reason: Motivo del handoff.

    Returns:
        Dict con motivo, metricas disponibles y momento del handoff.
    """
    registro: dict[str, Any] = {
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
        "intent": state.get("intent"),
    }
    if state.get("rag_confidence") is not None:
        registro["rag_confidence"] = round(float(state["rag_confidence"] or 0.0), 4)
    if state.get("budget_usage_pct") is not None:
        registro["budget_usage_pct"] = round(float(state.get("budget_usage_pct") or 0.0), 2)
    return registro


async def human_handoff_node(state: ConversationState) -> dict[str, Any]:
    """Escala la conversacion a un agente humano.

    Args:
        state: Estado del grafo; usa `client_id`, `conversation_id`,
            `contact_id`, `channel` y `handoff_reason`.

    Returns:
        Dict parcial con `requires_handoff`, `handoff_reason` y el texto enviado.
    """
    client_id = UUID(state["client_id"])
    conversation_id = UUID(state["conversation_id"])
    contact_id = UUID(state["contact_id"])
    channel = state["channel"]
    reason = state.get("handoff_reason") or DEFAULT_HANDOFF_REASON

    registro = _handoff_metadata(state, reason)

    async with tenant_session(client_id) as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            logger.error("Conversacion %s inexistente al escalar a humano", conversation_id)
        else:
            conversation.status = HANDOFF_STATUS
            # Reasignar el dict entero: SQLAlchemy no detecta mutaciones in-place
            # de un JSONB sin MutableDict.
            conversation.metadata_ = {**(conversation.metadata_ or {}), "handoff": registro}

    settings = await get_agent_settings(client_id)
    texto = _handoff_text(reason, settings.handoff_message)

    await deliver_message(
        client_id=client_id,
        conversation_id=conversation_id,
        contact_id=contact_id,
        channel=channel,
        text=texto,
    )

    enqueue_notification(
        "notify_handoff",
        client_id=str(client_id),
        conversation_id=str(conversation_id),
        reason=reason,
    )

    logger.info("Conversacion %s escalada a humano (motivo: %s)", conversation_id, reason)
    return {
        "requires_handoff": True,
        "handoff_reason": reason,
        "response_text": texto,
    }
