"""CRUD de conversaciones y su ciclo de vida.

GET /api/v1/conversations              lista filtrable por status, canal y agente
GET /api/v1/conversations/{id}         detalle con los mensajes paginados
PUT /api/v1/conversations/{id}/assign  asigna la conversacion a un agente humano
PUT /api/v1/conversations/{id}/status  cambia el estado validando la transicion
POST /api/v1/conversations/{id}/messages  una persona del equipo contesta al contacto

Las transiciones de estado no se resuelven aqui: las dicta
`app/services/conversation_lifecycle.py`, que es el unico sitio donde vive la
maquina de estados (tambien la usa el worker de auto-cierre).
"""

import logging
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.nodes._delivery import deliver_message
from app.agents.nodes._tenant import ChannelNotConfiguredError, ContactIdentifierNotFoundError
from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.events import EVENT_CONVERSATION_RESOLVED, EventEmitter
from app.core.exceptions import (
    CONFLICT,
    DELIVERY_FAILED,
    FORBIDDEN,
    NOT_FOUND,
    VALIDATION_ERROR,
    AppException,
)
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.schemas.conversation import (
    ConversationAssignRequest,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationMessageResponse,
    ConversationMessageSend,
    ConversationResponse,
    ConversationStatusChangeRequest,
)
from app.services.conversation_lifecycle import VALID_TRANSITIONS, ConversationLifecycle

logger = logging.getLogger(__name__)

router = APIRouter()

VALID_STATUSES = tuple(VALID_TRANSITIONS)

_READ_ROLES = ("super_admin", "admin", "supervisor", "agent")
_ASSIGN_ROLES = ("super_admin", "admin", "supervisor")
_STATUS_ROLES = ("super_admin", "admin", "supervisor", "agent")


async def _get_conversation_or_404(
    session: AsyncSession, conversation_id: UUID, client_id: UUID
) -> Conversation:
    """Carga una conversacion del tenant activo o levanta 404.

    Args:
        session: Sesion con contexto de tenant.
        conversation_id: Conversacion buscada.
        client_id: Tenant propietario.

    Returns:
        La conversacion.

    Raises:
        AppException: 404 si no existe para este tenant.
    """
    stmt = select(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.client_id == client_id,
    )
    conversation = (await session.execute(stmt)).scalar_one_or_none()
    if conversation is None:
        raise AppException(
            status_code=404,
            error_code=NOT_FOUND,
            message="Conversacion no encontrada",
        )
    return conversation


@router.get("", response_model=ConversationListResponse)
async def list_conversations(
    status: str | None = Query(default=None, description="Filtra por estado"),
    channel: str | None = Query(default=None, description="Filtra por canal"),
    assigned_user_id: UUID | None = Query(default=None, description="Filtra por agente asignado"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_role(*_READ_ROLES)),
) -> ConversationListResponse:
    """Lista las conversaciones del tenant, de la mas activa a la mas vieja.

    Args:
        status: Uno de los 7 estados validos.
        channel: Canal (whatsapp, instagram, facebook, ...).
        assigned_user_id: Agente humano a cargo.
        page: Pagina, empezando en 1.
        page_size: Tamano de pagina, maximo 100.
        user: Usuario autenticado.

    Returns:
        Pagina de conversaciones y total de coincidencias.

    Raises:
        AppException: 400 si el status no es uno de los validos.
    """
    if status is not None and status not in VALID_STATUSES:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message=f"Status invalido: {status}. Validos: {', '.join(VALID_STATUSES)}",
        )

    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        filtros = [Conversation.client_id == client_id]
        if status is not None:
            filtros.append(Conversation.status == status)
        if channel is not None:
            filtros.append(Conversation.channel == channel)
        if assigned_user_id is not None:
            filtros.append(Conversation.assigned_user_id == assigned_user_id)

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


@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: UUID,
    page: int = Query(default=1, ge=1, description="Pagina de mensajes"),
    page_size: int = Query(default=50, ge=1, le=200, description="Mensajes por pagina"),
    user: dict[str, Any] = Depends(require_role(*_READ_ROLES)),
) -> ConversationDetailResponse:
    """Devuelve una conversacion con una pagina de sus mensajes.

    Los mensajes salen en orden cronologico (created_at ASC), que es como se leen
    en una bandeja de entrada.

    Args:
        conversation_id: Conversacion a consultar.
        page: Pagina de mensajes, empezando en 1.
        page_size: Mensajes por pagina, maximo 200.
        user: Usuario autenticado.

    Returns:
        La conversacion con sus mensajes y el total de mensajes.

    Raises:
        AppException: 404 si no existe para este tenant.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        conversation = await _get_conversation_or_404(session, conversation_id, client_id)

        filtros = [
            Message.client_id == client_id,
            Message.conversation_id == conversation_id,
        ]
        total_mensajes = await session.scalar(
            select(func.count()).select_from(Message).where(*filtros)
        )

        stmt = (
            select(Message)
            .where(*filtros)
            .order_by(Message.created_at.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        mensajes = (await session.execute(stmt)).scalars().all()

        return ConversationDetailResponse(
            **ConversationResponse.model_validate(conversation).model_dump(),
            messages=[ConversationMessageResponse.model_validate(m) for m in mensajes],
            total_messages=int(total_mensajes or 0),
            page=page,
            page_size=page_size,
        )


@router.put("/{conversation_id}/assign", response_model=ConversationResponse)
async def assign_conversation(
    conversation_id: UUID,
    data: ConversationAssignRequest,
    user: dict[str, Any] = Depends(require_role(*_ASSIGN_ROLES)),
) -> ConversationResponse:
    """Pone la conversacion a cargo de un agente humano.

    Si todavia no estaba en `human_active`, la transiciona; si ya lo estaba, solo
    cambia el agente asignado (reasignacion entre personas del equipo).

    El agente destino se busca con el `client_id` en el WHERE: sin eso, un admin
    podria asignarle conversaciones a un usuario de otro tenant.

    Args:
        conversation_id: Conversacion a asignar.
        data: Body con el `user_id` del agente.
        user: Usuario autenticado; solo admin, supervisor y super_admin.

    Returns:
        La conversacion ya asignada.

    Raises:
        AppException: 404 si la conversacion o el agente no existen, 400 si el
            estado actual no admite pasar a `human_active`.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        conversation = await _get_conversation_or_404(session, conversation_id, client_id)

        agente = (
            await session.execute(
                select(User).where(
                    User.id == data.user_id,
                    User.client_id == client_id,
                )
            )
        ).scalar_one_or_none()
        if agente is None:
            raise AppException(
                status_code=404,
                error_code=NOT_FOUND,
                message="Agente no encontrado",
            )
        if not agente.is_active:
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message="El agente esta desactivado",
            )

        if conversation.status == "human_active":
            conversation.assigned_user_id = data.user_id
        else:
            await ConversationLifecycle(session).transition(
                conversation, "human_active", user_id=data.user_id
            )

        await session.flush()
        await session.refresh(conversation)
        respuesta = ConversationResponse.model_validate(conversation)

    logger.info("Conversacion %s asignada a %s", conversation_id, data.user_id)
    return respuesta


@router.put("/{conversation_id}/status")
async def change_conversation_status(
    conversation_id: UUID,
    data: ConversationStatusChangeRequest,
    user: dict[str, Any] = Depends(require_role(*_STATUS_ROLES)),
) -> dict[str, Any]:
    """Cambia el estado de una conversacion validando la transicion.

    Args:
        conversation_id: Conversacion a mover.
        data: Body con el estado destino.
        user: Usuario autenticado.

    Returns:
        El estado anterior, el nuevo y las transiciones disponibles desde el nuevo.

    Raises:
        AppException: 404 si no existe, 400 si el estado no existe o la
            transicion no esta permitida.
    """
    if data.status not in VALID_STATUSES:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message=f"Status invalido: {data.status}. Validos: {', '.join(VALID_STATUSES)}",
        )

    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        conversation = await _get_conversation_or_404(session, conversation_id, client_id)

        # Hay que guardarlo antes: despues de transition() el objeto ya trae el
        # estado nuevo y devolverlo como "previous" seria mentir.
        estado_anterior = conversation.status

        # Solo se autoasigna cuando el propio usuario toma la conversacion; una
        # asignacion a un tercero va por PUT /assign.
        user_id = user["user_id"] if data.status == "human_active" else None

        await ConversationLifecycle(session).transition(conversation, data.status, user_id=user_id)
        await session.flush()
        contact_id = conversation.contact_id
        resuelta_en = conversation.resolved_at
        creada_en = conversation.created_at

    # Fuera del `async with`, o sea despues del commit: un webhook saliente no
    # puede anunciar una resolucion que termino en rollback.
    if data.status == "resolved":
        await EventEmitter.emit(
            EVENT_CONVERSATION_RESOLVED,
            client_id,
            {
                "conversation_id": str(conversation_id),
                "contact_id": str(contact_id),
                "previous_status": estado_anterior,
                "resolved_by": str(user["user_id"]),
                "resolution_time_seconds": (
                    int((resuelta_en - creada_en).total_seconds())
                    if resuelta_en and creada_en
                    else None
                ),
            },
        )

    return {
        "message": f"Estado actualizado a '{data.status}'",
        "previous_status": estado_anterior,
        "status": data.status,
        "valid_next_transitions": ConversationLifecycle.get_valid_transitions(data.status),
    }


# Estados en los que una persona puede contestar: los demas son terminales
# (`resolved` solo admite pasar a `archived`).
_REPLY_STATUSES = ("new", "bot_active", "human_active", "waiting_human", "waiting_client")


@router.post(
    "/{conversation_id}/messages", response_model=ConversationMessageResponse, status_code=201
)
async def send_conversation_message(
    conversation_id: UUID,
    data: ConversationMessageSend,
    user: dict[str, Any] = Depends(require_role(*_STATUS_ROLES)),
) -> ConversationMessageResponse:
    """Una persona del equipo contesta al contacto por el canal de la conversacion.

    El envio va por el mismo `MessagingProvider` que usa el agente, asi que el
    contacto lo recibe por su canal (WhatsApp, Telegram, email...) y el mensaje
    queda en el historial como `sender_type="agent"`.

    Al contestar la persona **toma la conversacion**: pasa a `human_active` y queda
    asignada a quien escribe, de modo que el bot deja de responder. Se hace *antes*
    de enviar, para que el bot no conteste a la vez que la persona; si el envio
    falla, la conversacion queda en sus manos igualmente (era su intencion).
    El envio va fuera de la transaccion, como el resto de llamadas de red.

    Un `agent` solo puede contestar conversaciones sin asignar o asignadas a el;
    supervisor, admin y super_admin pueden contestar cualquiera.

    Args:
        conversation_id: Conversacion a la que se contesta.
        data: Texto del mensaje.
        user: Usuario autenticado.

    Returns:
        El mensaje saliente tal como queda en el historial.

    Raises:
        AppException: 404 si no existe; 403 si es de otra persona y quien llama es
            `agent`; 409 si esta resuelta o archivada; 409 si el contacto no tiene
            identificador en el canal o el canal no esta configurado; 502 si el
            proveedor rechaza el envio.
    """
    client_id: UUID = user["client_id"]
    user_id: UUID = user["user_id"]

    async with tenant_session(client_id) as session:
        conversation = await _get_conversation_or_404(session, conversation_id, client_id)

        if conversation.status not in _REPLY_STATUSES:
            raise AppException(
                status_code=409,
                error_code=CONFLICT,
                message=f"La conversacion esta {conversation.status}: ya no admite respuestas",
            )
        asignada = conversation.assigned_user_id
        if user["role"] == "agent" and asignada is not None and asignada != user_id:
            raise AppException(
                status_code=403,
                error_code=FORBIDDEN,
                message="La conversacion esta asignada a otra persona del equipo",
            )

        if conversation.status != "human_active":
            await ConversationLifecycle(session).transition(
                conversation, "human_active", user_id=user_id
            )
        elif asignada is None:
            conversation.assigned_user_id = user_id
        contact_id = conversation.contact_id
        channel = conversation.channel

    message_id = uuid4()
    try:
        await deliver_message(
            client_id,
            conversation_id,
            contact_id,
            channel,
            data.text,
            sender_type="agent",
            sender_id=user_id,
            message_id=message_id,
        )
    except (ContactIdentifierNotFoundError, ChannelNotConfiguredError) as exc:
        logger.warning("No se pudo contestar %s por %s: %s", conversation_id, channel, exc)
        raise AppException(
            status_code=409,
            error_code=CONFLICT,
            message="El contacto no se puede alcanzar por el canal de esta conversacion",
        ) from exc
    except Exception as exc:
        # El detalle del proveedor (URLs, cuerpos de error) va al log, no al cliente.
        logger.exception("El proveedor rechazo el envio de la conversacion %s", conversation_id)
        raise AppException(
            status_code=502,
            error_code=DELIVERY_FAILED,
            message="No se pudo enviar el mensaje. Intentalo de nuevo",
        ) from exc

    async with tenant_session(client_id) as session:
        mensaje = (
            await session.execute(
                select(Message).where(Message.id == message_id, Message.client_id == client_id)
            )
        ).scalar_one()
        return ConversationMessageResponse.model_validate(mensaje)
