"""API de llamadas agendadas: humanas, con voz IA o mixtas (Sprint 19, Dev B).

    GET    /api/v1/scheduled-calls                  lista con filtros y paginacion
    POST   /api/v1/scheduled-calls                  agenda (sin doble reserva de la misma persona)
    GET    /api/v1/scheduled-calls/slots            huecos libres (horario del negocio + calendario)
    GET    /api/v1/scheduled-calls/{id}             detalle
    PATCH  /api/v1/scheduled-calls/{id}             edita las notas
    POST   /api/v1/scheduled-calls/{id}/confirm     pendiente -> confirmada
    POST   /api/v1/scheduled-calls/{id}/cancel      cancela (y borra su evento del calendario)
    POST   /api/v1/scheduled-calls/{id}/reschedule  la mueve: la vieja queda `rescheduled`
    POST   /api/v1/scheduled-calls/{id}/complete    la cierra con su resultado
    POST   /api/v1/scheduled-calls/{id}/no-show     el lead no estuvo
    POST   /api/v1/scheduled-calls/{id}/initiate    inicia YA una llamada con voz IA

Reglas:

- **Sin doble reserva:** la serializa el servicio de Dev A (`pg_advisory_xact_lock` por persona).
- **Google Calendar es opcional** (`call_calendar`): si esta configurado, los huecos descuentan sus
  eventos y las llamadas humanas crean el suyo; si no lo esta, o Google no responde, el
  agendamiento sigue y la respuesta lo dice (`calendar_checked`, `calendar_event_id`).
- **Despues del commit** van las cosas externas (evento del calendario, email al agente): nunca
  con la transaccion de la agenda abierta, y un fallo ahi no deshace el agendamiento.
- Un `agent` solo actua sobre llamadas suyas o sin persona asignada. Iniciar una llamada de voz IA
  es de `supervisor` para arriba (llama a una persona) y tiene limite por usuario y hora.
- `ai_voice_config` no admite la clave `runtime`: es del servidor (ver `scheduled_call_voice`).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.dependencies import require_leads_module
from app.core.exceptions import (
    CONFLICT,
    FORBIDDEN,
    NOT_FOUND,
    VALIDATION_ERROR,
    AppException,
)
from app.core.rate_limit import hit
from app.models.lead import Lead
from app.models.scheduled_call import (
    CALL_CANCELLED,
    CALL_CONFIRMED,
    CALL_NO_SHOW,
    UPCOMING_STATUSES,
    ScheduledCall,
)
from app.schemas.common import PaginatedResponse
from app.schemas.deal import (
    CallStatus,
    CallType,
    ScheduledCallComplete,
    ScheduledCallCreate,
    ScheduledCallReschedule,
    ScheduledCallResponse,
)
from app.schemas.scheduled_call_api import ScheduledCallNotes, SlotsResponse
from app.services import call_calendar
from app.services.call_notifications import avisar_agente_de_llamada_nueva
from app.services.call_scheduling import EstadoInvalidoError, Intervalo, huecos_libres
from app.services.lead_outreach import datos_del_tenant
from app.services.lead_sequence_timing import ventana_del_tenant
from app.services.scheduled_call_voice import RUNTIME, iniciar_llamada_ia
from app.services.scheduled_calls import (
    CallError,
    agendar,
    cambiar_estado,
    completar,
    ocupados_del_usuario,
    reprogramar,
)
from app.services.voice_call_provider import available_voice_providers

logger = logging.getLogger(__name__)

router = APIRouter()

_READ_ROLES = ("super_admin", "admin", "supervisor", "agent")
_WRITE_ROLES = ("super_admin", "admin", "supervisor", "agent")
_INITIATE_ROLES = ("super_admin", "admin", "supervisor")

MAX_DIAS_HUECOS = 31
INICIAR_POR_USUARIO_Y_HORA = 30

_ESTADO_HTTP: dict[str, tuple[int, str]] = {
    "overlap": (409, CONFLICT),
    "calendar_busy": (409, CONFLICT),
    "in_the_past": (400, VALIDATION_ERROR),
    "lead_unavailable": (409, CONFLICT),
    "lead_mismatch": (400, VALIDATION_ERROR),
    "deal_mismatch": (400, VALIDATION_ERROR),
    "user_unavailable": (400, VALIDATION_ERROR),
    "call_record_unavailable": (400, VALIDATION_ERROR),
    "invalid_status": (409, CONFLICT),
    "not_found": (404, NOT_FOUND),
    "not_ai_call": (409, CONFLICT),
    "already_starting": (409, CONFLICT),
    "opted_out": (409, CONFLICT),
    "no_phone": (400, VALIDATION_ERROR),
    "provider_unknown": (400, VALIDATION_ERROR),
    "provider_not_configured": (503, "PROVIDER_UNAVAILABLE"),
    "provider_down": (503, "PROVIDER_UNAVAILABLE"),
    "provider_rejected": (502, "PROVIDER_REJECTED"),
}


def _uid(user: dict[str, Any]) -> UUID:
    return UUID(str(user["user_id"]))


def _como_http(exc: CallError | EstadoInvalidoError) -> AppException:
    """Traduce un error del servicio de llamadas a la respuesta de la API."""
    estado, codigo = _ESTADO_HTTP.get(exc.code, (409, CONFLICT))
    return AppException(status_code=estado, error_code=codigo, message=str(exc))


def _fin(llamada: ScheduledCall) -> datetime:
    return llamada.scheduled_at + timedelta(minutes=llamada.duration_minutes)


async def _llamada_o_404(
    session: AsyncSession, call_id: UUID, user: dict[str, Any], *, bloquear: bool = False
) -> ScheduledCall:
    """Una llamada del tenant sobre la que este usuario puede actuar.

    Un `agent` solo actua sobre llamadas suyas o sin persona asignada.

    Raises:
        AppException: 404 si no existe; 403 si es de otro y el usuario es `agent`.
    """
    consulta = select(ScheduledCall).where(
        ScheduledCall.id == call_id, ScheduledCall.client_id == user["client_id"]
    )
    if bloquear:
        consulta = consulta.with_for_update()
    llamada: ScheduledCall | None = (await session.execute(consulta)).scalar_one_or_none()
    if llamada is None:
        raise AppException(status_code=404, error_code=NOT_FOUND, message="Llamada no encontrada")
    if user["role"] == "agent" and llamada.assigned_user_id not in (None, _uid(user)):
        raise AppException(
            status_code=403,
            error_code=FORBIDDEN,
            message="Un agente solo puede actuar sobre llamadas suyas o sin persona asignada",
        )
    return llamada


async def _ids_de_eventos_propios(
    session: AsyncSession, client_id: UUID, desde: datetime, hasta: datetime
) -> set[str]:
    """Ids de los eventos de calendario que nosotros creamos y caen cerca del rango."""
    filas = await session.execute(
        select(ScheduledCall.calendar_event_id).where(
            ScheduledCall.client_id == client_id,
            ScheduledCall.calendar_event_id.is_not(None),
            ScheduledCall.status.in_(UPCOMING_STATUSES),
            ScheduledCall.scheduled_at > desde - timedelta(hours=8),
            ScheduledCall.scheduled_at < hasta,
        )
    )
    return {e for e in filas.scalars().all() if e}


async def _guardar_evento(
    client_id: UUID, call_id: UUID, event_id: str | None, *, user_id: UUID | None = None
) -> None:
    """Guarda (o borra) el id del evento de una llamada, en su propia transaccion corta."""
    async with tenant_session(client_id, user_id=user_id) as session:
        llamada = (
            await session.execute(
                select(ScheduledCall).where(
                    ScheduledCall.id == call_id, ScheduledCall.client_id == client_id
                )
            )
        ).scalar_one_or_none()
        if llamada is not None:
            llamada.calendar_event_id = event_id


@router.get("", response_model=PaginatedResponse[ScheduledCallResponse])
async def list_calls(
    status: CallStatus | None = None,
    call_type: CallType | None = None,
    lead_id: UUID | None = None,
    deal_id: UUID | None = None,
    assigned_user_id: UUID | None = None,
    scheduled_from: datetime | None = None,
    scheduled_to: datetime | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> PaginatedResponse[ScheduledCallResponse]:
    """Lista las llamadas agendadas, las mas proximas primero.

    Args:
        status: Solo este estado.
        call_type: Solo este tipo.
        lead_id: Solo las de este lead.
        deal_id: Solo las de este deal.
        assigned_user_id: Solo las de esta persona.
        scheduled_from: Desde este instante.
        scheduled_to: Hasta este instante.
        page: Pagina, desde 1.
        page_size: Tamano de pagina, hasta 100.
        user: Usuario autenticado.

    Returns:
        La pagina.
    """
    client_id: UUID = user["client_id"]
    filtros: list[Any] = [ScheduledCall.client_id == client_id]
    for columna, valor in (
        (ScheduledCall.status, status),
        (ScheduledCall.call_type, call_type),
        (ScheduledCall.lead_id, lead_id),
        (ScheduledCall.deal_id, deal_id),
        (ScheduledCall.assigned_user_id, assigned_user_id),
    ):
        if valor is not None:
            filtros.append(columna == valor)
    if scheduled_from is not None:
        filtros.append(ScheduledCall.scheduled_at >= scheduled_from)
    if scheduled_to is not None:
        filtros.append(ScheduledCall.scheduled_at <= scheduled_to)
    async with tenant_session(client_id) as session:
        total = (
            await session.execute(select(func.count()).select_from(ScheduledCall).where(*filtros))
        ).scalar_one()
        filas = (
            (
                await session.execute(
                    select(ScheduledCall)
                    .where(*filtros)
                    .order_by(ScheduledCall.scheduled_at, ScheduledCall.id)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return PaginatedResponse[ScheduledCallResponse](
            items=[ScheduledCallResponse.model_validate(f) for f in filas],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=(total + page_size - 1) // page_size,
        )


@router.get("/slots", response_model=SlotsResponse)
async def get_slots(
    user_id: UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    duration_minutes: int = Query(default=30, ge=5, le=480),
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> SlotsResponse:
    """Huecos libres para agendar una llamada.

    Descuenta las llamadas de la persona, el horario del negocio y, si hay calendario de Google
    configurado y responde, sus eventos (los que no creamos nosotros).

    Args:
        user_id: Quien llamaria; sin el (voz IA) solo cuenta el horario y el calendario.
        date_from: Inicio de la busqueda (aware); por defecto, ahora.
        date_to: Fin (aware); por defecto, 7 dias despues del inicio. Maximo 31 dias.
        duration_minutes: Duracion de la llamada.
        user: Usuario autenticado.

    Returns:
        Los huecos en UTC y si se miro el calendario.

    Raises:
        AppException: 400 si las fechas no tienen zona, `date_to` no es posterior o el rango pasa
            de 31 dias.
    """
    client_id: UUID = user["client_id"]
    ahora = datetime.now(timezone.utc)
    desde = date_from or ahora
    hasta = date_to or desde + timedelta(days=7)
    if desde.tzinfo is None or hasta.tzinfo is None:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message="Las fechas necesitan zona horaria",
        )
    if hasta <= desde or hasta - desde > timedelta(days=MAX_DIAS_HUECOS):
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message=f"date_to debe ser posterior a date_from y distar como mucho {MAX_DIAS_HUECOS} dias",
        )
    async with tenant_session(client_id) as session:
        ventana = await ventana_del_tenant(session, client_id)
        ocupados: list[Intervalo] = (
            await ocupados_del_usuario(session, client_id, user_id, desde=desde, hasta=hasta)
            if user_id is not None
            else []
        )
        propios = await _ids_de_eventos_propios(session, client_id, desde, hasta)
    calendario = await call_calendar.ocupados_del_calendario(
        client_id, desde, hasta, ignorar_ids=propios
    )
    huecos = huecos_libres(
        desde=desde,
        hasta=hasta,
        duracion_minutos=duration_minutes,
        ventana=ventana,
        ocupados=[*ocupados, *calendario.intervalos],
        ahora=ahora,
    )
    return SlotsResponse(
        slots=huecos,
        timezone=ventana.tz.key,
        duration_minutes=duration_minutes,
        calendar_checked=calendario.consultado,
    )


@router.post("", response_model=ScheduledCallResponse, status_code=201)
async def schedule_call(
    data: ScheduledCallCreate,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> ScheduledCallResponse:
    """Agenda una llamada con un lead.

    Una llamada humana o mixta comprueba ademas el calendario de Google (si esta configurado y
    responde) y crea su evento; el agente asignado recibe un email. Ambas cosas ocurren despues de
    guardar la llamada y no la deshacen si fallan.

    Args:
        data: Datos de la llamada.
        user: Usuario autenticado.

    Returns:
        La llamada creada.

    Raises:
        AppException: 404 si el lead no existe; 403 si un agente agenda a otra persona o sobre un
            lead de otro; 400 si la hora esta en el pasado, `ai_voice_config` trae la clave
            reservada `runtime` o el deal/usuario no son validos; 409 si esa persona ya tiene una
            llamada (o un evento de calendario) en ese horario o el lead esta suprimido.
    """
    client_id: UUID = user["client_id"]
    uid = _uid(user)
    ahora = datetime.now(timezone.utc)
    if RUNTIME in data.ai_voice_config:
        raise AppException(
            status_code=422,
            error_code=VALIDATION_ERROR,
            message=f"'{RUNTIME}' es una clave reservada de ai_voice_config",
        )
    if data.ai_voice_provider and data.ai_voice_provider not in available_voice_providers():
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message=f"Proveedor de voz desconocido. Disponibles: {list(available_voice_providers())}",
        )
    if user["role"] == "agent" and data.assigned_user_id not in (None, uid):
        raise AppException(
            status_code=403,
            error_code=FORBIDDEN,
            message="Un agente solo puede agendarse llamadas a si mismo",
        )
    fin = data.scheduled_at + timedelta(minutes=data.duration_minutes)
    if data.call_type != "ai_voice" and data.scheduled_at > ahora:
        async with tenant_session(client_id) as session:
            propios = await _ids_de_eventos_propios(session, client_id, data.scheduled_at, fin)
        calendario = await call_calendar.ocupados_del_calendario(
            client_id, data.scheduled_at, fin, ignorar_ids=propios
        )
        if calendario.intervalos:
            raise AppException(
                status_code=409,
                error_code=CONFLICT,
                message="Hay un evento en el calendario en ese horario",
            )
    async with tenant_session(client_id, user_id=uid) as session:
        lead: Lead | None = (
            await session.execute(
                select(Lead).where(
                    Lead.id == data.lead_id, Lead.client_id == client_id, Lead.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if lead is None:
            raise AppException(status_code=404, error_code=NOT_FOUND, message="Lead no encontrado")
        if user["role"] == "agent" and lead.assigned_user_id not in (None, uid):
            raise AppException(
                status_code=403,
                error_code=FORBIDDEN,
                message="Un agente solo agenda llamadas de leads suyos o sin responsable",
            )
        try:
            llamada = await agendar(session, lead, data, ahora=ahora, user_id=uid)
        except CallError as exc:
            raise _como_http(exc) from exc
        nombre, empresa = lead.first_name, lead.company_name
        call_id, tipo = llamada.id, llamada.call_type

    if tipo != "ai_voice":
        event_id = await call_calendar.crear_evento(
            client_id, nombre=nombre, empresa=empresa, inicio=data.scheduled_at, fin=fin
        )
        if event_id is not None:
            await _guardar_evento(client_id, call_id, event_id, user_id=uid)
        await avisar_agente_de_llamada_nueva(client_id, call_id)

    async with tenant_session(client_id) as session:
        creada = (
            await session.execute(
                select(ScheduledCall).where(
                    ScheduledCall.id == call_id, ScheduledCall.client_id == client_id
                )
            )
        ).scalar_one()
        return ScheduledCallResponse.model_validate(creada)


@router.get("/{call_id}", response_model=ScheduledCallResponse)
async def get_call(
    call_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> ScheduledCallResponse:
    """Detalle de una llamada agendada (sin transcripcion: esta en `call_records`).

    Args:
        call_id: Llamada.
        user: Usuario autenticado.

    Returns:
        La llamada.
    """
    async with tenant_session(user["client_id"]) as session:
        consulta = await session.execute(
            select(ScheduledCall).where(
                ScheduledCall.id == call_id, ScheduledCall.client_id == user["client_id"]
            )
        )
        llamada = consulta.scalar_one_or_none()
        if llamada is None:
            raise AppException(
                status_code=404, error_code=NOT_FOUND, message="Llamada no encontrada"
            )
        return ScheduledCallResponse.model_validate(llamada)


@router.patch("/{call_id}", response_model=ScheduledCallResponse)
async def update_notes(
    call_id: UUID,
    data: ScheduledCallNotes,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> ScheduledCallResponse:
    """Edita las notas. La hora y la persona se cambian reprogramando.

    Args:
        call_id: Llamada.
        data: Notas nuevas.
        user: Usuario autenticado.

    Returns:
        La llamada.
    """
    async with tenant_session(user["client_id"], user_id=_uid(user)) as session:
        llamada = await _llamada_o_404(session, call_id, user, bloquear=True)
        llamada.notes = data.notes
        await session.flush()
        await session.refresh(llamada)
        return ScheduledCallResponse.model_validate(llamada)


async def _cambiar(
    call_id: UUID, user: dict[str, Any], nuevo: str, *, outcome: str | None = None
) -> ScheduledCallResponse:
    """Cambia el estado de una llamada con la fila bloqueada."""
    async with tenant_session(user["client_id"], user_id=_uid(user)) as session:
        llamada = await _llamada_o_404(session, call_id, user, bloquear=True)
        try:
            cambiar_estado(
                session,
                llamada,
                nuevo,
                ahora=datetime.now(timezone.utc),
                user_id=_uid(user),
                outcome=outcome,
            )
        except EstadoInvalidoError as exc:
            raise _como_http(exc) from exc
        await session.flush()
        await session.refresh(llamada)
        return ScheduledCallResponse.model_validate(llamada)


@router.post("/{call_id}/confirm", response_model=ScheduledCallResponse)
async def confirm_call(
    call_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> ScheduledCallResponse:
    """Confirma una llamada pendiente.

    Args:
        call_id: Llamada.
        user: Usuario autenticado.

    Returns:
        La llamada confirmada.

    Raises:
        AppException: 404 si no existe; 409 si no estaba pendiente.
    """
    return await _cambiar(call_id, user, CALL_CONFIRMED)


@router.post("/{call_id}/no-show", response_model=ScheduledCallResponse)
async def mark_no_show(
    call_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> ScheduledCallResponse:
    """Marca que el lead no estuvo en la llamada.

    Args:
        call_id: Llamada.
        user: Usuario autenticado.

    Returns:
        La llamada.

    Raises:
        AppException: 404 si no existe; 409 si no se puede pasar a `no_show` desde su estado.
    """
    return await _cambiar(call_id, user, CALL_NO_SHOW)


@router.post("/{call_id}/cancel", response_model=ScheduledCallResponse)
async def cancel_call(
    call_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> ScheduledCallResponse:
    """Cancela una llamada pendiente o confirmada y borra su evento del calendario.

    Args:
        call_id: Llamada.
        user: Usuario autenticado.

    Returns:
        La llamada cancelada.

    Raises:
        AppException: 404 si no existe; 409 si ya no se puede cancelar.
    """
    respuesta = await _cambiar(call_id, user, CALL_CANCELLED)
    if respuesta.calendar_event_id and await call_calendar.cancelar_evento(
        user["client_id"], respuesta.calendar_event_id
    ):
        await _guardar_evento(user["client_id"], call_id, None, user_id=_uid(user))
        respuesta = respuesta.model_copy(update={"calendar_event_id": None})
    return respuesta


@router.post("/{call_id}/reschedule", response_model=ScheduledCallResponse, status_code=201)
async def reschedule_call(
    call_id: UUID,
    data: ScheduledCallReschedule,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> ScheduledCallResponse:
    """Mueve la llamada: la vieja queda `rescheduled` y nace una nueva (con su recordatorio).

    El evento del calendario se mueve a la nueva; si no se puede, se intenta crear uno nuevo.

    Args:
        call_id: Llamada pendiente o confirmada.
        data: Nuevo inicio (y duracion).
        user: Usuario autenticado.

    Returns:
        La llamada nueva.

    Raises:
        AppException: 404 si no existe; 409 si ya no se puede reprogramar o la persona esta
            ocupada; 400 si la hora esta en el pasado.
    """
    client_id: UUID = user["client_id"]
    uid = _uid(user)
    ahora = datetime.now(timezone.utc)
    async with tenant_session(client_id, user_id=uid) as session:
        vieja = await _llamada_o_404(session, call_id, user, bloquear=True)
        evento_viejo = vieja.calendar_event_id
        lead = (
            await session.execute(
                select(Lead).where(Lead.id == vieja.lead_id, Lead.client_id == client_id)
            )
        ).scalar_one()
        nombre, empresa = lead.first_name, lead.company_name
        try:
            nueva = await reprogramar(session, vieja, data, ahora=ahora, user_id=uid)
        except (CallError, EstadoInvalidoError) as exc:
            raise _como_http(exc) from exc
        vieja.calendar_event_id = None
        nueva_id, tipo = nueva.id, nueva.call_type
        inicio, fin = nueva.scheduled_at, _fin(nueva)

    if tipo != "ai_voice":
        event_id: str | None = None
        if evento_viejo and await call_calendar.mover_evento(
            client_id, evento_viejo, inicio=inicio, fin=fin
        ):
            event_id = evento_viejo
        else:
            if evento_viejo:
                await call_calendar.cancelar_evento(client_id, evento_viejo)
            event_id = await call_calendar.crear_evento(
                client_id, nombre=nombre, empresa=empresa, inicio=inicio, fin=fin
            )
        if event_id is not None:
            await _guardar_evento(client_id, nueva_id, event_id, user_id=uid)
        await avisar_agente_de_llamada_nueva(client_id, nueva_id)

    async with tenant_session(client_id) as session:
        resultado = (
            await session.execute(
                select(ScheduledCall).where(
                    ScheduledCall.id == nueva_id, ScheduledCall.client_id == client_id
                )
            )
        ).scalar_one()
        return ScheduledCallResponse.model_validate(resultado)


@router.post("/{call_id}/complete", response_model=ScheduledCallResponse)
async def complete_call(
    call_id: UUID,
    data: ScheduledCallComplete,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> ScheduledCallResponse:
    """Cierra una llamada que ocurrio, con su resultado y la grabacion si la hay.

    Args:
        call_id: Llamada confirmada o en curso.
        data: Resultado, llamada real (`call_records`) y notas.
        user: Usuario autenticado.

    Returns:
        La llamada cerrada.

    Raises:
        AppException: 404 si no existe; 409 si no se puede completar desde su estado; 400 si la
            grabacion no es de este tenant.
    """
    async with tenant_session(user["client_id"], user_id=_uid(user)) as session:
        llamada = await _llamada_o_404(session, call_id, user, bloquear=True)
        try:
            await completar(
                session,
                llamada,
                outcome=data.outcome,
                ahora=datetime.now(timezone.utc),
                call_record_id=data.call_record_id,
                notes=data.notes,
                user_id=_uid(user),
            )
        except (CallError, EstadoInvalidoError) as exc:
            raise _como_http(exc) from exc
        await session.refresh(llamada)
        return ScheduledCallResponse.model_validate(llamada)


@router.post("/{call_id}/initiate", response_model=ScheduledCallResponse, status_code=202)
async def initiate_call(
    call_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_INITIATE_ROLES)),
) -> ScheduledCallResponse:
    """Inicia ya una llamada con voz IA: el agente habla cuando el lead contesta.

    Solo `supervisor` para arriba y con limite por usuario y hora: llama a una persona. No se llama
    a quien pidio no recibir mas contacto ni a quien no tiene un telefono internacional valido.
    La llamada pasa a `in_progress`; al terminar, el canal de voz la enlaza con su grabacion.

    Args:
        call_id: Llamada de tipo `ai_voice` o `hybrid`, pendiente o confirmada.
        user: Usuario autenticado.

    Returns:
        La llamada en curso (202).

    Raises:
        AppException: 404 si no existe; 409 si no es de voz IA, ya se esta iniciando, no esta
            pendiente o confirmada, o el lead pidio no recibir contacto; 400 si no hay telefono;
            503 si el proveedor no esta configurado o no responde; 502 si lo rechaza.
    """
    client_id: UUID = user["client_id"]
    uid = _uid(user)
    await hit(f"call-initiate:{client_id}:{uid}", INICIAR_POR_USUARIO_Y_HORA, 3600)
    async with tenant_session(client_id) as session:
        existe = (
            await session.execute(
                select(ScheduledCall.id).where(
                    ScheduledCall.id == call_id, ScheduledCall.client_id == client_id
                )
            )
        ).scalar_one_or_none()
        if existe is None:
            raise AppException(
                status_code=404, error_code=NOT_FOUND, message="Llamada no encontrada"
            )
        _, idioma = await datos_del_tenant(session, client_id)
    try:
        await iniciar_llamada_ia(
            client_id, call_id, ahora=datetime.now(timezone.utc), idioma=idioma, user_id=uid
        )
    except CallError as exc:
        raise _como_http(exc) from exc
    async with tenant_session(client_id) as session:
        llamada = (
            await session.execute(
                select(ScheduledCall).where(
                    ScheduledCall.id == call_id, ScheduledCall.client_id == client_id
                )
            )
        ).scalar_one()
        return ScheduledCallResponse.model_validate(llamada)
