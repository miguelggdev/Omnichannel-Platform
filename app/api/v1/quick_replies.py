"""CRUD de respuestas rapidas del tenant.

GET    /api/v1/quick-replies                 lista, filtrable por categoria
POST   /api/v1/quick-replies                 crea una (atajo unico por tenant)
PUT    /api/v1/quick-replies/{id}            edicion parcial
DELETE /api/v1/quick-replies/{id}            borra
POST   /api/v1/quick-replies/{id}/render     resuelve las variables para una conversacion

El endpoint de render es el que le da sentido a la feature: el contenido guardado
lleva marcadores `{{contact_name}}`, `{{agent_name}}`, `{{ticket_id}}` y
`{{date}}`, y quien va a enviar la respuesta necesita el texto ya resuelto para
esa conversacion concreta. La resolucion vive en `app/services/quick_reply.py`.
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import DUPLICATE, NOT_FOUND, AppException
from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.quick_reply import QuickReply
from app.models.user import User
from app.schemas.quick_reply import (
    QuickReplyCreate,
    QuickReplyRenderRequest,
    QuickReplyRenderResponse,
    QuickReplyResponse,
    QuickReplyUpdate,
)
from app.services.quick_reply import resolve_quick_reply

logger = logging.getLogger(__name__)

router = APIRouter()

_READ_ROLES = ("super_admin", "admin", "supervisor", "agent")
_WRITE_ROLES = ("super_admin", "admin", "supervisor", "agent")
_DELETE_ROLES = ("super_admin", "admin")


async def _get_or_404(session: AsyncSession, quick_reply_id: UUID, client_id: UUID) -> QuickReply:
    """Carga una respuesta rapida del tenant activo o levanta 404.

    Args:
        session: Sesion con contexto de tenant.
        quick_reply_id: Respuesta buscada.
        client_id: Tenant propietario.

    Returns:
        La respuesta rapida.

    Raises:
        AppException: 404 si no existe para este tenant.
    """
    stmt = select(QuickReply).where(
        QuickReply.id == quick_reply_id,
        QuickReply.client_id == client_id,
    )
    quick_reply = (await session.execute(stmt)).scalar_one_or_none()
    if quick_reply is None:
        raise AppException(
            status_code=404,
            error_code=NOT_FOUND,
            message="Respuesta rapida no encontrada",
        )
    return quick_reply


async def _rechazar_atajo_duplicado(
    session: AsyncSession, client_id: UUID, shortcut: str, excluir: UUID | None = None
) -> None:
    """Levanta 409 si el atajo ya existe en el tenant.

    La base tiene `uq_quick_reply_shortcut`; esto se adelanta para devolver un
    mensaje legible en vez de un error de integridad.

    Args:
        session: Sesion con contexto de tenant.
        client_id: Tenant propietario.
        shortcut: Atajo a comprobar.
        excluir: Respuesta que no cuenta como choque (la que se esta editando).

    Raises:
        AppException: 409 si otro registro ya usa ese atajo.
    """
    stmt = select(QuickReply).where(
        QuickReply.client_id == client_id,
        QuickReply.shortcut == shortcut,
    )
    if excluir is not None:
        stmt = stmt.where(QuickReply.id != excluir)

    if (await session.execute(stmt)).scalar_one_or_none() is not None:
        raise AppException(
            status_code=409,
            error_code=DUPLICATE,
            message=f"El atajo '{shortcut}' ya existe",
        )


@router.get("", response_model=list[QuickReplyResponse])
async def list_quick_replies(
    category: str | None = Query(default=None, description="Filtra por categoria"),
    user: dict[str, Any] = Depends(require_role(*_READ_ROLES)),
) -> list[QuickReplyResponse]:
    """Lista las respuestas rapidas del tenant, ordenadas por atajo.

    No se pagina: son un puñado por tenant y la UI las ofrece en un desplegable
    mientras el agente escribe.

    Args:
        category: Limita a una categoria.
        user: Usuario autenticado.

    Returns:
        Las respuestas rapidas del tenant.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        filtros = [QuickReply.client_id == client_id]
        if category is not None:
            filtros.append(QuickReply.category == category)

        stmt = select(QuickReply).where(*filtros).order_by(QuickReply.shortcut.asc())
        respuestas = (await session.execute(stmt)).scalars().all()
        return [QuickReplyResponse.model_validate(r) for r in respuestas]


@router.post("", status_code=201, response_model=QuickReplyResponse)
async def create_quick_reply(
    data: QuickReplyCreate,
    user: dict[str, Any] = Depends(require_role(*_WRITE_ROLES)),
) -> QuickReplyResponse:
    """Crea una respuesta rapida.

    Args:
        data: Atajo, titulo, contenido y categoria.
        user: Usuario autenticado; queda como autor.

    Returns:
        La respuesta rapida creada.

    Raises:
        AppException: 409 si el atajo ya existe en el tenant.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        await _rechazar_atajo_duplicado(session, client_id, data.shortcut)

        quick_reply = QuickReply(
            client_id=client_id,
            shortcut=data.shortcut,
            title=data.title,
            content=data.content,
            category=data.category,
            created_by=user["user_id"],
        )
        session.add(quick_reply)
        await session.flush()
        await session.refresh(quick_reply)
        return QuickReplyResponse.model_validate(quick_reply)


@router.put("/{quick_reply_id}", response_model=QuickReplyResponse)
async def update_quick_reply(
    quick_reply_id: UUID,
    data: QuickReplyUpdate,
    user: dict[str, Any] = Depends(require_role(*_WRITE_ROLES)),
) -> QuickReplyResponse:
    """Actualiza los campos enviados de una respuesta rapida.

    Args:
        quick_reply_id: Respuesta a actualizar.
        data: Campos a modificar.
        user: Usuario autenticado.

    Returns:
        La respuesta rapida ya actualizada.

    Raises:
        AppException: 404 si no existe, 409 si el atajo nuevo ya esta en uso.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        quick_reply = await _get_or_404(session, quick_reply_id, client_id)

        cambios = data.model_dump(exclude_unset=True)
        if "shortcut" in cambios and cambios["shortcut"] != quick_reply.shortcut:
            await _rechazar_atajo_duplicado(
                session, client_id, cambios["shortcut"], excluir=quick_reply_id
            )

        for campo, valor in cambios.items():
            if valor is not None or campo == "category":
                setattr(quick_reply, campo, valor)

        await session.flush()
        await session.refresh(quick_reply)
        return QuickReplyResponse.model_validate(quick_reply)


@router.delete("/{quick_reply_id}", status_code=200)
async def delete_quick_reply(
    quick_reply_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_DELETE_ROLES)),
) -> dict[str, str]:
    """Borra una respuesta rapida.

    Args:
        quick_reply_id: Respuesta a borrar.
        user: Usuario autenticado; solo admin y super_admin.

    Returns:
        Confirmacion del borrado.

    Raises:
        AppException: 404 si no existe para este tenant.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        quick_reply = await _get_or_404(session, quick_reply_id, client_id)
        await session.delete(quick_reply)
        await session.flush()

    logger.info("Respuesta rapida %s borrada (tenant %s)", quick_reply_id, client_id)
    return {"message": "Respuesta rapida eliminada"}


@router.post("/{quick_reply_id}/render", response_model=QuickReplyRenderResponse)
async def render_quick_reply(
    quick_reply_id: UUID,
    data: QuickReplyRenderRequest,
    user: dict[str, Any] = Depends(require_role(*_READ_ROLES)),
) -> QuickReplyRenderResponse:
    """Devuelve el contenido con las variables resueltas para una conversacion.

    Las variables que no se puedan resolver quedan tal cual en el texto y se
    listan en `unresolved`: es preferible que el agente vea `{{contact_name}}`
    sin resolver y lo corrija, a que salga un hueco vacio hacia el cliente.

    Args:
        quick_reply_id: Respuesta a resolver.
        data: Conversacion desde la que se usa.
        user: Usuario autenticado; tambien es el `{{agent_name}}`.

    Returns:
        El contenido resuelto y las variables que quedaron pendientes.

    Raises:
        AppException: 404 si la respuesta o la conversacion no existen.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        quick_reply = await _get_or_404(session, quick_reply_id, client_id)

        conversation = (
            await session.execute(
                select(Conversation).where(
                    Conversation.id == data.conversation_id,
                    Conversation.client_id == client_id,
                )
            )
        ).scalar_one_or_none()
        if conversation is None:
            raise AppException(
                status_code=404,
                error_code=NOT_FOUND,
                message="Conversacion no encontrada",
            )

        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.id == conversation.contact_id,
                    Contact.client_id == client_id,
                )
            )
        ).scalar_one_or_none()

        agent = (
            await session.execute(
                select(User).where(
                    User.id == user["user_id"],
                    User.client_id == client_id,
                )
            )
        ).scalar_one_or_none()

        contexto: dict[str, Any] = {"conversation": conversation}
        # Se omiten las claves que no se pudieron resolver en vez de meterlas en
        # None: el resolver distingue "falta el dato" de "el dato es invalido",
        # y en los dos casos la variable queda sin sustituir y reportada.
        if contact is not None:
            contexto["contact"] = contact
        if agent is not None:
            contexto["agent"] = agent

        contenido, sin_resolver = resolve_quick_reply(quick_reply.content, contexto)

        return QuickReplyRenderResponse(
            shortcut=quick_reply.shortcut,
            content=contenido,
            unresolved=sin_resolver,
        )
