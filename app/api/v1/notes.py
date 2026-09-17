"""Notas internas del equipo sobre un contacto.

GET  /api/v1/contacts/{contact_id}/notes   lista paginada, de la mas nueva a la mas vieja
POST /api/v1/contacts/{contact_id}/notes   agrega una nota firmada por el usuario actual

Son internas: nunca salen hacia el contacto por ningun canal, solo se ven desde
el panel. El autor sale del JWT, no del body, para que nadie pueda firmar una
nota a nombre de otro.
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.api.v1.contacts import get_contact_or_404
from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.models.internal_note import InternalNote
from app.schemas.common import PaginatedResponse
from app.schemas.note import NoteCreate, NoteResponse

logger = logging.getLogger(__name__)

router = APIRouter()

_ROLES = ("super_admin", "admin", "supervisor", "agent")


@router.get("/{contact_id}/notes", response_model=PaginatedResponse[NoteResponse])
async def list_notes(
    contact_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> PaginatedResponse[NoteResponse]:
    """Lista las notas internas de un contacto.

    Args:
        contact_id: Contacto del que se piden las notas.
        page: Pagina, empezando en 1.
        page_size: Tamano de pagina, maximo 100.
        user: Usuario autenticado.

    Returns:
        Pagina de notas y total.

    Raises:
        AppException: 404 si el contacto no existe para este tenant.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        await get_contact_or_404(session, contact_id, client_id)

        filtros = [
            InternalNote.client_id == client_id,
            InternalNote.contact_id == contact_id,
        ]
        total = int(
            await session.scalar(select(func.count()).select_from(InternalNote).where(*filtros))
            or 0
        )

        stmt = (
            select(InternalNote)
            .where(*filtros)
            .order_by(InternalNote.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        notas = (await session.execute(stmt)).scalars().all()

        return PaginatedResponse[NoteResponse](
            items=[NoteResponse.model_validate(n) for n in notas],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=(total + page_size - 1) // page_size,
        )


@router.post("/{contact_id}/notes", status_code=201, response_model=NoteResponse)
async def create_note(
    contact_id: UUID,
    data: NoteCreate,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> NoteResponse:
    """Agrega una nota interna sobre un contacto.

    Args:
        contact_id: Contacto sobre el que se escribe.
        data: Contenido de la nota.
        user: Usuario autenticado; queda como autor.

    Returns:
        La nota creada.

    Raises:
        AppException: 404 si el contacto no existe para este tenant.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        await get_contact_or_404(session, contact_id, client_id)

        nota = InternalNote(
            client_id=client_id,
            contact_id=contact_id,
            author_id=user["user_id"],
            content=data.content,
        )
        session.add(nota)
        await session.flush()
        await session.refresh(nota)
        return NoteResponse.model_validate(nota)
