"""Nodo de modo entrenamiento (ADR-005).

Contrato: `specs/sprint-06-langgraph.md` §10. Es terminal.

Cuando el tenant tiene `agent_configs.training_mode` activo, la respuesta que
genero el RAG **no** se envia: queda en `pending_responses` con estado `pending`
para que un supervisor la apruebe. Al contacto se le avisa que su consulta esta
en revision, para que no quede esperando en silencio.

La respuesta aprobada pasa despues a `approved_responses` (con su embedding) y
vuelve al grafo como few-shot: ese circuito es de la API de aprobacion, no de
este nodo.

Desviacion sobre la spec: la columna es `pending_responses.generated_response`,
no `suggested_answer`.
"""

import logging
from typing import Any
from uuid import UUID

from app.agents.nodes._delivery import deliver_message
from app.agents.nodes._notifications import enqueue_notification
from app.agents.nodes._state import ConversationState
from app.core.database import tenant_session
from app.models.pending_response import PendingResponse

logger = logging.getLogger(__name__)

WAITING_MESSAGE = "Tu consulta esta siendo procesada. Un agente revisara la respuesta en breve."


async def training_approval_node(state: ConversationState) -> dict[str, Any]:
    """Guarda la respuesta candidata para revision humana en vez de enviarla.

    Args:
        state: Estado del grafo; usa `client_id`, `conversation_id`,
            `contact_id`, `channel`, `message` y `response_text`.

    Returns:
        Dict parcial con `response_text=None` (no se envio la respuesta real) y
        `training_mode=True`.
    """
    client_id = UUID(state["client_id"])
    conversation_id = UUID(state["conversation_id"])
    contact_id = UUID(state["contact_id"])
    channel = state["channel"]
    pregunta = (state.get("message") or {}).get("text") or ""
    respuesta = (state.get("response_text") or "").strip()

    if not respuesta:
        # Llegar aqui sin texto significa que el grafo enruto mal: el nodo de RAG
        # escala a humano cuando no puede responder. Se deja constancia igual
        # para que el supervisor vea la pregunta sin respuesta.
        logger.warning(
            "Modo entrenamiento sin respuesta generada en la conversacion %s", conversation_id
        )

    async with tenant_session(client_id) as session:
        pending = PendingResponse(
            client_id=client_id,
            conversation_id=conversation_id,
            question=pregunta,
            generated_response=respuesta,
            status="pending",
        )
        session.add(pending)
        await session.flush()  # necesitamos pending.id para la notificacion
        pending_id = str(pending.id)

    await deliver_message(
        client_id=client_id,
        conversation_id=conversation_id,
        contact_id=contact_id,
        channel=channel,
        text=WAITING_MESSAGE,
    )

    enqueue_notification(
        "notify_pending_response",
        client_id=str(client_id),
        conversation_id=str(conversation_id),
        pending_response_id=pending_id,
    )

    logger.info(
        "Respuesta retenida para aprobacion (pending_response=%s, conversacion=%s)",
        pending_id,
        conversation_id,
    )
    return {"response_text": None, "training_mode": True}
