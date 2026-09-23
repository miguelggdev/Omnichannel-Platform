"""Envio de mensajes salientes y su persistencia, compartido por tres nodos.

`respond`, `human_handoff` y `training_approval` terminan haciendo lo mismo:
resolver a donde escribir, enviar por el `MessagingProvider` del canal y dejar el
mensaje en `messages` para que el historial de la conversacion no tenga huecos.
La spec repite ese bloque en cada nodo; aqui vive una sola vez.

Orden deliberado: primero se envia, despues se guarda. Si el proveedor falla, la
excepcion sube hasta la tarea de Celery (que reintenta) y no queda en la base un
mensaje que el contacto nunca recibio. El precio es el inverso: si el envio sale
bien y el commit falla, el reintento puede enviar dos veces. Se elige ese lado
porque un duplicado es visible y recuperable, y un mensaje fantasma en el
historial no.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from app.agents.nodes._tenant import get_channel_config, get_contact_identifier
from app.core.database import tenant_session
from app.core.events import EVENT_MESSAGE_SENT, EventEmitter
from app.core.metrics import record_message
from app.models.conversation import Conversation
from app.models.message import Message
from app.services.messaging.base import MessageContent
from app.services.messaging.factory import get_messaging_provider

logger = logging.getLogger(__name__)


async def _contexto_de_respuesta(
    client_id: UUID, conversation_id: UUID, channel: str
) -> dict[str, Any] | None:
    """Datos del canal que hacen falta para responder en el mismo hilo.

    Hoy solo email: un correo sin `In-Reply-To`/`References` correctos abre un
    hilo nuevo en el cliente de correo del usuario (spec Sprint 9, notas
    tecnicas). Se toman del ultimo mensaje **entrante** de la conversacion — el
    que el cliente acaba de mandar — y no del asunto, que uniria conversaciones
    ajenas con el mismo "Re: consulta".

    Args:
        client_id: Tenant propietario.
        conversation_id: Conversacion a la que se responde.
        channel: Canal por el que se responde.

    Returns:
        `subject`, `in_reply_to` y `references`, o `None` si el canal no lo usa
        o no hay un mensaje entrante del que colgarse.
    """
    if channel != "email":
        return None

    async with tenant_session(client_id) as session:
        metadata = (
            await session.execute(
                select(Message.metadata_)
                .where(
                    Message.client_id == client_id,
                    Message.conversation_id == conversation_id,
                    Message.direction == "inbound",
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if not metadata or not metadata.get("message_id"):
        return None
    referencias = " ".join(
        parte for parte in (metadata.get("references"), metadata["message_id"]) if parte
    )
    return {
        "subject": metadata.get("subject"),
        "in_reply_to": metadata["message_id"],
        "references": referencias,
    }


async def deliver_message(
    client_id: UUID,
    conversation_id: UUID,
    contact_id: UUID,
    channel: str,
    text: str,
    metadata: dict[str, Any] | None = None,
) -> str | None:
    """Envia un texto al contacto y lo registra como mensaje saliente.

    Args:
        client_id: Tenant propietario.
        conversation_id: Conversacion a la que pertenece el mensaje.
        contact_id: Contacto destinatario.
        channel: Canal por el que responder.
        text: Cuerpo del mensaje.
        metadata: Datos propios del canal para este envio (por ejemplo, el teclado
            `request_contact` de Telegram). Se suman al contexto de hilo de email,
            y el llamador manda si coinciden.

    Returns:
        Id externo que devolvio el proveedor, o None si no devolvio ninguno.

    Raises:
        ContactIdentifierNotFoundError: Si el contacto no tiene identificador en
            el canal.
        ChannelNotConfiguredError: Si el canal no tiene proveedor o credenciales.
    """
    identifier = await get_contact_identifier(client_id, contact_id, channel)
    provider_name, channel_config = get_channel_config(channel)
    provider = get_messaging_provider(provider_name, {"channel": channel})

    contexto = await _contexto_de_respuesta(client_id, conversation_id, channel)
    if metadata:
        contexto = {**(contexto or {}), **metadata}

    external_id = await provider.send_message(
        to=identifier,
        content=MessageContent(text=text, metadata=contexto),
        channel_config=channel_config,
    )

    message_id = uuid4()

    async with tenant_session(client_id) as session:
        session.add(
            Message(
                id=message_id,
                client_id=client_id,
                conversation_id=conversation_id,
                direction="outbound",
                message_type="text",
                content=text,
                external_message_id=external_id or None,
                sender_type="bot",
                sender_id=None,
            )
        )
        conversation = (
            await session.execute(
                select(Conversation).where(
                    Conversation.id == conversation_id, Conversation.client_id == client_id
                )
            )
        ).scalar_one_or_none()
        if conversation is not None:
            conversation.last_message_at = datetime.now(timezone.utc)

    record_message(str(client_id), channel, "outbound")

    # Despues del commit, como las metricas: un mensaje que no llego a
    # persistirse no es un mensaje del historial.
    await EventEmitter.emit(
        EVENT_MESSAGE_SENT,
        client_id,
        {
            "message_id": str(message_id),
            "conversation_id": str(conversation_id),
            "contact_id": str(contact_id),
            "channel": channel,
            "content": text,
            "direction": "outgoing",
            "external_message_id": external_id or None,
        },
    )

    logger.info(
        "Mensaje saliente enviado por %s: conversation_id=%s external_id=%s",
        provider_name,
        conversation_id,
        external_id,
    )
    return external_id or None
