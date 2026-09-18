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
from uuid import UUID

from app.agents.nodes._tenant import get_channel_config, get_contact_identifier
from app.core.database import tenant_session
from app.core.metrics import record_message
from app.models.conversation import Conversation
from app.models.message import Message
from app.services.messaging.base import MessageContent
from app.services.messaging.factory import get_messaging_provider

logger = logging.getLogger(__name__)


async def deliver_message(
    client_id: UUID,
    conversation_id: UUID,
    contact_id: UUID,
    channel: str,
    text: str,
) -> str | None:
    """Envia un texto al contacto y lo registra como mensaje saliente.

    Args:
        client_id: Tenant propietario.
        conversation_id: Conversacion a la que pertenece el mensaje.
        contact_id: Contacto destinatario.
        channel: Canal por el que responder.
        text: Cuerpo del mensaje.

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

    external_id = await provider.send_message(
        to=identifier,
        content=MessageContent(text=text),
        channel_config=channel_config,
    )

    async with tenant_session(client_id) as session:
        session.add(
            Message(
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
        conversation = await session.get(Conversation, conversation_id)
        if conversation is not None:
            conversation.last_message_at = datetime.now(timezone.utc)

    record_message(str(client_id), channel, "outbound")
    logger.info(
        "Mensaje saliente enviado por %s: conversation_id=%s external_id=%s",
        provider_name,
        conversation_id,
        external_id,
    )
    return external_id or None
