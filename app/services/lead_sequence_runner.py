"""Avance de las inscripciones a secuencias: lo que hace la tarea de Beat (Sprint 18, Dev B).

`procesar_tenant()` toma las inscripciones activas a las que les toca, **una por transaccion**:

1. Se bloquea la fila (`FOR UPDATE SKIP LOCKED`): dos workers no envian lo mismo dos veces.
2. El motor puro (`decidir`) dice que toca; aqui se ejecuta (enviar, crear la tarea, esperar...).
3. El avance se registra en la **misma transaccion** que el envio. Si el envio sale bien y el commit
   falla, el reintento puede repetir el mensaje; se elige ese lado (un duplicado es visible, un
   mensaje perdido no), como en `_delivery.py`.

Por que una transaccion por inscripcion y no por lote: un fallo en la quinta no puede deshacer los
cuatro mensajes ya enviados (se repetirian en la siguiente pasada).

Reintentos:

- Fallo **temporal** del proveedor: el paso se reintenta a los `ESPERA_REINTENTO`; tras
  `MAX_FALLOS_POR_PASO` fallos seguidos del mismo paso, sale con `bounced`.
- Fallo **permanente** (numero sin WhatsApp, email invalido): sale con `bounced`.
- Canal sin configurar en el despliegue: sale con `no_channel`.
- Presupuesto de tokens agotado (modo `ai`): se aplaza `ESPERA_PRESUPUESTO`, sin contar como fallo.

Un paso `task` no tiene tabla propia (no existe en el modelo): deja una actividad
`sequence_task` en el historial del lead con el titulo, el plazo y el responsable, y fija
`leads.next_follow_up_at`. Con `pause_until_done` la inscripcion queda pausada hasta que una persona
la reanuda (`POST /leads/{id}/enroll/{seq}/resume`).
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import tenant_session
from app.models.conversation import Conversation
from app.models.lead import Lead
from app.models.lead_activity import (
    ACTIVITY_SEQUENCE_MESSAGE_SENT,
    ACTIVITY_SEQUENCE_SEND_FAILED,
    ACTIVITY_SEQUENCE_TASK,
    LeadActivity,
)
from app.models.lead_sequence import (
    ENROLLMENT_ACTIVE,
    EXIT_BOUNCED,
    EXIT_GDPR,
    EXIT_INVALID_STEP,
    EXIT_NO_CHANNEL,
    LeadSequence,
    LeadSequenceEnrollment,
)
from app.models.message import Message
from app.models.user import User
from app.schemas.lead_sequence import MessageStep, TaskStep, variables_de_plantilla
from app.services import lead_sequence_ai
from app.services.lead_activity import registrar_actividad
from app.services.lead_outreach import (
    CanalNoDisponibleError,
    EnvioPermanenteError,
    EnvioTemporalError,
    con_pie_de_baja,
    datos_del_tenant,
    destino_del_lead,
    enviar_texto,
)
from app.services.lead_sequence_engine import (
    Complete,
    CreateTask,
    Decision,
    Exit,
    Paso,
    SendMessage,
    Wait,
    decidir,
    renderizar_plantilla,
)
from app.services.lead_sequence_timing import (
    VentanaEnvio,
    programar_siguiente,
    ventana_del_tenant,
)
from app.services.lead_sequences import (
    PasosInvalidosError,
    cargar_pasos,
    horas_de_respuesta,
    registrar_avance,
    salir,
    snapshot_del_lead,
    variables_del_lead,
)

logger = logging.getLogger(__name__)

#: Cuanto se espera antes de reintentar un envio que fallo de forma temporal.
ESPERA_REINTENTO = timedelta(minutes=15)
#: Fallos seguidos del mismo paso tras los que el lead sale con `bounced`.
MAX_FALLOS_POR_PASO = 5
#: Cuanto se aplaza un paso `ai` cuando el tenant se quedo sin presupuesto de tokens.
ESPERA_PRESUPUESTO = timedelta(hours=1)

#: Resultados de procesar una inscripcion (para el resumen y los tests).
RES_ENVIADO = "sent"
RES_ESPERA = "waiting"
RES_TAREA = "task"
RES_COMPLETADA = "completed"
RES_SALIDA = "exited"
RES_REINTENTO = "retry"
RES_APLAZADA = "deferred"
RES_OMITIDA = "skipped"


@dataclass
class ResumenPasada:
    """Lo que hizo una pasada sobre un tenant (sin datos personales).

    Attributes:
        procesadas: Inscripciones examinadas.
        por_resultado: Cuantas terminaron en cada `RES_*`.
    """

    procesadas: int = 0
    por_resultado: dict[str, int] = field(default_factory=dict)

    def anotar(self, resultado: str) -> None:
        """Cuenta una inscripcion con su resultado."""
        self.procesadas += 1
        self.por_resultado[resultado] = self.por_resultado.get(resultado, 0) + 1


@dataclass(frozen=True)
class _SecuenciaCargada:
    """Una secuencia ya leida y validada, reutilizada por todas sus inscripciones del lote."""

    activa: bool
    prioridad: tuple[str, ...]
    pasos: tuple[Paso, ...] | None


@dataclass(frozen=True)
class _ContextoTenant:
    """Lo comun a todas las inscripciones de un tenant en una pasada."""

    client_id: UUID
    ventana: VentanaEnvio
    negocio: str | None
    idioma: str
    ahora: datetime


async def _ids_pendientes(
    session: AsyncSession, client_id: UUID, ahora: datetime, limite: int
) -> list[UUID]:
    """Ids de las inscripciones activas a las que les toca, las mas atrasadas primero."""
    filas = await session.execute(
        select(LeadSequenceEnrollment.id)
        .where(
            LeadSequenceEnrollment.client_id == client_id,
            LeadSequenceEnrollment.status == ENROLLMENT_ACTIVE,
            LeadSequenceEnrollment.next_step_at <= ahora,
        )
        .order_by(LeadSequenceEnrollment.next_step_at, LeadSequenceEnrollment.id)
        .limit(limite)
    )
    return list(filas.scalars().all())


async def _cargar_secuencia(
    session: AsyncSession, client_id: UUID, sequence_id: UUID
) -> _SecuenciaCargada | None:
    """Lee y valida una secuencia; `None` si ya no existe."""
    secuencia = (
        await session.execute(
            select(LeadSequence).where(
                LeadSequence.id == sequence_id, LeadSequence.client_id == client_id
            )
        )
    ).scalar_one_or_none()
    if secuencia is None:
        return None
    try:
        pasos: tuple[Paso, ...] | None = tuple(await cargar_pasos(session, client_id, sequence_id))
    except PasosInvalidosError:
        pasos = None
    return _SecuenciaCargada(
        activa=secuencia.is_active,
        prioridad=tuple(secuencia.channel_priority or ()),
        pasos=pasos,
    )


async def _fallos_del_paso(
    session: AsyncSession, enrollment: LeadSequenceEnrollment, posicion: int
) -> int:
    """Cuantos envios fallidos lleva este paso de esta inscripcion."""
    cuenta = (
        await session.execute(
            select(func.count())
            .select_from(LeadActivity)
            .where(
                LeadActivity.client_id == enrollment.client_id,
                LeadActivity.lead_id == enrollment.lead_id,
                LeadActivity.activity_type == ACTIVITY_SEQUENCE_SEND_FAILED,
                LeadActivity.metadata_["enrollment_id"].astext == str(enrollment.id),
                LeadActivity.metadata_["position"].astext == str(posicion),
            )
        )
    ).scalar_one()
    return int(cuenta)


def _anotar_fallo(
    session: AsyncSession,
    enrollment: LeadSequenceEnrollment,
    *,
    posicion: int,
    canal: str,
    codigo: str,
) -> None:
    """Deja el fallo en el historial (codigo, no el detalle del proveedor)."""
    registrar_actividad(
        session,
        client_id=enrollment.client_id,
        lead_id=enrollment.lead_id,
        tipo=ACTIVITY_SEQUENCE_SEND_FAILED,
        sequence_id=enrollment.sequence_id,
        enrollment_id=enrollment.id,
        position=posicion,
        channel=canal,
        code=codigo,
    )


async def _nombre_del_agente(session: AsyncSession, lead: Lead) -> str | None:
    """Nombre de pila del responsable del lead, para `{{agent_name}}`."""
    if lead.assigned_user_id is None:
        return None
    nombre = (
        await session.execute(
            select(User.first_name).where(
                User.id == lead.assigned_user_id, User.client_id == lead.client_id
            )
        )
    ).scalar_one_or_none()
    return str(nombre) if nombre else None


async def _texto_del_mensaje(
    session: AsyncSession,
    lead: Lead,
    paso: MessageStep,
    canal: str,
    ctx: _ContextoTenant,
) -> tuple[str | None, str | None]:
    """El texto del paso: plantilla renderizada o redactado por la IA.

    Returns:
        `(texto, None)` o `(None, motivo)` con `motivo` uno de los `SKIP_*` de la IA.
    """
    if paso.mode == "ai":
        preparado, motivo = await lead_sequence_ai.preparar_mensaje(
            session, lead, paso, canal, negocio=ctx.negocio, idioma=ctx.idioma
        )
        if preparado is None:
            return None, motivo
        return await lead_sequence_ai.redactar(preparado)

    cuerpo = paso.body or ""
    agente = (
        await _nombre_del_agente(session, lead)
        if "agent_name" in variables_de_plantilla(cuerpo)
        else None
    )
    return (
        renderizar_plantilla(
            cuerpo, variables_del_lead(lead, business_name=ctx.negocio, agent_name=agente)
        ),
        None,
    )


def _asunto(paso: MessageStep, lead: Lead, ctx: _ContextoTenant) -> str:
    """Asunto del correo: el del paso renderizado, o el nombre del negocio."""
    if paso.subject:
        asunto = renderizar_plantilla(
            paso.subject, variables_del_lead(lead, business_name=ctx.negocio)
        )
        if asunto:
            return asunto[:200]
    return (ctx.negocio or "Seguimiento")[:200]


async def _guardar_en_conversacion(
    session: AsyncSession, lead: Lead, canal: str, texto: str, externo: str | None
) -> None:
    """Deja el mensaje enviado en la conversacion del contacto por ese canal, si hay una.

    Asi quien atienda el chat ve lo que el lead recibio. Sin contacto enlazado o sin conversacion
    en el canal, no se crea ninguna: una secuencia no abre conversaciones (responde el lead y las
    abre el webhook entrante).
    """
    if lead.contact_id is None:
        return
    conversacion = (
        await session.execute(
            select(Conversation)
            .where(
                Conversation.client_id == lead.client_id,
                Conversation.contact_id == lead.contact_id,
                Conversation.channel == canal,
            )
            .order_by(Conversation.last_message_at.desc().nulls_last())
            .limit(1)
        )
    ).scalar_one_or_none()
    if conversacion is None:
        return
    session.add(
        Message(
            id=uuid4(),
            client_id=lead.client_id,
            conversation_id=conversacion.id,
            direction="outbound",
            message_type="text",
            content=texto,
            external_message_id=externo,
            sender_type="bot",
        )
    )
    conversacion.last_message_at = datetime.now(timezone.utc)


async def _enviar_paso(
    session: AsyncSession,
    enrollment: LeadSequenceEnrollment,
    lead: Lead,
    decision: Decision,
    accion: SendMessage,
    ctx: _ContextoTenant,
) -> str:
    """Ejecuta un paso `message` y registra el avance o el fallo.

    Returns:
        Un `RES_*`.
    """
    paso, canal, posicion = accion.step, accion.channel, accion.position
    texto, motivo = await _texto_del_mensaje(session, lead, paso, canal, ctx)
    if texto is None:
        if motivo == lead_sequence_ai.SKIP_SUPPRESSED:
            salir(session, enrollment, EXIT_GDPR, ahora=ctx.ahora)
            return RES_SALIDA
        if motivo == lead_sequence_ai.SKIP_NO_BUDGET:
            enrollment.next_step_at = ctx.ahora + ESPERA_PRESUPUESTO
            return RES_APLAZADA
        return await _fallo_temporal(
            session, enrollment, posicion, canal, motivo or lead_sequence_ai.SKIP_MODEL_FAILED, ctx
        )

    destino = await destino_del_lead(session, lead, canal)
    if destino is None:
        _anotar_fallo(session, enrollment, posicion=posicion, canal=canal, codigo="no_destination")
        salir(session, enrollment, EXIT_BOUNCED, ahora=ctx.ahora)
        return RES_SALIDA

    cuerpo = con_pie_de_baja(texto, ctx.idioma)
    try:
        externo = await enviar_texto(
            canal=canal,
            destino=destino,
            texto=cuerpo,
            asunto=_asunto(paso, lead, ctx) if canal == "email" else None,
            plantilla_whatsapp=paso.whatsapp_template,
            idioma=ctx.idioma,
        )
    except CanalNoDisponibleError as exc:
        _anotar_fallo(session, enrollment, posicion=posicion, canal=canal, codigo=exc.code)
        salir(session, enrollment, EXIT_NO_CHANNEL, ahora=ctx.ahora)
        return RES_SALIDA
    except EnvioPermanenteError as exc:
        _anotar_fallo(session, enrollment, posicion=posicion, canal=canal, codigo=exc.code)
        salir(session, enrollment, EXIT_BOUNCED, ahora=ctx.ahora)
        return RES_SALIDA
    except EnvioTemporalError as exc:
        return await _fallo_temporal(session, enrollment, posicion, canal, exc.code, ctx)

    await _guardar_en_conversacion(session, lead, canal, cuerpo, externo)
    registrar_actividad(
        session,
        client_id=enrollment.client_id,
        lead_id=lead.id,
        tipo=ACTIVITY_SEQUENCE_MESSAGE_SENT,
        sequence_id=enrollment.sequence_id,
        enrollment_id=enrollment.id,
        position=posicion,
        channel=canal,
        mode=paso.mode,
    )
    registrar_avance(session, enrollment, decision, ahora=ctx.ahora, ventana=ctx.ventana)
    return RES_ENVIADO


async def _fallo_temporal(
    session: AsyncSession,
    enrollment: LeadSequenceEnrollment,
    posicion: int,
    canal: str,
    codigo: str,
    ctx: _ContextoTenant,
) -> str:
    """Anota el fallo y reintenta el paso mas tarde; al quinto seguido, sale con `bounced`."""
    previos = await _fallos_del_paso(session, enrollment, posicion)
    _anotar_fallo(session, enrollment, posicion=posicion, canal=canal, codigo=codigo)
    if previos + 1 >= MAX_FALLOS_POR_PASO:
        salir(session, enrollment, EXIT_BOUNCED, ahora=ctx.ahora)
        return RES_SALIDA
    enrollment.next_step_at = ctx.ahora + ESPERA_REINTENTO
    return RES_REINTENTO


def _crear_tarea(
    session: AsyncSession,
    enrollment: LeadSequenceEnrollment,
    lead: Lead,
    decision: Decision,
    accion: CreateTask,
    ctx: _ContextoTenant,
) -> None:
    """Deja la tarea en el historial, fija el proximo seguimiento y avanza la inscripcion."""
    paso: TaskStep = accion.step
    vence = ctx.ahora + timedelta(hours=paso.due_in_hours)
    responsable = lead.assigned_user_id if paso.assign_to == "lead_owner" else None
    registrar_actividad(
        session,
        client_id=enrollment.client_id,
        lead_id=lead.id,
        tipo=ACTIVITY_SEQUENCE_TASK,
        sequence_id=enrollment.sequence_id,
        enrollment_id=enrollment.id,
        position=accion.position,
        task_title=paso.title,
        assigned_user_id=str(responsable) if responsable else None,
        due_at=vence.isoformat(),
        pause_until_done=paso.pause_until_done,
    )
    if lead.next_follow_up_at is None or lead.next_follow_up_at < ctx.ahora:
        lead.next_follow_up_at = vence
    registrar_avance(session, enrollment, decision, ahora=ctx.ahora, ventana=ctx.ventana)


async def procesar_inscripcion(
    session: AsyncSession,
    enrollment: LeadSequenceEnrollment,
    ctx: _ContextoTenant,
    cache: dict[UUID, _SecuenciaCargada | None],
) -> str:
    """Hace lo que toca a una inscripcion ya bloqueada.

    Args:
        session: Sesion del tenant, con la fila de la inscripcion bloqueada.
        enrollment: La inscripcion (activa y vencida).
        ctx: Lo comun del tenant en esta pasada.
        cache: Secuencias ya leidas en este lote.

    Returns:
        Un `RES_*`.
    """
    if enrollment.sequence_id not in cache:
        cache[enrollment.sequence_id] = await _cargar_secuencia(
            session, enrollment.client_id, enrollment.sequence_id
        )
    secuencia = cache[enrollment.sequence_id]
    if secuencia is None or secuencia.pasos is None:
        salir(session, enrollment, EXIT_INVALID_STEP, ahora=ctx.ahora)
        return RES_SALIDA

    lead = (
        await session.execute(
            select(Lead).where(
                Lead.id == enrollment.lead_id, Lead.client_id == enrollment.client_id
            )
        )
    ).scalar_one_or_none()
    if lead is None:
        salir(session, enrollment, EXIT_GDPR, ahora=ctx.ahora)
        return RES_SALIDA

    snapshot = await snapshot_del_lead(session, lead, desde=enrollment.created_at)
    decision = decidir(
        pasos=secuencia.pasos,
        current_step=enrollment.current_step,
        steps_executed=enrollment.steps_executed,
        lead=snapshot,
        channel_priority=secuencia.prioridad,
        secuencia_activa=secuencia.activa,
    )
    accion = decision.action

    if isinstance(accion, Exit):
        registrar_avance(session, enrollment, decision, ahora=ctx.ahora, ventana=ctx.ventana)
        return RES_SALIDA
    if isinstance(accion, Complete):
        registrar_avance(session, enrollment, decision, ahora=ctx.ahora, ventana=ctx.ventana)
        return RES_COMPLETADA
    if isinstance(accion, Wait):
        horas = (
            await horas_de_respuesta(session, lead, ctx.ventana.tz)
            if accion.step.smart_timing
            else []
        )
        proximo = programar_siguiente(ctx.ahora, accion.step, ctx.ventana, horas)
        registrar_avance(
            session,
            enrollment,
            decision,
            ahora=ctx.ahora,
            ventana=ctx.ventana,
            next_step_at=proximo,
        )
        return RES_ESPERA
    if isinstance(accion, CreateTask):
        _crear_tarea(session, enrollment, lead, decision, accion, ctx)
        return RES_TAREA
    return await _enviar_paso(session, enrollment, lead, decision, accion, ctx)


async def _procesar_una(
    client_id: UUID,
    enrollment_id: UUID,
    ctx: _ContextoTenant,
    cache: dict[UUID, _SecuenciaCargada | None],
) -> str:
    """Una inscripcion en su propia transaccion: bloqueo, ejecucion y avance."""
    async with tenant_session(client_id) as session:
        fila = (
            await session.execute(
                select(LeadSequenceEnrollment)
                .where(
                    LeadSequenceEnrollment.id == enrollment_id,
                    LeadSequenceEnrollment.client_id == client_id,
                    LeadSequenceEnrollment.status == ENROLLMENT_ACTIVE,
                    LeadSequenceEnrollment.next_step_at <= ctx.ahora,
                )
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if fila is None:
            return RES_OMITIDA  # otro worker la tiene, o ya avanzo
        resultado = await procesar_inscripcion(session, fila, ctx, cache)
        await session.flush()
        return resultado


async def procesar_tenant(
    client_id: UUID, *, ahora: datetime | None = None, limite: int | None = None
) -> ResumenPasada:
    """Avanza las inscripciones vencidas de un tenant.

    Args:
        client_id: Tenant.
        ahora: Instante de referencia (por defecto, ahora mismo).
        limite: Maximo de inscripciones (por defecto, `LEAD_SEQUENCE_BATCH_SIZE`).

    Returns:
        El resumen de la pasada. Un fallo en una inscripcion se registra y no detiene las demas.
    """
    ahora = ahora or datetime.now(timezone.utc)
    limite = limite or get_settings().LEAD_SEQUENCE_BATCH_SIZE
    resumen = ResumenPasada()
    async with tenant_session(client_id) as session:
        ids = await _ids_pendientes(session, client_id, ahora, limite)
        if not ids:
            return resumen
        ventana = await ventana_del_tenant(session, client_id)
        negocio, idioma = await datos_del_tenant(session, client_id)
    ctx = _ContextoTenant(client_id, ventana, negocio, idioma, ahora)
    cache: dict[UUID, _SecuenciaCargada | None] = {}
    for enrollment_id in ids:
        try:
            resumen.anotar(await _procesar_una(client_id, enrollment_id, ctx, cache))
        except Exception:
            logger.exception(
                "Fallo avanzando la inscripcion %s (client_id=%s)", enrollment_id, client_id
            )
            resumen.anotar("error")
    return resumen
