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

from sqlalchemy import select

from app.agents.nodes._delivery import deliver_message
from app.agents.nodes._notifications import enqueue_notification
from app.agents.nodes._state import ConversationState
from app.agents.nodes._tenant import get_agent_settings
from app.core.database import tenant_session
from app.core.metrics import record_handoff
from app.models.conversation import Conversation
from app.services.i18n import SYSTEM_MESSAGES, get_system_message, normalizar_idioma

logger = logging.getLogger(__name__)

HANDOFF_STATUS = "waiting_human"

#: Motivos de handoff con texto propio. Los textos viven en `app/services/i18n.py`
#: (`handoff_<motivo>`), una sola copia para los seis idiomas.
HANDOFF_REASONS: tuple[str, ...] = (
    "insufficient_context",
    "budget_exceeded",
    "human_request",
    "complaint",
    "transcription_failed",
    "negative_sentiment",
    "scheduling_unavailable",
)

#: El texto en espanol de cada motivo (el de referencia y el de rescate).
HANDOFF_MESSAGES: dict[str, str] = {
    motivo: SYSTEM_MESSAGES["es"][f"handoff_{motivo}"] for motivo in HANDOFF_REASONS
}

DEFAULT_HANDOFF_REASON = "insufficient_context"


def _handoff_text(reason: str, configurado: str | None, idioma: str | None = None) -> str:
    """Elige el texto que se le envia al contacto.

    El mensaje que el tenant configuro manda siempre: es su voz, en el idioma
    que el haya escrito. Sin el, se usa el del motivo en el idioma del contacto;
    un motivo desconocido cae al de `DEFAULT_HANDOFF_REASON`.

    Args:
        reason: Motivo del handoff.
        configurado: `agent_configs.handoff_message` del tenant, si lo definio.
        idioma: Idioma detectado del contacto; `None` o no soportado es espanol.

    Returns:
        Mensaje de transferencia.
    """
    if configurado:
        return configurado
    motivo = reason if reason in HANDOFF_MESSAGES else DEFAULT_HANDOFF_REASON
    return get_system_message(f"handoff_{motivo}", idioma)


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
    if state.get("partial_results"):
        # BUG-024: si un nodo con varias tool-calls en el mismo turno (ej.
        # scheduling_node) ya ejecuto alguna con exito antes de escalar, que
        # quede visible aca -- el agente humano necesita saber que una accion
        # (ej. una cita) ya se concreto antes de retomar la conversacion.
        registro["partial_results"] = state["partial_results"]
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
    idioma = state.get("detected_language")

    async with tenant_session(client_id) as session:
        conversation = (
            await session.execute(
                select(Conversation).where(
                    Conversation.id == conversation_id, Conversation.client_id == client_id
                )
            )
        ).scalar_one_or_none()
        if conversation is None:
            logger.error("Conversacion %s inexistente al escalar a humano", conversation_id)
        else:
            conversation.status = HANDOFF_STATUS
            # Reasignar el dict entero: SQLAlchemy no detecta mutaciones in-place
            # de un JSONB sin MutableDict.
            conversation.metadata_ = {**(conversation.metadata_ or {}), "handoff": registro}
            # Con el presupuesto agotado el grafo llega aqui sin pasar por
            # `language_detect`, pero el idioma de la conversacion ya esta guardado.
            idioma = idioma or normalizar_idioma(
                (conversation.metadata_ or {}).get("detected_language")
            )

    settings = await get_agent_settings(client_id)
    texto = _handoff_text(reason, settings.handoff_message, idioma)

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

    record_handoff(str(client_id), reason)
    logger.info("Conversacion %s escalada a humano (motivo: %s)", conversation_id, reason)
    return {
        "requires_handoff": True,
        "handoff_reason": reason,
        "response_text": texto,
    }
