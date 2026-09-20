"""Mensajes que un visitante de Webchat se perdio mientras estuvo desconectado.

La entrega en vivo es un publish de Redis: si el visitante no estaba suscrito (cerro
la pestana, perdio la red, recargo la pagina) el mensaje no llega por ahi. Pero
`deliver_message()` lo guarda siempre en `messages`, asi que la base es la fuente de
verdad y esto es lo que se lee al reconectar.

Todo va acotado al tenant y a las conversaciones del **contacto de ese visitante**:
el `visitor_id` que llega ya esta autenticado (sesion firmada), pero aqui no se
confia en el para nada mas que para encontrar su identificador cifrado por el indice
ciego del tenant.
"""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from app.core.database import tenant_session
from app.core.encryption import blind_index
from app.models.contact_identifier import ContactIdentifier
from app.models.conversation import Conversation
from app.models.message import Message

if TYPE_CHECKING:
    from datetime import datetime

WEBCHAT_CHANNEL = "webchat"


def _frame(mensaje: Message) -> dict[str, Any]:
    """Convierte un mensaje saliente guardado en el frame que ve el cliente.

    Args:
        mensaje: Fila de `messages`.

    Returns:
        Frame `message`. El `message_id` es el que el cliente ya conoce por la
        entrega en vivo (`external_message_id`), para que pueda deduplicar.
    """
    return {
        "type": "message",
        "message_id": mensaje.external_message_id or str(mensaje.id),
        "text": mensaje.content or "",
        "timestamp": mensaje.created_at.isoformat(),
    }


async def mensajes_perdidos(
    client_id: UUID,
    visitor_id: str,
    last_message_id: str | None,
    limite: int,
) -> list[dict[str, Any]]:
    """Mensajes salientes de las conversaciones de Webchat del visitante.

    Args:
        client_id: Tenant.
        visitor_id: Visitante ya autenticado.
        last_message_id: Ultimo `message_id` que el cliente recibio. Si se conoce,
            se devuelve lo posterior; si no (visitante nuevo, o un id que no
            pertenece a este visitante) se devuelven los ultimos `limite`.
        limite: Maximo de mensajes a devolver.

    Returns:
        Frames `message` en orden cronologico. Vacio si el visitante aun no tiene
        contacto ni conversaciones.
    """
    async with tenant_session(client_id) as session:
        contact_id = (
            await session.execute(
                select(ContactIdentifier.contact_id).where(
                    ContactIdentifier.client_id == client_id,
                    ContactIdentifier.channel == WEBCHAT_CHANNEL,
                    ContactIdentifier.identifier_hash == blind_index(visitor_id, client_id),
                )
            )
        ).scalar_one_or_none()
        if contact_id is None:
            return []

        conversaciones = (
            (
                await session.execute(
                    select(Conversation.id).where(
                        Conversation.client_id == client_id,
                        Conversation.contact_id == contact_id,
                        Conversation.channel == WEBCHAT_CHANNEL,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not conversaciones:
            return []

        base = (
            Message.client_id == client_id,
            Message.conversation_id.in_(conversaciones),
            Message.direction == "outbound",
        )

        desde: datetime | None = None
        if last_message_id:
            # Se busca dentro de las conversaciones de ESTE visitante: un id ajeno
            # no da acceso a nada y simplemente cae en "ultimos N".
            desde = (
                await session.execute(
                    select(Message.created_at).where(
                        *base, Message.external_message_id == last_message_id
                    )
                )
            ).scalar_one_or_none()

        if desde is not None:
            filas = (
                (
                    await session.execute(
                        select(Message)
                        .where(*base, Message.created_at > desde)
                        .order_by(Message.created_at.asc())
                        .limit(limite)
                    )
                )
                .scalars()
                .all()
            )
        else:
            recientes = (
                (
                    await session.execute(
                        select(Message)
                        .where(*base)
                        .order_by(Message.created_at.desc())
                        .limit(limite)
                    )
                )
                .scalars()
                .all()
            )
            filas = list(reversed(recientes))

    return [_frame(m) for m in filas]
