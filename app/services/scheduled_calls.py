"""Llamadas agendadas contra la base (Sprint 19, slice Dev A, ADR-087).

Agendar (sin doble reserva de la misma persona), huecos libres, cambios de estado,
reprogramar, recordatorios pendientes para la tarea de Celery (Dev B) y lo que el RGPD necesita.
Las reglas puras estan en `app/services/call_scheduling.py`. Todas las funciones reciben una
sesion con el contexto del tenant fijado y filtran ademas por `client_id` explicito.

**Doble reserva:** la base no puede impedir que dos llamadas de la misma persona se solapen sin
la extension `btree_gist`. Se serializa por usuario con `pg_advisory_xact_lock` (dura lo que la
transaccion) y se comprueba el solape despues de tomar el bloqueo: dos peticiones a la vez para
el mismo hueco, una gana y la otra recibe `overlap`.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call_record import CallRecord
from app.models.deal import Deal
from app.models.lead import Lead
from app.models.lead_activity import ACTIVITY_CALL_SCHEDULED, ACTIVITY_CALL_STATUS_CHANGED
from app.models.scheduled_call import (
    CALL_CANCELLED,
    CALL_COMPLETED,
    CALL_CONFIRMED,
    CALL_RESCHEDULED,
    UPCOMING_STATUSES,
    ScheduledCall,
)
from app.schemas.deal import ScheduledCallCreate, ScheduledCallReschedule
from app.services.call_scheduling import (
    Intervalo,
    huecos_libres,
    primer_solape,
    recordatorio_pendiente,
    validar_estado,
)
from app.services.lead_activity import registrar_actividad
from app.services.lead_privacy import lead_esta_anonimizado
from app.services.lead_sequence_timing import ventana_del_tenant
from app.services.tenant_refs import existe_en_tenant, usuario_asignable

#: Duracion maxima de una llamada (el CHECK de la tabla): acota la busqueda de solapes.
_MAX_DURACION = timedelta(minutes=480)


class CallError(Exception):
    """Operacion no permitida sobre una llamada agendada.

    Attributes:
        code: Codigo estable para la API.
    """

    def __init__(self, code: str, message: str) -> None:
        """Crea el error.

        Args:
            code: Codigo estable (`overlap`, `in_the_past`, `lead_unavailable`...).
            message: Descripcion (sin datos de la persona).
        """
        super().__init__(message)
        self.code = code


async def _bloquear_agenda(session: AsyncSession, client_id: UUID, user_id: UUID) -> None:
    """Serializa, hasta el fin de la transaccion, las reservas de una persona."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:clave))"),
        {"clave": f"scheduled_calls:{client_id}:{user_id}"},
    )


async def ocupados_del_usuario(
    session: AsyncSession,
    client_id: UUID,
    user_id: UUID,
    *,
    desde: datetime,
    hasta: datetime,
    excluir: UUID | None = None,
) -> list[Intervalo]:
    """Tramos ocupados por las llamadas pendientes o confirmadas de una persona.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        user_id: Quien llama.
        desde: Inicio del rango.
        hasta: Fin del rango.
        excluir: Una llamada a ignorar (la que se reprograma).

    Returns:
        Los tramos que tocan el rango.
    """
    consulta = select(ScheduledCall.scheduled_at, ScheduledCall.duration_minutes).where(
        ScheduledCall.client_id == client_id,
        ScheduledCall.assigned_user_id == user_id,
        ScheduledCall.status.in_(UPCOMING_STATUSES),
        # Una llamada que empezo hasta 8 h antes de `desde` aun puede ocupar el rango.
        ScheduledCall.scheduled_at > desde - _MAX_DURACION,
        ScheduledCall.scheduled_at < hasta,
    )
    if excluir is not None:
        consulta = consulta.where(ScheduledCall.id != excluir)
    filas = (await session.execute(consulta)).tuples().all()
    return [
        tramo
        for tramo in (Intervalo.de_llamada(inicio, minutos) for inicio, minutos in filas)
        if tramo.solapa(Intervalo(desde, hasta))
    ]


async def huecos_para(
    session: AsyncSession,
    client_id: UUID,
    *,
    user_id: UUID | None,
    desde: datetime,
    hasta: datetime,
    duracion_minutos: int,
    ahora: datetime,
) -> list[datetime]:
    """Huecos libres para agendar, en el horario del negocio.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        user_id: Quien llamaria (`None` en `ai_voice`: solo cuenta el horario).
        desde: Inicio de la busqueda.
        hasta: Fin de la busqueda.
        duracion_minutos: Duracion.
        ahora: Instante actual.

    Returns:
        Inicios en UTC.
    """
    ventana = await ventana_del_tenant(session, client_id)
    ocupados = (
        await ocupados_del_usuario(session, client_id, user_id, desde=desde, hasta=hasta)
        if user_id is not None
        else []
    )
    return huecos_libres(
        desde=desde,
        hasta=hasta,
        duracion_minutos=duracion_minutos,
        ventana=ventana,
        ocupados=ocupados,
        ahora=ahora,
    )


async def _comprobar_hueco(
    session: AsyncSession,
    client_id: UUID,
    user_id: UUID | None,
    inicio: datetime,
    minutos: int,
    *,
    excluir: UUID | None = None,
) -> None:
    """Bloquea la agenda de la persona y falla si el tramo pisa otra llamada suya."""
    if user_id is None:
        return
    await _bloquear_agenda(session, client_id, user_id)
    propuesto = Intervalo.de_llamada(inicio, minutos)
    ocupados = await ocupados_del_usuario(
        session, client_id, user_id, desde=propuesto.inicio, hasta=propuesto.fin, excluir=excluir
    )
    if primer_solape(propuesto, ocupados) is not None:
        raise CallError("overlap", "Esa persona ya tiene una llamada en ese horario")


async def agendar(
    session: AsyncSession,
    lead: Lead,
    datos: ScheduledCallCreate,
    *,
    ahora: datetime,
    user_id: UUID | None = None,
) -> ScheduledCall:
    """Agenda una llamada con un lead.

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead (ya cargado; `datos.lead_id` debe ser el suyo).
        datos: Datos validados.
        ahora: Instante actual.
        user_id: Quien la agenda.

    Returns:
        La llamada (con `id`).

    Raises:
        CallError: `lead_mismatch`, `lead_unavailable` (borrado o suprimido), `in_the_past`,
            `deal_mismatch` (el deal es de otro lead), `user_unavailable` (quien llama no es un
            usuario activo del tenant) u `overlap`.
    """
    if datos.lead_id != lead.id:
        raise CallError("lead_mismatch", "La llamada no corresponde a ese lead")
    if lead.deleted_at is not None or lead_esta_anonimizado(lead):
        raise CallError("lead_unavailable", "El lead esta borrado o suprimido")
    if datos.scheduled_at <= ahora:
        raise CallError("in_the_past", "La llamada debe agendarse en el futuro")
    if datos.deal_id is not None:
        del_lead = (
            await session.execute(
                select(func.count())
                .select_from(Deal)
                .where(
                    Deal.client_id == lead.client_id,
                    Deal.id == datos.deal_id,
                    Deal.lead_id == lead.id,
                )
            )
        ).scalar_one()
        if not del_lead:
            raise CallError("deal_mismatch", "El deal no es de ese lead")
    if datos.assigned_user_id is not None and not await usuario_asignable(
        session, lead.client_id, datos.assigned_user_id
    ):
        raise CallError(
            "user_unavailable", "El usuario no existe, esta desactivado o no atiende leads"
        )
    zona = datos.timezone or (await ventana_del_tenant(session, lead.client_id)).tz.key
    await _comprobar_hueco(
        session, lead.client_id, datos.assigned_user_id, datos.scheduled_at, datos.duration_minutes
    )
    llamada = ScheduledCall(
        client_id=lead.client_id,
        lead_id=lead.id,
        deal_id=datos.deal_id,
        assigned_user_id=datos.assigned_user_id,
        call_type=datos.call_type,
        status="pending",
        scheduled_at=datos.scheduled_at,
        duration_minutes=datos.duration_minutes,
        timezone=zona,
        ai_voice_provider=datos.ai_voice_provider,
        ai_voice_config=datos.ai_voice_config,
        notes=datos.notes,
        created_at=ahora,
    )
    session.add(llamada)
    await session.flush()
    registrar_actividad(
        session,
        client_id=lead.client_id,
        lead_id=lead.id,
        tipo=ACTIVITY_CALL_SCHEDULED,
        user_id=user_id,
        call_id=llamada.id,
        call_type=llamada.call_type,
        scheduled_at=llamada.scheduled_at.isoformat(),
    )
    return llamada


def cambiar_estado(
    session: AsyncSession,
    llamada: ScheduledCall,
    nuevo: str,
    *,
    ahora: datetime,
    user_id: UUID | None = None,
    outcome: str | None = None,
    call_record_id: UUID | None = None,
) -> None:
    """Cambia el estado de una llamada (la escribe el `flush` de la sesion).

    Args:
        session: Sesion con el contexto del tenant fijado.
        llamada: Llamada.
        nuevo: Estado nuevo.
        ahora: Instante del cambio.
        user_id: Quien lo cambia.
        outcome: Resultado (al completar o marcar `no_show`).
        call_record_id: La llamada real en `call_records`; **ya comprobada** en el tenant (usar
            `completar()`, que lo hace).

    Raises:
        EstadoInvalidoError: Si la transicion no esta permitida.
    """
    anterior = llamada.status
    validar_estado(anterior, nuevo)
    llamada.status = nuevo
    if nuevo == CALL_CONFIRMED:
        llamada.confirmed_at = ahora
    elif nuevo == CALL_COMPLETED:
        llamada.completed_at = ahora
    elif nuevo == CALL_CANCELLED:
        llamada.cancelled_at = ahora
    if outcome is not None:
        llamada.outcome = outcome
    if call_record_id is not None:
        llamada.call_record_id = call_record_id
    registrar_actividad(
        session,
        client_id=llamada.client_id,
        lead_id=llamada.lead_id,
        tipo=ACTIVITY_CALL_STATUS_CHANGED,
        user_id=user_id,
        call_id=llamada.id,
        status_from=anterior,
        status_to=nuevo,
        outcome=outcome,
    )


async def completar(
    session: AsyncSession,
    llamada: ScheduledCall,
    *,
    outcome: str,
    ahora: datetime,
    call_record_id: UUID | None = None,
    notes: str | None = None,
    user_id: UUID | None = None,
) -> None:
    """Cierra una llamada que ocurrio, con su resultado y la llamada real si la hay.

    Args:
        session: Sesion con el contexto del tenant fijado.
        llamada: Llamada confirmada o en curso.
        outcome: Resultado (`CallOutcome`).
        ahora: Instante del cierre.
        call_record_id: La llamada real en `call_records`.
        notes: Notas (sustituyen a las anteriores si se envian).
        user_id: Quien la cierra.

    Raises:
        CallError: `call_record_unavailable` si la llamada real no es de este tenant (la FK no
            mira la RLS).
        EstadoInvalidoError: Si la llamada no se puede completar desde su estado.
    """
    if call_record_id is not None and not await existe_en_tenant(
        session, CallRecord, llamada.client_id, call_record_id
    ):
        raise CallError("call_record_unavailable", "Esa grabacion de llamada no existe")
    cambiar_estado(
        session,
        llamada,
        CALL_COMPLETED,
        ahora=ahora,
        user_id=user_id,
        outcome=outcome,
        call_record_id=call_record_id,
    )
    if notes is not None:
        llamada.notes = notes
    await session.flush()


async def reprogramar(
    session: AsyncSession,
    llamada: ScheduledCall,
    datos: ScheduledCallReschedule,
    *,
    ahora: datetime,
    user_id: UUID | None = None,
) -> ScheduledCall:
    """Mueve una llamada a otro momento: la vieja queda `rescheduled` y nace una nueva.

    Se crea una fila nueva (y no se edita la hora) para que los recordatorios ya enviados no
    impidan avisar de la hora nueva y quede el rastro del cambio.

    Args:
        session: Sesion con el contexto del tenant fijado.
        llamada: Llamada pendiente o confirmada.
        datos: Nuevo inicio (y duracion).
        ahora: Instante actual.
        user_id: Quien la mueve.

    Returns:
        La llamada nueva.

    Raises:
        EstadoInvalidoError: Si la llamada ya no se puede reprogramar.
        CallError: `in_the_past` u `overlap`.
    """
    validar_estado(llamada.status, CALL_RESCHEDULED)
    if datos.scheduled_at <= ahora:
        raise CallError("in_the_past", "La llamada debe agendarse en el futuro")
    minutos = datos.duration_minutes or llamada.duration_minutes
    await _comprobar_hueco(
        session,
        llamada.client_id,
        llamada.assigned_user_id,
        datos.scheduled_at,
        minutos,
        excluir=llamada.id,
    )
    cambiar_estado(session, llamada, CALL_RESCHEDULED, ahora=ahora, user_id=user_id)
    nueva = ScheduledCall(
        client_id=llamada.client_id,
        lead_id=llamada.lead_id,
        deal_id=llamada.deal_id,
        assigned_user_id=llamada.assigned_user_id,
        call_type=llamada.call_type,
        status="pending",
        scheduled_at=datos.scheduled_at,
        duration_minutes=minutos,
        timezone=llamada.timezone,
        ai_voice_provider=llamada.ai_voice_provider,
        ai_voice_config=dict(llamada.ai_voice_config or {}),
        notes=llamada.notes,
        created_at=ahora,
    )
    session.add(nueva)
    await session.flush()
    registrar_actividad(
        session,
        client_id=nueva.client_id,
        lead_id=nueva.lead_id,
        tipo=ACTIVITY_CALL_SCHEDULED,
        user_id=user_id,
        call_id=nueva.id,
        call_type=nueva.call_type,
        scheduled_at=nueva.scheduled_at.isoformat(),
        rescheduled_from=llamada.id,
    )
    return nueva


async def recordatorios_pendientes(
    session: AsyncSession, client_id: UUID, ahora: datetime, *, limite: int = 200
) -> list[tuple[ScheduledCall, str]]:
    """Llamadas a las que toca enviar un recordatorio ya, bloqueadas para este worker.

    `FOR UPDATE SKIP LOCKED`: dos workers se reparten las filas en vez de avisar dos veces.
    Quien llama envia el aviso y luego `call_scheduling.marcar_recordatorio()` en la misma
    transaccion.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        ahora: Instante actual.
        limite: Maximo de filas.

    Returns:
        `(llamada, "24h" | "1h")`, de la mas proxima a la menos.
    """
    filas = await session.execute(
        select(ScheduledCall)
        .where(
            ScheduledCall.client_id == client_id,
            ScheduledCall.status.in_(UPCOMING_STATUSES),
            ScheduledCall.scheduled_at > ahora,
            ScheduledCall.scheduled_at <= ahora + timedelta(hours=24),
        )
        .order_by(ScheduledCall.scheduled_at, ScheduledCall.id)
        .limit(limite)
        .with_for_update(skip_locked=True)
    )
    pendientes = []
    for llamada in filas.scalars().all():
        cual = recordatorio_pendiente(llamada, ahora)
        if cual is not None:
            pendientes.append((llamada, cual))
    return pendientes


def llamada_a_dict(llamada: ScheduledCall) -> dict[str, Any]:
    """Una llamada para el export RGPD (la transcripcion va con `call_records`).

    Args:
        llamada: Llamada.

    Returns:
        Sus datos, serializables.
    """
    return {
        "id": str(llamada.id),
        "call_type": llamada.call_type,
        "status": llamada.status,
        "scheduled_at": llamada.scheduled_at.isoformat(),
        "duration_minutes": llamada.duration_minutes,
        "timezone": llamada.timezone,
        "outcome": llamada.outcome,
        "notes": llamada.notes,
        "call_record_id": str(llamada.call_record_id) if llamada.call_record_id else None,
    }


async def llamadas_para_export(
    session: AsyncSession, client_id: UUID, lead_ids: Sequence[UUID]
) -> dict[UUID, list[dict[str, Any]]]:
    """Las llamadas agendadas de varios leads, para el export RGPD.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        lead_ids: Leads del titular.

    Returns:
        Lead -> sus llamadas (por fecha).
    """
    resultado: dict[UUID, list[dict[str, Any]]] = {lead_id: [] for lead_id in lead_ids}
    if not lead_ids:
        return resultado
    filas = await session.execute(
        select(ScheduledCall)
        .where(ScheduledCall.client_id == client_id, ScheduledCall.lead_id.in_(list(lead_ids)))
        .order_by(ScheduledCall.scheduled_at, ScheduledCall.id)
    )
    for llamada in filas.scalars().all():
        resultado[llamada.lead_id].append(llamada_a_dict(llamada))
    return resultado


async def suprimir_llamadas(
    session: AsyncSession, client_id: UUID, lead_ids: Sequence[UUID], *, ahora: datetime
) -> int:
    """Supresion RGPD: cancela las llamadas futuras y borra el texto libre de todas.

    A quien pidio la supresion no se le llama. La transcripcion y el audio estan en
    `call_records`, cuyo propio camino de supresion (el del contacto) ya los cubre.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        lead_ids: Leads suprimidos.
        ahora: Instante de la supresion.

    Returns:
        Llamadas futuras canceladas.
    """
    if not lead_ids:
        return 0
    llamadas = (
        (
            await session.execute(
                select(ScheduledCall).where(
                    ScheduledCall.client_id == client_id, ScheduledCall.lead_id.in_(list(lead_ids))
                )
            )
        )
        .scalars()
        .all()
    )
    canceladas = 0
    for llamada in llamadas:
        if llamada.status in UPCOMING_STATUSES:
            llamada.status = CALL_CANCELLED
            llamada.cancelled_at = ahora
            canceladas += 1
        llamada.notes = None
        llamada.ai_voice_config = {}
    await session.flush()
    return canceladas
