"""Endpoints de administracion: cumplimiento de RGPD sobre un contacto.

GET    /api/v1/admin/contacts/{id}/export       todos sus datos personales en JSON
DELETE /api/v1/admin/contacts/{id}/gdpr-delete  anonimiza esos datos

Por que anonimizar y no borrar (spec §10.2)
-------------------------------------------
Borrar la fila del contacto arrastraria sus conversaciones y mensajes por las
FK, y con ellos el historico agregado del tenant (cuantas conversaciones hubo,
cuantas se resolvieron). El derecho de supresion se cumple igual quitando los
datos personales y dejando el esqueleto sin identificar.

Las dos operaciones quedan registradas en `audit_logs` sin que este modulo haga
nada: los UPDATE sobre `contacts`, `messages` y `conversations` los captura el
trigger de la migracion 006, con el usuario que los pidio, que
`AuditContextMiddleware` publica en `app.current_user_id`.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import NOT_FOUND, VALIDATION_ERROR, AppException
from app.models.contact import Contact
from app.models.contact_identifier import ContactIdentifier
from app.models.contact_tag import ContactTag
from app.models.conversation import Conversation
from app.models.internal_note import InternalNote
from app.models.message import Message
from app.models.tag import Tag

logger = logging.getLogger(__name__)

router = APIRouter()

_GDPR_ROLES = ("super_admin", "admin")

# Texto con el que se reemplazan los datos personales.
ANONIMIZADO = "[ELIMINADO]"
CONTENIDO_ANONIMIZADO = "[CONTENIDO ELIMINADO POR SOLICITUD RGPD]"


async def _get_contact_or_404(session: AsyncSession, contact_id: UUID, client_id: UUID) -> Contact:
    """Carga un contacto del tenant activo o levanta 404.

    Args:
        session: Sesion con contexto de tenant.
        contact_id: Contacto buscado.
        client_id: Tenant propietario.

    Returns:
        El contacto.

    Raises:
        AppException: 404 si no existe para este tenant.
    """
    contact = (
        await session.execute(
            select(Contact).where(Contact.id == contact_id, Contact.client_id == client_id)
        )
    ).scalar_one_or_none()
    if contact is None:
        raise AppException(
            status_code=404,
            error_code=NOT_FOUND,
            message="Contacto no encontrado",
        )
    return contact


@router.get("/contacts/{contact_id}/export")
async def export_contact_data(
    contact_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_GDPR_ROLES)),
) -> dict[str, Any]:
    """Exporta en JSON todo lo que la plataforma guarda sobre un contacto.

    Cubre el derecho de acceso y portabilidad: contacto, identificadores por
    canal, etiquetas, notas internas y todas sus conversaciones con sus mensajes.

    Se devuelve entero, sin paginar, porque un export parcial no cumple el
    proposito. Las conversaciones de un contacto son decenas, no millones.

    Args:
        contact_id: Contacto a exportar.
        user: Usuario autenticado; solo admin y super_admin.

    Returns:
        Estructura JSON con todos los datos asociados.

    Raises:
        AppException: 404 si el contacto no existe para este tenant.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        contact = await _get_contact_or_404(session, contact_id, client_id)

        identificadores = (
            (
                await session.execute(
                    select(ContactIdentifier).where(
                        ContactIdentifier.client_id == client_id,
                        ContactIdentifier.contact_id == contact_id,
                    )
                )
            )
            .scalars()
            .all()
        )

        etiquetas = (
            await session.execute(
                select(Tag.name)
                .join(ContactTag, ContactTag.tag_id == Tag.id)
                .where(
                    ContactTag.client_id == client_id,
                    ContactTag.contact_id == contact_id,
                )
                .order_by(Tag.name.asc())
            )
        ).all()

        notas = (
            (
                await session.execute(
                    select(InternalNote)
                    .where(
                        InternalNote.client_id == client_id,
                        InternalNote.contact_id == contact_id,
                    )
                    .order_by(InternalNote.created_at.asc())
                )
            )
            .scalars()
            .all()
        )

        conversaciones = (
            (
                await session.execute(
                    select(Conversation)
                    .where(
                        Conversation.client_id == client_id,
                        Conversation.contact_id == contact_id,
                    )
                    .order_by(Conversation.created_at.asc())
                )
            )
            .scalars()
            .all()
        )

        # Todos los mensajes de una vez y agrupados en memoria: una consulta por
        # conversacion seria N+1 sobre la tabla mas grande del sistema.
        ids_conversaciones = [c.id for c in conversaciones]
        mensajes_por_conversacion: dict[UUID, list[Message]] = {
            cid: [] for cid in ids_conversaciones
        }
        if ids_conversaciones:
            mensajes = (
                (
                    await session.execute(
                        select(Message)
                        .where(
                            Message.client_id == client_id,
                            Message.conversation_id.in_(ids_conversaciones),
                        )
                        .order_by(Message.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
            for mensaje in mensajes:
                mensajes_por_conversacion[mensaje.conversation_id].append(mensaje)

        return {
            "export_date": datetime.now(timezone.utc).isoformat(),
            "contact": {
                "id": str(contact.id),
                "first_name": contact.first_name,
                "last_name": contact.last_name,
                "display_name": contact.display_name,
                "metadata": contact.metadata_ or {},
                "is_gdpr_deleted": contact.is_gdpr_deleted,
                "gdpr_deleted_at": (
                    contact.gdpr_deleted_at.isoformat() if contact.gdpr_deleted_at else None
                ),
                "created_at": contact.created_at.isoformat(),
            },
            "identifiers": [
                {
                    "id": str(i.id),
                    "channel": i.channel,
                    "identifier_value": i.identifier_value,
                    "created_at": i.created_at.isoformat(),
                }
                for i in identificadores
            ],
            "tags": [fila.name for fila in etiquetas],
            "notes": [
                {
                    "id": str(n.id),
                    "content": n.content,
                    "author_id": str(n.author_id),
                    "created_at": n.created_at.isoformat(),
                }
                for n in notas
            ],
            "conversations": [
                {
                    "id": str(c.id),
                    "channel": c.channel,
                    "status": c.status,
                    "subject": c.subject,
                    "created_at": c.created_at.isoformat(),
                    "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
                    "messages": [
                        {
                            "id": str(m.id),
                            "direction": m.direction,
                            "message_type": m.message_type,
                            "content": m.content,
                            "media_url": m.media_url,
                            "sender_type": m.sender_type,
                            "created_at": m.created_at.isoformat(),
                        }
                        for m in mensajes_por_conversacion[c.id]
                    ],
                }
                for c in conversaciones
            ],
        }


@router.delete("/contacts/{contact_id}/gdpr-delete")
async def gdpr_delete_contact(
    contact_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_GDPR_ROLES)),
) -> dict[str, Any]:
    """Anonimiza los datos personales de un contacto, sin borrar las filas.

    Qué se anonimiza:
      - nombre, apellido, display_name y `metadata` del contacto;
      - el valor de cada identificador de canal (telefono, PSID, usuario);
      - el contenido y el media de los mensajes **entrantes**, que son las
        palabras del propio contacto;
      - las notas internas que el equipo escribio sobre el.

    Qué NO se anonimiza, a proposito: los mensajes salientes. Son el registro de
    lo que la empresa respondio, no datos aportados por el contacto, y borrarlos
    destruiria la trazabilidad de la atencion. Si en un caso concreto una
    respuesta contiene datos personales, se edita ese mensaje aparte.

    La operacion es idempotente: repetirla sobre un contacto ya anonimizado
    responde 400, para que un doble clic no reescriba `gdpr_deleted_at` y se
    pierda la fecha real de la supresion.

    Args:
        contact_id: Contacto a anonimizar.
        user: Usuario autenticado; solo admin y super_admin.

    Returns:
        Resumen de cuantas filas se tocaron.

    Raises:
        AppException: 404 si no existe, 400 si ya estaba anonimizado.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        contact = await _get_contact_or_404(session, contact_id, client_id)

        if contact.is_gdpr_deleted:
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message=(
                    "El contacto ya fue anonimizado el "
                    f"{contact.gdpr_deleted_at.isoformat() if contact.gdpr_deleted_at else '?'}"
                ),
            )

        ahora = datetime.now(timezone.utc)

        contact.first_name = ANONIMIZADO
        contact.last_name = ANONIMIZADO
        contact.display_name = ANONIMIZADO
        contact.metadata_ = {}
        contact.is_gdpr_deleted = True
        contact.gdpr_deleted_at = ahora

        identificadores = (
            (
                await session.execute(
                    select(ContactIdentifier).where(
                        ContactIdentifier.client_id == client_id,
                        ContactIdentifier.contact_id == contact_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for identificador in identificadores:
            # El sufijo con el id mantiene unico el valor: la tabla tiene
            # unique(client_id, channel, identifier_value) y un contacto puede
            # tener dos identificadores del mismo canal.
            identificador.identifier_value = f"[ELIMINADO-{identificador.id.hex[:8]}]"

        conversaciones = (
            (
                await session.execute(
                    select(Conversation.id).where(
                        Conversation.client_id == client_id,
                        Conversation.contact_id == contact_id,
                    )
                )
            )
            .scalars()
            .all()
        )

        mensajes_anonimizados = 0
        if conversaciones:
            mensajes = (
                (
                    await session.execute(
                        select(Message).where(
                            Message.client_id == client_id,
                            Message.conversation_id.in_(conversaciones),
                            Message.direction == "inbound",
                        )
                    )
                )
                .scalars()
                .all()
            )
            for mensaje in mensajes:
                mensaje.content = CONTENIDO_ANONIMIZADO
                mensaje.media_url = None
                mensaje.metadata_ = {}
                mensajes_anonimizados += 1

        notas = (
            (
                await session.execute(
                    select(InternalNote).where(
                        InternalNote.client_id == client_id,
                        InternalNote.contact_id == contact_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for nota in notas:
            nota.content = CONTENIDO_ANONIMIZADO

        await session.flush()

    logger.info(
        "RGPD: contacto %s anonimizado por %s (tenant %s)",
        contact_id,
        user["user_id"],
        client_id,
    )
    return {
        "status": "success",
        "message": "Datos del contacto anonimizados",
        "contact_id": str(contact_id),
        "gdpr_deleted_at": ahora.isoformat(),
        "identifiers_anonymized": len(identificadores),
        "messages_anonymized": mensajes_anonimizados,
        "notes_anonymized": len(notas),
    }
