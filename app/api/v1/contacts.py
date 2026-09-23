"""CRUD de contactos del CRM.

GET    /api/v1/contacts                        lista paginada, buscable y filtrable por tag
POST   /api/v1/contacts                        alta manual de un contacto
GET    /api/v1/contacts/{id}                   detalle con identificadores, tags y notas
PUT    /api/v1/contacts/{id}                   edicion de los datos de nombre y metadata
POST   /api/v1/contacts/{id}/merge/{target_id} fusiona dos contactos duplicados
GET    /api/v1/contacts/{id}/conversations     historial de conversaciones del contacto

Todas las consultas llevan el `client_id` explicito en el WHERE ademas de correr
bajo RLS, igual que en `documents.py`: CLAUDE.md (restriccion 2) pide que el
filtro de seguridad se vea en la query y no quede delegado solo a la politica.
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.events import EVENT_CONTACT_CREATED, EVENT_CONTACT_UPDATED, EventEmitter
from app.core.exceptions import NOT_FOUND, VALIDATION_ERROR, AppException
from app.models.contact import Contact
from app.models.contact_identifier import ContactIdentifier
from app.models.contact_tag import ContactTag
from app.models.conversation import Conversation
from app.models.internal_note import InternalNote
from app.models.tag import Tag
from app.schemas.contact import (
    ContactCreate,
    ContactDetailResponse,
    ContactListResponse,
    ContactNoteResponse,
    ContactResponse,
    ContactTagResponse,
    ContactUpdate,
    IdentifierResponse,
)
from app.schemas.conversation import ConversationListResponse, ConversationResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# Notas que se devuelven junto al detalle del contacto. El listado completo y
# paginado vive en GET /contacts/{id}/notes (notes.py).
DETAIL_NOTES_LIMIT = 20

UNIFIER_UNAVAILABLE = "UNIFIER_UNAVAILABLE"

_READ_ROLES = ("super_admin", "admin", "supervisor", "agent")
_WRITE_ROLES = ("super_admin", "admin", "supervisor", "agent")
_MERGE_ROLES = ("super_admin", "admin")


async def get_contact_or_404(session: AsyncSession, contact_id: UUID, client_id: UUID) -> Contact:
    """Carga un contacto del tenant activo o levanta 404.

    Lo usan tambien `notes.py` y `tags.py` para no repetir la comprobacion.

    Args:
        session: Sesion con contexto de tenant.
        contact_id: Contacto buscado.
        client_id: Tenant propietario.

    Returns:
        El contacto.

    Raises:
        AppException: 404 si no existe para este tenant.
    """
    stmt = select(Contact).where(
        Contact.id == contact_id,
        Contact.client_id == client_id,
    )
    contact = (await session.execute(stmt)).scalar_one_or_none()
    if contact is None:
        raise AppException(
            status_code=404,
            error_code=NOT_FOUND,
            message="Contacto no encontrado",
        )
    return contact


@router.get("", response_model=ContactListResponse)
async def list_contacts(
    search: str | None = Query(default=None, description="Busca en nombre, apellido y display"),
    tag_id: UUID | None = Query(default=None, description="Solo contactos con esta etiqueta"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_role(*_READ_ROLES)),
) -> ContactListResponse:
    """Lista los contactos del tenant, sin incluir los ya fusionados.

    Un contacto con `merged_into_id` es un duplicado absorbido por otro: sigue en
    la tabla para que las conversaciones viejas no queden huerfanas, pero no debe
    aparecer en la agenda.

    Args:
        search: Texto a buscar en first_name, last_name y display_name (ILIKE).
        tag_id: Filtra por etiqueta asignada.
        page: Pagina, empezando en 1.
        page_size: Tamano de pagina, maximo 100.
        user: Usuario autenticado.

    Returns:
        Pagina de contactos y total de coincidencias.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        filtros = [
            Contact.client_id == client_id,
            Contact.merged_into_id.is_(None),
        ]

        if search:
            patron = f"%{search}%"
            filtros.append(
                or_(
                    Contact.first_name.ilike(patron),
                    Contact.last_name.ilike(patron),
                    Contact.display_name.ilike(patron),
                )
            )

        stmt = select(Contact).where(*filtros)
        count_stmt = select(func.count()).select_from(Contact).where(*filtros)

        if tag_id is not None:
            # El join va en las dos sentencias: si solo fuera en la de datos, el
            # total contaria contactos que la pagina no muestra.
            condicion = (ContactTag.contact_id == Contact.id) & (ContactTag.tag_id == tag_id)
            stmt = stmt.join(ContactTag, condicion)
            count_stmt = count_stmt.join(ContactTag, condicion)

        total = await session.scalar(count_stmt)

        stmt = (
            stmt.order_by(Contact.updated_at.desc()).offset((page - 1) * page_size).limit(page_size)
        )
        contactos = (await session.execute(stmt)).scalars().all()

        return ContactListResponse(
            items=[ContactResponse.model_validate(c) for c in contactos],
            total=int(total or 0),
            page=page,
            page_size=page_size,
        )


@router.post("", status_code=201, response_model=ContactResponse)
async def create_contact(
    data: ContactCreate,
    user: dict[str, Any] = Depends(require_role(*_WRITE_ROLES)),
) -> ContactResponse:
    """Da de alta un contacto a mano (los del webhook los crea el worker).

    Args:
        data: Nombre, apellido, display_name y metadata.
        user: Usuario autenticado; aporta el tenant.

    Returns:
        El contacto creado.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        contact = Contact(
            client_id=client_id,
            first_name=data.first_name,
            last_name=data.last_name,
            display_name=data.display_name,
            metadata_=data.metadata or {},
        )
        session.add(contact)
        await session.flush()
        # id, created_at y updated_at son server_default: sin refresh llegan a
        # None y ContactResponse falla al validar.
        await session.refresh(contact)
        respuesta = ContactResponse.model_validate(contact)

    logger.info("Contacto %s creado por tenant %s", respuesta.id, client_id)

    # Despues del commit: el evento anuncia un contacto que ya existe.
    await EventEmitter.emit(
        EVENT_CONTACT_CREATED,
        client_id,
        {"contact_id": str(respuesta.id), "channel": None},
    )
    return respuesta


@router.get("/{contact_id}", response_model=ContactDetailResponse)
async def get_contact(
    contact_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_READ_ROLES)),
) -> ContactDetailResponse:
    """Devuelve un contacto con sus identificadores, etiquetas y ultimas notas.

    Args:
        contact_id: Contacto a consultar.
        user: Usuario autenticado.

    Returns:
        El contacto con sus datos relacionados.

    Raises:
        AppException: 404 si no existe para este tenant.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        contact = await get_contact_or_404(session, contact_id, client_id)

        identificadores = (
            (
                await session.execute(
                    select(ContactIdentifier)
                    .where(
                        ContactIdentifier.client_id == client_id,
                        ContactIdentifier.contact_id == contact_id,
                    )
                    .order_by(ContactIdentifier.created_at.asc())
                )
            )
            .scalars()
            .all()
        )

        # Join contra tags para traer nombre y color en una sola consulta: con
        # lazy="select" en la relacion, tocar `contact_tag.tag` desde async
        # levantaria MissingGreenlet.
        etiquetas = (
            await session.execute(
                select(Tag.id, Tag.name, Tag.color)
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
                    .order_by(InternalNote.created_at.desc())
                    .limit(DETAIL_NOTES_LIMIT)
                )
            )
            .scalars()
            .all()
        )

        return ContactDetailResponse(
            **ContactResponse.model_validate(contact).model_dump(),
            metadata=contact.metadata_ or {},
            identifiers=[IdentifierResponse.model_validate(i) for i in identificadores],
            tags=[
                ContactTagResponse(id=fila.id, name=fila.name, color=fila.color)
                for fila in etiquetas
            ],
            notes=[ContactNoteResponse.model_validate(n) for n in notas],
        )


@router.put("/{contact_id}", response_model=ContactResponse)
async def update_contact(
    contact_id: UUID,
    data: ContactUpdate,
    user: dict[str, Any] = Depends(require_role(*_WRITE_ROLES)),
) -> ContactResponse:
    """Actualiza los campos enviados de un contacto.

    Solo se tocan los campos presentes en el body (`exclude_unset`): mandar
    `{"display_name": "X"}` no borra el resto.

    Args:
        contact_id: Contacto a actualizar.
        data: Campos a modificar.
        user: Usuario autenticado.

    Returns:
        El contacto ya actualizado.

    Raises:
        AppException: 404 si no existe, 400 si ya fue fusionado.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        contact = await get_contact_or_404(session, contact_id, client_id)

        if contact.merged_into_id is not None:
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message=(
                    f"El contacto ya fue fusionado; editar el destino ({contact.merged_into_id})"
                ),
            )

        cambios = data.model_dump(exclude_unset=True)
        # `metadata` es el alias publico de la columna `metadata_` del modelo.
        metadata_modificada = False
        if "metadata" in cambios:
            metadata = cambios.pop("metadata")
            if metadata is not None:
                contact.metadata_ = metadata
                metadata_modificada = True

        for campo, valor in cambios.items():
            setattr(contact, campo, valor)

        await session.flush()
        await session.refresh(contact)
        respuesta = ContactResponse.model_validate(contact)
        campos_modificados = sorted(cambios) + (["metadata"] if metadata_modificada else [])

    # Un PATCH sin campos no cambio nada: anunciarlo seria un evento falso.
    if campos_modificados:
        await EventEmitter.emit(
            EVENT_CONTACT_UPDATED,
            client_id,
            {"contact_id": str(contact_id), "updated_fields": campos_modificados},
        )
    return respuesta


@router.post("/{contact_id}/merge/{target_id}", response_model=ContactResponse)
async def merge_contacts(
    contact_id: UUID,
    target_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_MERGE_ROLES)),
) -> ContactResponse:
    """Fusiona el contacto origen dentro del destino.

    Mueve identificadores, conversaciones, notas y etiquetas al destino y marca
    el origen con `merged_into_id`. El trabajo real lo hace `ContactUnifier`
    (`app/services/contact_unifier.py`, entrega de Dev A): aqui solo se validan
    los dos contactos y los permisos.

    El import del unificador es perezoso a proposito. Mientras Dev A no lo
    entregue, este endpoint responde 503 con un motivo legible en vez de tumbar
    el arranque de toda la API por un ImportError en el router.

    Args:
        contact_id: Contacto origen (el duplicado que se absorbe).
        target_id: Contacto destino (el que sobrevive).
        user: Usuario autenticado; solo admin y super_admin.

    Returns:
        El contacto destino tras la fusion.

    Raises:
        AppException: 400 si los ids coinciden o el origen ya fue fusionado,
            404 si alguno no existe, 503 si el unificador no esta disponible.
    """
    if contact_id == target_id:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message="No se puede fusionar un contacto consigo mismo",
        )

    client_id: UUID = user["client_id"]

    try:
        from app.services.contact_unifier import ContactUnifier
    except ImportError as exc:
        logger.error("ContactUnifier no disponible: %s", exc)
        raise AppException(
            status_code=503,
            error_code=UNIFIER_UNAVAILABLE,
            message="La fusion de contactos no esta disponible todavia",
        ) from exc

    async with tenant_session(client_id) as session:
        source = await get_contact_or_404(session, contact_id, client_id)
        target = await get_contact_or_404(session, target_id, client_id)

        if source.merged_into_id is not None:
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message="El contacto origen ya fue fusionado",
            )
        if target.merged_into_id is not None:
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message="El contacto destino ya fue fusionado; usar el destino final",
            )

        await ContactUnifier(session).merge(
            source_id=contact_id, target_id=target_id, client_id=client_id
        )

        await session.flush()
        await session.refresh(target)
        respuesta = ContactResponse.model_validate(target)

    logger.info("Contacto %s fusionado en %s (tenant %s)", contact_id, target_id, client_id)
    return respuesta


@router.get("/{contact_id}/conversations", response_model=ConversationListResponse)
async def list_contact_conversations(
    contact_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_role(*_READ_ROLES)),
) -> ConversationListResponse:
    """Historial de conversaciones de un contacto, de la mas reciente a la mas vieja.

    Args:
        contact_id: Contacto del que se pide el historial.
        page: Pagina, empezando en 1.
        page_size: Tamano de pagina, maximo 100.
        user: Usuario autenticado.

    Returns:
        Pagina de conversaciones y total.

    Raises:
        AppException: 404 si el contacto no existe para este tenant.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        await get_contact_or_404(session, contact_id, client_id)

        filtros = [
            Conversation.client_id == client_id,
            Conversation.contact_id == contact_id,
        ]
        total = await session.scalar(select(func.count()).select_from(Conversation).where(*filtros))

        stmt = (
            select(Conversation)
            .where(*filtros)
            .order_by(Conversation.last_message_at.desc().nullslast())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        conversaciones = (await session.execute(stmt)).scalars().all()

        return ConversationListResponse(
            items=[ConversationResponse.model_validate(c) for c in conversaciones],
            total=int(total or 0),
            page=page,
            page_size=page_size,
        )
