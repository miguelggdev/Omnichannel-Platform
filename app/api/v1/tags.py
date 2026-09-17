"""CRUD de etiquetas del tenant y su asignacion a contactos.

GET    /api/v1/tags                             lista las etiquetas del tenant
POST   /api/v1/tags                             crea una etiqueta
DELETE /api/v1/tags/{tag_id}                    borra la etiqueta y sus asignaciones
POST   /api/v1/contacts/{id}/tags/{tag_id}      asigna la etiqueta a un contacto
DELETE /api/v1/contacts/{id}/tags/{tag_id}      quita la etiqueta del contacto

Dos routers en un mismo modulo: el de etiquetas cuelga de `/tags` y el de
asignaciones de `/contacts`, que es donde el recurso pertenece de verdad. La spec
(§11) propone `/tags/contacts/{contact_id}/tags/{tag_id}`, que repite el segmento
y deja la relacion colgando del recurso equivocado.
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from app.api.v1.contacts import get_contact_or_404
from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import DUPLICATE, NOT_FOUND, AppException
from app.models.contact_tag import ContactTag
from app.models.tag import Tag
from app.schemas.tag import TagCreate, TagResponse

logger = logging.getLogger(__name__)

# Etiquetas del tenant: se monta en /api/v1/tags.
router = APIRouter()

# Relacion contacto-etiqueta: se monta en /api/v1/contacts.
contact_tags_router = APIRouter()

_READ_ROLES = ("super_admin", "admin", "supervisor", "agent")
_WRITE_ROLES = ("super_admin", "admin", "supervisor", "agent")
_DELETE_ROLES = ("super_admin", "admin")


@router.get("", response_model=list[TagResponse])
async def list_tags(
    user: dict[str, Any] = Depends(require_role(*_READ_ROLES)),
) -> list[TagResponse]:
    """Lista las etiquetas del tenant por orden alfabetico.

    No se pagina: son pocas por tenant y la UI las pinta todas en un selector.

    Args:
        user: Usuario autenticado.

    Returns:
        Las etiquetas del tenant.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        etiquetas = (
            (
                await session.execute(
                    select(Tag).where(Tag.client_id == client_id).order_by(Tag.name.asc())
                )
            )
            .scalars()
            .all()
        )
        return [TagResponse.model_validate(t) for t in etiquetas]


@router.post("", status_code=201, response_model=TagResponse)
async def create_tag(
    data: TagCreate,
    user: dict[str, Any] = Depends(require_role(*_WRITE_ROLES)),
) -> TagResponse:
    """Crea una etiqueta.

    El nombre es unico por tenant (`uq_tag_name_per_client`); aqui se comprueba
    antes de insertar para devolver un 409 legible en vez de un error de
    integridad.

    Args:
        data: Nombre y color de la etiqueta.
        user: Usuario autenticado.

    Returns:
        La etiqueta creada.

    Raises:
        AppException: 409 si ya existe una etiqueta con ese nombre.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        existente = (
            await session.execute(
                select(Tag).where(Tag.client_id == client_id, Tag.name == data.name)
            )
        ).scalar_one_or_none()
        if existente is not None:
            raise AppException(
                status_code=409,
                error_code=DUPLICATE,
                message=f"La etiqueta '{data.name}' ya existe",
            )

        tag = Tag(client_id=client_id, name=data.name, color=data.color)
        session.add(tag)
        await session.flush()
        await session.refresh(tag)
        return TagResponse.model_validate(tag)


@router.delete("/{tag_id}", status_code=200)
async def delete_tag(
    tag_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_DELETE_ROLES)),
) -> dict[str, Any]:
    """Borra una etiqueta y todas sus asignaciones a contactos.

    Las filas de `contact_tags` se borran explicitamente: la FK de Sprint 1 no
    declara `ON DELETE CASCADE` (mismo caso que `document_chunks` en Sprint 5),
    asi que sin esto el DELETE fallaria por violacion de integridad en cuanto la
    etiqueta estuviera en uso.

    Args:
        tag_id: Etiqueta a borrar.
        user: Usuario autenticado; solo admin y super_admin.

    Returns:
        Confirmacion con cuantas asignaciones se eliminaron.

    Raises:
        AppException: 404 si la etiqueta no existe para este tenant.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        tag = (
            await session.execute(select(Tag).where(Tag.id == tag_id, Tag.client_id == client_id))
        ).scalar_one_or_none()
        if tag is None:
            raise AppException(
                status_code=404,
                error_code=NOT_FOUND,
                message="Etiqueta no encontrada",
            )

        # `borradas` se anota como Any a proposito: `AsyncSession.execute()` esta
        # tipado como `Result[Any]`, y segun la version de SQLAlchemy ese tipo
        # expone `rowcount` (un DML siempre devuelve un `CursorResult`) o no. Con
        # `cast` a `CursorResult` mypy falla en una version por atributo
        # inexistente y en la otra por cast redundante; `Any` vale en ambas.
        borradas: Any = await session.execute(
            sa_delete(ContactTag).where(
                ContactTag.client_id == client_id,
                ContactTag.tag_id == tag_id,
            )
        )
        await session.delete(tag)
        await session.flush()

    logger.info("Etiqueta %s borrada (tenant %s)", tag_id, client_id)
    return {"message": "Etiqueta eliminada", "assignments_removed": borradas.rowcount or 0}


@contact_tags_router.post("/{contact_id}/tags/{tag_id}", status_code=201)
async def assign_tag_to_contact(
    contact_id: UUID,
    tag_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_WRITE_ROLES)),
) -> dict[str, Any]:
    """Asigna una etiqueta a un contacto.

    Args:
        contact_id: Contacto a etiquetar.
        tag_id: Etiqueta a asignar.
        user: Usuario autenticado.

    Returns:
        Confirmacion de la asignacion.

    Raises:
        AppException: 404 si el contacto o la etiqueta no existen, 409 si la
            etiqueta ya estaba asignada.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        await get_contact_or_404(session, contact_id, client_id)

        tag = (
            await session.execute(select(Tag).where(Tag.id == tag_id, Tag.client_id == client_id))
        ).scalar_one_or_none()
        if tag is None:
            raise AppException(
                status_code=404,
                error_code=NOT_FOUND,
                message="Etiqueta no encontrada",
            )

        existente = (
            await session.execute(
                select(ContactTag).where(
                    ContactTag.client_id == client_id,
                    ContactTag.contact_id == contact_id,
                    ContactTag.tag_id == tag_id,
                )
            )
        ).scalar_one_or_none()
        if existente is not None:
            raise AppException(
                status_code=409,
                error_code=DUPLICATE,
                message="La etiqueta ya estaba asignada a este contacto",
            )

        session.add(ContactTag(client_id=client_id, contact_id=contact_id, tag_id=tag_id))
        await session.flush()

    return {"message": "Etiqueta asignada"}


@contact_tags_router.delete("/{contact_id}/tags/{tag_id}", status_code=200)
async def remove_tag_from_contact(
    contact_id: UUID,
    tag_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_WRITE_ROLES)),
) -> dict[str, Any]:
    """Quita una etiqueta de un contacto.

    Args:
        contact_id: Contacto a desetiquetar.
        tag_id: Etiqueta a quitar.
        user: Usuario autenticado.

    Returns:
        Confirmacion de la baja.

    Raises:
        AppException: 404 si el contacto no tenia esa etiqueta.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        relacion = (
            await session.execute(
                select(ContactTag).where(
                    ContactTag.client_id == client_id,
                    ContactTag.contact_id == contact_id,
                    ContactTag.tag_id == tag_id,
                )
            )
        ).scalar_one_or_none()
        if relacion is None:
            raise AppException(
                status_code=404,
                error_code=NOT_FOUND,
                message="El contacto no tiene asignada esa etiqueta",
            )

        await session.delete(relacion)
        await session.flush()

    return {"message": "Etiqueta removida"}
