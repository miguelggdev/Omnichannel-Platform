"""Secuencias de follow-up contra la base (Sprint 18, slice Dev A, ADR-086).

El motor (`lead_sequence_engine.decidir`) es puro; este modulo es lo que lo rodea: guardar una
secuencia, inscribir y sacar leads, leer la foto del lead que el motor necesita y aplicar su
decision. La tarea de Celery de Dev B (`tasks.lead_sequences.process_due_enrollments`) queda en:

    for inscripcion in await inscripciones_pendientes(session, client_id, ahora):
        pasos = await cargar_pasos(session, client_id, inscripcion.sequence_id)
        lead = await snapshot_del_lead(session, lead_row, desde=inscripcion.created_at)
        decision = decidir(pasos=pasos, current_step=inscripcion.current_step, ...)
        ...ejecutar decision.action (enviar / crear tarea)...
        await registrar_avance(session, inscripcion, decision, ahora=ahora, next_step_at=...)

Todas las funciones reciben una sesion con el contexto del tenant fijado (`tenant_session`) y
filtran ademas por `client_id` explicito, como el resto del modulo de leads.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import and_, delete, exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact_identifier import ContactIdentifier
from app.models.conversation import Conversation
from app.models.lead import Lead
from app.models.lead_activity import (
    ACTIVITY_EMAIL_OPENED,
    ACTIVITY_EMAIL_REPLIED,
    ACTIVITY_INBOUND_MESSAGE,
    ACTIVITY_LINK_CLICKED,
    ACTIVITY_SEQUENCE_COMPLETED,
    ACTIVITY_SEQUENCE_ENROLLED,
    ACTIVITY_SEQUENCE_EXITED,
    LeadActivity,
)
from app.models.lead_pipeline_stage import LeadPipelineStage
from app.models.lead_sequence import (
    ENROLLMENT_ACTIVE,
    ENROLLMENT_COMPLETED,
    ENROLLMENT_EXITED,
    ENROLLMENT_PAUSED,
    LIVE_STATUSES,
    OPT_OUT_EXITS,
    LeadSequence,
    LeadSequenceEnrollment,
    LeadSequenceStep,
)
from app.models.lead_source import LeadSource
from app.models.message import Message
from app.schemas.lead_sequence import (
    OUTREACH_CHANNELS,
    SequenceDefinition,
    TriggerConditions,
    TriggerEvent,
    paso_a_fila,
    paso_desde_fila,
    validar_grafo,
)
from app.services.lead_activity import registrar_actividad
from app.services.lead_privacy import lead_esta_anonimizado
from app.services.lead_sequence_engine import (
    Complete,
    CreateTask,
    Decision,
    Exit,
    LeadSnapshot,
    Paso,
    Wait,
    motivo_de_bloqueo,
)
from app.services.lead_sequence_timing import VentanaEnvio, siguiente_apertura, ventana_del_tenant

#: Lo que la foto del lead lee de su historial (`lead_activities`).
_SENALES_EN_HISTORIAL = (
    ACTIVITY_EMAIL_OPENED,
    ACTIVITY_LINK_CLICKED,
    ACTIVITY_EMAIL_REPLIED,
    ACTIVITY_INBOUND_MESSAGE,
)

#: Respuestas recientes del lead que se miran para el smart timing.
_RESPUESTAS_PARA_TIMING = 50


class SecuenciaError(Exception):
    """Operacion no permitida sobre una secuencia o una inscripcion.

    Attributes:
        code: Codigo estable para la API (`already_enrolled`, `sequence_inactive`...).
    """

    def __init__(self, code: str, message: str) -> None:
        """Crea el error.

        Args:
            code: Codigo estable.
            message: Descripcion para el usuario (sin datos del lead).
        """
        super().__init__(message)
        self.code = code


class YaInscritoError(SecuenciaError):
    """El lead ya tiene una inscripcion viva en esa secuencia."""

    def __init__(self) -> None:
        """Crea el error con su codigo."""
        super().__init__("already_enrolled", "El lead ya esta en esta secuencia")


class PasosInvalidosError(SecuenciaError):
    """Los pasos guardados no forman una secuencia valida (editados a mano en la base)."""

    def __init__(self, detalle: str) -> None:
        """Crea el error.

        Args:
            detalle: Que falla.
        """
        super().__init__("invalid_steps", f"Pasos de la secuencia no validos: {detalle}")


# ─── Secuencias y pasos ─────────────────────────────────────────────────────────────────────


def _filas_de_pasos(
    client_id: UUID, sequence_id: UUID, definicion: SequenceDefinition
) -> list[LeadSequenceStep]:
    """Los pasos de una definicion como filas, con su posicion."""
    filas = []
    for posicion, paso in enumerate(definicion.steps, start=1):
        step_type, config = paso_a_fila(paso)
        filas.append(
            LeadSequenceStep(
                client_id=client_id,
                sequence_id=sequence_id,
                position=posicion,
                step_type=step_type,
                config=config,
            )
        )
    return filas


async def crear_secuencia(
    session: AsyncSession,
    client_id: UUID,
    definicion: SequenceDefinition,
    *,
    user_id: UUID | None = None,
) -> LeadSequence:
    """Guarda una secuencia y sus pasos.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        definicion: Secuencia ya validada (incluido el grafo).
        user_id: Quien la crea.

    Returns:
        La secuencia (con `id`).

    Raises:
        SecuenciaError: `duplicate_name` si el tenant ya tiene una con ese nombre.
    """
    secuencia = LeadSequence(
        client_id=client_id,
        name=definicion.name,
        description=definicion.description,
        trigger_conditions=definicion.trigger_conditions.model_dump(mode="json"),
        channel_priority=list(definicion.channel_priority),
        created_by_user_id=user_id,
    )
    try:
        async with session.begin_nested():
            session.add(secuencia)
            await session.flush()
    except IntegrityError as exc:
        if not _viola(exc, "uq_lead_sequence_name"):
            raise
        raise SecuenciaError("duplicate_name", "Ya existe una secuencia con ese nombre") from exc
    session.add_all(_filas_de_pasos(client_id, secuencia.id, definicion))
    await session.flush()
    return secuencia


async def reemplazar_pasos(
    session: AsyncSession, client_id: UUID, secuencia: LeadSequence, definicion: SequenceDefinition
) -> None:
    """Sustituye los pasos de una secuencia por los de `definicion`.

    Las inscripciones vivas siguen por la **posicion** en la que iban: si el cambio acorta la
    secuencia, terminan en su siguiente turno; si reordena, continuan por el paso que ahora ocupa
    esa posicion. Es lo esperable al editar una campana en marcha; el panel (Dev B) debe avisarlo.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        secuencia: Secuencia a editar.
        definicion: Nueva definicion (se usan sus pasos).
    """
    await session.execute(
        delete(LeadSequenceStep).where(
            LeadSequenceStep.client_id == client_id,
            LeadSequenceStep.sequence_id == secuencia.id,
        )
    )
    session.add_all(_filas_de_pasos(client_id, secuencia.id, definicion))
    await session.flush()


async def cargar_pasos(session: AsyncSession, client_id: UUID, sequence_id: UUID) -> list[Paso]:
    """Los pasos de una secuencia, en orden y validados.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        sequence_id: Secuencia.

    Returns:
        Los pasos (posicion = indice + 1).

    Raises:
        PasosInvalidosError: Si una fila no valida, hay huecos en las posiciones o el grafo no es
            ejecutable. Quien avanza la inscripcion debe sacarla con `EXIT_INVALID_STEP`.
    """
    filas = (
        await session.execute(
            select(LeadSequenceStep)
            .where(
                LeadSequenceStep.client_id == client_id,
                LeadSequenceStep.sequence_id == sequence_id,
            )
            .order_by(LeadSequenceStep.position)
        )
    ).scalars()
    pasos: list[Paso] = []
    for esperada, fila in enumerate(filas, start=1):
        if fila.position != esperada:
            raise PasosInvalidosError(f"falta el paso {esperada}")
        try:
            pasos.append(paso_desde_fila(fila.step_type, fila.config))
        except ValidationError as exc:
            raise PasosInvalidosError(f"el paso {fila.position} no valida") from exc
    try:
        validar_grafo(pasos)
    except ValueError as exc:
        raise PasosInvalidosError(str(exc)) from exc
    return pasos


# ─── Inscripcion y salida ───────────────────────────────────────────────────────────────────


def _viola(exc: IntegrityError, restriccion: str) -> bool:
    """Si el error de integridad es la violacion de esa restriccion concreta.

    Un `IntegrityError` tambien puede ser una FK (un usuario o un lead borrado a la vez) o un
    CHECK: traducirlo todo a "ya inscrito" o "nombre repetido" esconderia el fallo real.
    """
    return restriccion in str(exc.orig)


def motivo_para_no_inscribir(lead: Lead, secuencia: LeadSequence) -> str | None:
    """Por que no se puede inscribir a un lead, como codigo `EXIT_*`, o `None` si se puede.

    Delega en `motivo_de_bloqueo` del motor: inscribir y avanzar aplican la misma regla.

    Args:
        lead: Lead.
        secuencia: Secuencia.

    Returns:
        El codigo, o `None`.
    """
    foto = LeadSnapshot(
        status=lead.status,
        deleted=lead.deleted_at is not None,
        anonymized=lead_esta_anonimizado(lead),
    )
    return motivo_de_bloqueo(foto, secuencia.is_active)


async def se_dio_de_baja(session: AsyncSession, lead: Lead) -> bool:
    """Si el lead salio alguna vez de una secuencia pidiendo no recibir mas mensajes.

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead.

    Returns:
        `True` si tiene alguna inscripcion cerrada con un motivo de `OPT_OUT_EXITS`.
    """
    return bool(
        (
            await session.execute(
                select(
                    exists().where(
                        LeadSequenceEnrollment.client_id == lead.client_id,
                        LeadSequenceEnrollment.lead_id == lead.id,
                        LeadSequenceEnrollment.exit_reason.in_(OPT_OUT_EXITS),
                    )
                )
            )
        ).scalar_one()
    )


async def inscribir(
    session: AsyncSession,
    lead: Lead,
    secuencia: LeadSequence,
    *,
    ahora: datetime,
    user_id: UUID | None = None,
    next_step_at: datetime | None = None,
) -> LeadSequenceEnrollment:
    """Inscribe un lead en una secuencia.

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead (del mismo tenant que la secuencia).
        secuencia: Secuencia.
        ahora: Instante de la inscripcion.
        user_id: Quien lo inscribe; `None` si es un disparador automatico.
        next_step_at: Cuando ejecutar el primer paso. Por defecto, la siguiente apertura del
            negocio desde `ahora`: un lead que llega a las 3 a. m. no recibe la bienvenida a esa
            hora.

    Returns:
        La inscripcion creada.

    Raises:
        SecuenciaError: `cannot_enroll` si el lead o la secuencia no lo permiten; `opted_out`
            si el lead pidio no recibir mas mensajes (tambien para una inscripcion a mano).
        YaInscritoError: Si ya tiene una inscripcion viva en ella (tambien en una carrera, por el
            UNIQUE parcial).
    """
    if lead.client_id != secuencia.client_id:
        raise SecuenciaError("cannot_enroll", "El lead y la secuencia son de tenants distintos")
    motivo = motivo_para_no_inscribir(lead, secuencia)
    if motivo is not None:
        raise SecuenciaError("cannot_enroll", f"No se puede inscribir al lead ({motivo})")
    if await se_dio_de_baja(session, lead):
        raise SecuenciaError("opted_out", "El lead pidio no recibir mas mensajes")
    if next_step_at is None:
        next_step_at = siguiente_apertura(ahora, await ventana_del_tenant(session, lead.client_id))
    return await _crear_inscripcion(
        session, lead, secuencia, ahora=ahora, user_id=user_id, next_step_at=next_step_at
    )


async def _crear_inscripcion(
    session: AsyncSession,
    lead: Lead,
    secuencia: LeadSequence,
    *,
    ahora: datetime,
    user_id: UUID | None,
    next_step_at: datetime,
) -> LeadSequenceEnrollment:
    """Inserta la inscripcion y la anota; quien llama ya comprobo que se puede inscribir.

    Raises:
        YaInscritoError: Si ya tiene una inscripcion viva en ella.
    """
    inscripcion = LeadSequenceEnrollment(
        client_id=lead.client_id,
        lead_id=lead.id,
        sequence_id=secuencia.id,
        enrolled_by_user_id=user_id,
        current_step=1,
        status=ENROLLMENT_ACTIVE,
        steps_executed=0,
        next_step_at=next_step_at,
        created_at=ahora,
    )
    try:
        async with session.begin_nested():
            session.add(inscripcion)
            await session.flush()
    except IntegrityError as exc:
        if not _viola(exc, "uq_lead_enrollment_live"):
            raise
        raise YaInscritoError() from exc
    registrar_actividad(
        session,
        client_id=lead.client_id,
        lead_id=lead.id,
        tipo=ACTIVITY_SEQUENCE_ENROLLED,
        user_id=user_id,
        sequence_id=secuencia.id,
        enrollment_id=inscripcion.id,
    )
    return inscripcion


def salir(
    session: AsyncSession,
    inscripcion: LeadSequenceEnrollment,
    motivo: str,
    *,
    ahora: datetime,
    user_id: UUID | None = None,
) -> None:
    """Saca un lead de la secuencia (la escribe el `flush` de la sesion).

    Args:
        session: Sesion con el contexto del tenant fijado.
        inscripcion: Inscripcion viva.
        motivo: Codigo `EXIT_*`.
        ahora: Instante de la salida.
        user_id: Quien lo saca; `None` si es el sistema.

    Raises:
        SecuenciaError: `not_live` si la inscripcion ya habia terminado.
    """
    if inscripcion.status not in LIVE_STATUSES:
        raise SecuenciaError("not_live", "La inscripcion ya termino")
    inscripcion.status = ENROLLMENT_EXITED
    inscripcion.exit_reason = motivo
    inscripcion.completed_at = ahora
    inscripcion.next_step_at = None
    registrar_actividad(
        session,
        client_id=inscripcion.client_id,
        lead_id=inscripcion.lead_id,
        tipo=ACTIVITY_SEQUENCE_EXITED,
        user_id=user_id,
        sequence_id=inscripcion.sequence_id,
        enrollment_id=inscripcion.id,
        exit_code=motivo,
    )


async def salir_de_secuencias(
    session: AsyncSession,
    client_id: UUID,
    lead_ids: Sequence[UUID],
    motivo: str,
    *,
    ahora: datetime,
    user_id: UUID | None = None,
    sequence_id: UUID | None = None,
    saltar_bloqueadas: bool = False,
) -> int:
    """Saca a varios leads de todas sus secuencias vivas (o de una).

    Lo usan la supresion RGPD (`EXIT_GDPR`), y Dev B al recibir una respuesta (`EXIT_REPLIED`),
    una baja (`EXIT_UNSUBSCRIBED`) o al cerrar un lead.

    Una baja (`OPT_OUT_EXITS`) saca de **todas** las secuencias aunque se pase `sequence_id`:
    quien pidio no recibir mas mensajes no los recibe de ninguna.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        lead_ids: Leads.
        motivo: Codigo `EXIT_*`.
        ahora: Instante de la salida.
        user_id: Quien los saca.
        sequence_id: Solo esa secuencia (por defecto, todas). Se ignora con una baja.
        saltar_bloqueadas: No esperar a las inscripciones que un worker tiene bloqueadas
            (`SKIP LOCKED`). Para la supresion RGPD: la peticion no se queda colgada mientras el
            worker envia, y en el siguiente turno la regla de bloqueo del motor (lead
            anonimizado) las saca. No usarlo con motivos que esa regla no ve (`EXIT_REPLIED`).

    Returns:
        Inscripciones cerradas.
    """
    if not lead_ids:
        return 0
    consulta = select(LeadSequenceEnrollment).where(
        LeadSequenceEnrollment.client_id == client_id,
        LeadSequenceEnrollment.lead_id.in_(list(lead_ids)),
        LeadSequenceEnrollment.status.in_(LIVE_STATUSES),
    )
    if sequence_id is not None and motivo not in OPT_OUT_EXITS:
        consulta = consulta.where(LeadSequenceEnrollment.sequence_id == sequence_id)
    vivas = (
        (await session.execute(consulta.with_for_update(skip_locked=saltar_bloqueadas)))
        .scalars()
        .all()
    )
    for inscripcion in vivas:
        salir(session, inscripcion, motivo, ahora=ahora, user_id=user_id)
    await session.flush()
    return len(vivas)


def pausar(inscripcion: LeadSequenceEnrollment) -> None:
    """Detiene una inscripcion activa hasta que se reanude.

    Args:
        inscripcion: Inscripcion activa.

    Raises:
        SecuenciaError: `not_active` si no esta activa.
    """
    if inscripcion.status != ENROLLMENT_ACTIVE:
        raise SecuenciaError("not_active", "Solo se pausa una inscripcion activa")
    inscripcion.status = ENROLLMENT_PAUSED
    inscripcion.next_step_at = None


def reanudar(
    inscripcion: LeadSequenceEnrollment, *, ahora: datetime, ventana: VentanaEnvio | None
) -> None:
    """Reanuda una inscripcion pausada; su paso actual se ejecuta en el siguiente turno.

    Args:
        inscripcion: Inscripcion pausada.
        ahora: Instante actual.
        ventana: Horario del negocio (`ventana_del_tenant`); el paso se programa para la
            siguiente apertura (completar una tarea a las 23:00 no debe disparar un mensaje a esa
            hora). Obligatorio, como en `registrar_avance`; `None` solo si el paso puede ir a
            cualquier hora.

    Raises:
        SecuenciaError: `not_paused` si no esta pausada.
    """
    if inscripcion.status != ENROLLMENT_PAUSED:
        raise SecuenciaError("not_paused", "Solo se reanuda una inscripcion pausada")
    inscripcion.status = ENROLLMENT_ACTIVE
    inscripcion.next_step_at = siguiente_apertura(ahora, ventana) if ventana else ahora


# ─── Avance ─────────────────────────────────────────────────────────────────────────────────


async def inscripciones_pendientes(
    session: AsyncSession, client_id: UUID, ahora: datetime, *, limite: int = 100
) -> list[LeadSequenceEnrollment]:
    """Inscripciones activas a las que les toca avanzar, bloqueadas para este worker.

    `FOR UPDATE SKIP LOCKED`: dos workers a la vez se reparten las filas en vez de enviar dos
    veces el mismo mensaje. Usa `ix_lead_sequence_enrollments_due`.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        ahora: Instante de referencia.
        limite: Maximo de filas.

    Returns:
        Las inscripciones, de la mas atrasada a la menos.
    """
    filas = await session.execute(
        select(LeadSequenceEnrollment)
        .where(
            LeadSequenceEnrollment.client_id == client_id,
            LeadSequenceEnrollment.status == ENROLLMENT_ACTIVE,
            LeadSequenceEnrollment.next_step_at <= ahora,
        )
        .order_by(LeadSequenceEnrollment.next_step_at, LeadSequenceEnrollment.id)
        .limit(limite)
        .with_for_update(skip_locked=True)
    )
    return list(filas.scalars().all())


def registrar_avance(
    session: AsyncSession,
    inscripcion: LeadSequenceEnrollment,
    decision: Decision,
    *,
    ahora: datetime,
    ventana: VentanaEnvio | None,
    next_step_at: datetime | None = None,
) -> None:
    """Aplica a la inscripcion la decision del motor, una vez ejecutada su accion.

    - `Exit`: sale con su motivo.
    - `Complete`: termina.
    - `Wait`: avanza y duerme hasta `next_step_at` (obligatorio; ver `programar_siguiente`).
    - `CreateTask` con `pause_until_done`: avanza y queda pausada hasta que se complete la tarea.
    - Resto (`SendMessage`, `CreateTask`): avanza; el siguiente paso toca en el siguiente turno,
      **dentro del horario del negocio** si se pasa `ventana` (dos mensajes seguidos sin `wait`
      no deben acabar el segundo a medianoche).

    Args:
        session: Sesion con el contexto del tenant fijado.
        inscripcion: Inscripcion activa.
        decision: Lo que devolvio `decidir()`.
        ahora: Instante actual.
        ventana: Horario del negocio (`ventana_del_tenant`). Es obligatorio pasarlo de forma
            explicita; `None` solo cuando el siguiente paso puede ir a cualquier hora.
        next_step_at: Cuando despertar, para un `Wait`.

    Raises:
        ValueError: Si es un `Wait` sin `next_step_at`.
    """
    accion = decision.action
    inscripcion.steps_executed += decision.steps_consumed
    if isinstance(accion, Exit):
        salir(session, inscripcion, accion.reason, ahora=ahora)
        return
    inscripcion.last_step_at = ahora
    if isinstance(accion, Complete):
        inscripcion.status = ENROLLMENT_COMPLETED
        inscripcion.completed_at = ahora
        inscripcion.next_step_at = None
        registrar_actividad(
            session,
            client_id=inscripcion.client_id,
            lead_id=inscripcion.lead_id,
            tipo=ACTIVITY_SEQUENCE_COMPLETED,
            sequence_id=inscripcion.sequence_id,
            enrollment_id=inscripcion.id,
        )
        return
    inscripcion.current_step = decision.next_step
    if isinstance(accion, Wait):
        if next_step_at is None:
            raise ValueError("Un paso 'wait' necesita next_step_at")
        inscripcion.next_step_at = next_step_at
    elif isinstance(accion, CreateTask) and accion.step.pause_until_done:
        inscripcion.status = ENROLLMENT_PAUSED
        inscripcion.next_step_at = None
    else:
        inscripcion.next_step_at = siguiente_apertura(ahora, ventana) if ventana else ahora


# ─── Foto del lead ──────────────────────────────────────────────────────────────────────────


async def canales_del_lead(session: AsyncSession, lead: Lead) -> frozenset[str]:
    """Canales por los que se le puede escribir al lead.

    `email` si tiene email; `whatsapp` si tiene telefono (el canal de salida por defecto en la
    region; si el numero no tiene WhatsApp, el envio fallara y Dev B lo saca con `bounced`); y
    los canales de los identificadores de su contacto enlazado.

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead.

    Returns:
        Los canales, dentro de `OUTREACH_CHANNELS`.
    """
    canales: set[str] = set()
    if lead.email:
        canales.add("email")
    if lead.phone:
        canales.add("whatsapp")
    if lead.contact_id is not None:
        filas = await session.execute(
            select(ContactIdentifier.channel)
            .where(
                ContactIdentifier.client_id == lead.client_id,
                ContactIdentifier.contact_id == lead.contact_id,
            )
            .distinct()
        )
        canales.update(filas.scalars().all())
    return frozenset(canales & set(OUTREACH_CHANNELS))


async def snapshot_del_lead(session: AsyncSession, lead: Lead, *, desde: datetime) -> LeadSnapshot:
    """La foto del lead que necesita el motor.

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead.
        desde: Inicio de la inscripcion: `replied`, `email_opened` y `link_clicked` miran solo
            lo ocurrido desde entonces.

    Returns:
        La foto.
    """
    slug = None
    if lead.pipeline_stage_id is not None:
        slug = (
            await session.execute(
                select(LeadPipelineStage.slug).where(
                    LeadPipelineStage.client_id == lead.client_id,
                    LeadPipelineStage.id == lead.pipeline_stage_id,
                )
            )
        ).scalar_one_or_none()

    contesto = False
    if lead.contact_id is not None:
        contesto = bool(
            (
                await session.execute(
                    select(
                        exists().where(
                            Message.client_id == lead.client_id,
                            Message.conversation_id == Conversation.id,
                            Conversation.client_id == lead.client_id,
                            Conversation.contact_id == lead.contact_id,
                            Message.direction == "inbound",
                            Message.created_at >= desde,
                        )
                    )
                )
            ).scalar_one()
        )

    tipos = (
        await session.execute(
            select(LeadActivity.activity_type)
            .where(
                LeadActivity.client_id == lead.client_id,
                LeadActivity.lead_id == lead.id,
                LeadActivity.activity_type.in_(_SENALES_EN_HISTORIAL),
                LeadActivity.created_at >= desde,
            )
            .distinct()
        )
    ).scalars()
    vistos = set(tipos)

    return LeadSnapshot(
        status=lead.status,
        deleted=lead.deleted_at is not None,
        anonymized=lead_esta_anonimizado(lead),
        opted_out=await se_dio_de_baja(session, lead),
        stage_slug=slug,
        total_score=lead.total_score,
        channels=await canales_del_lead(session, lead),
        # Un lead sin contacto enlazado (importado, solo email) tambien puede contestar: la
        # deteccion de respuestas lo anota en su historial.
        replied=contesto or bool(vistos & {ACTIVITY_EMAIL_REPLIED, ACTIVITY_INBOUND_MESSAGE}),
        email_opened=ACTIVITY_EMAIL_OPENED in vistos,
        link_clicked=ACTIVITY_LINK_CLICKED in vistos,
        assigned_user_id=lead.assigned_user_id,
    )


async def horas_de_respuesta(session: AsyncSession, lead: Lead, tz: ZoneInfo) -> list[int]:
    """Horas locales de las ultimas respuestas del lead, para `programar_siguiente()`.

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead.
        tz: Zona horaria del negocio.

    Returns:
        Una hora (0-23) por mensaje entrante reciente; vacia sin contacto enlazado.
    """
    if lead.contact_id is None:
        return []
    instantes = (
        await session.execute(
            select(Message.created_at)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.client_id == lead.client_id,
                Conversation.client_id == lead.client_id,
                Conversation.contact_id == lead.contact_id,
                Message.direction == "inbound",
            )
            .order_by(Message.created_at.desc())
            .limit(_RESPUESTAS_PARA_TIMING)
        )
    ).scalars()
    return [instante.astimezone(tz).hour for instante in instantes]


def variables_del_lead(
    lead: Lead, *, business_name: str | None = None, agent_name: str | None = None
) -> dict[str, str | None]:
    """Valores de `TEMPLATE_VARIABLES` para renderizar un mensaje a este lead.

    Args:
        lead: Lead.
        business_name: Nombre del negocio: el del perfil o, si no hay, `clients.name`. Pasarlo
            siempre: las plantillas predefinidas cuentan con el.
        agent_name: Nombre del agente que firma.

    Returns:
        Variable -> valor (o `None`, que `renderizar_plantilla` deja vacio).
    """
    return {
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "company_name": lead.company_name,
        "job_title": lead.job_title,
        "business_name": business_name,
        "agent_name": agent_name,
    }


# ─── Disparadores ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DatosDisparo:
    """Lo del lead que miran los disparadores.

    Attributes:
        source_type: Tipo de su fuente (`None` sin fuente).
        stage_slug: Slug de su etapa.
        temperature: `cold`, `warm` o `hot`.
        total_score: Score total.
        fit_score: FIT.
    """

    source_type: str | None
    stage_slug: str | None
    temperature: str
    total_score: int
    fit_score: int


def cumple_disparador(
    condiciones: TriggerConditions, evento: TriggerEvent, datos: DatosDisparo
) -> bool:
    """Si un evento sobre un lead dispara la inscripcion en una secuencia.

    Args:
        condiciones: Disparadores de la secuencia.
        evento: Lo que acaba de pasar.
        datos: Datos del lead.

    Returns:
        `True` si el evento esta en la lista y se cumplen todos los filtros con valor.
    """
    if evento not in condiciones.events:
        return False
    if condiciones.source_types and datos.source_type not in condiciones.source_types:
        return False
    if condiciones.stages and datos.stage_slug not in condiciones.stages:
        return False
    if condiciones.temperatures and datos.temperature not in condiciones.temperatures:
        return False
    if condiciones.min_total_score is not None and datos.total_score < condiciones.min_total_score:
        return False
    return condiciones.min_fit_score is None or datos.fit_score >= condiciones.min_fit_score


async def datos_de_disparo(session: AsyncSession, lead: Lead) -> DatosDisparo:
    """Lee de la base lo que los disparadores necesitan del lead.

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead.

    Returns:
        Los datos.
    """
    fila = (
        await session.execute(
            select(LeadSource.source_type, LeadPipelineStage.slug)
            .select_from(Lead)
            .outerjoin(
                LeadSource,
                and_(LeadSource.id == Lead.source_id, LeadSource.client_id == Lead.client_id),
            )
            .outerjoin(
                LeadPipelineStage,
                and_(
                    LeadPipelineStage.id == Lead.pipeline_stage_id,
                    LeadPipelineStage.client_id == Lead.client_id,
                ),
            )
            .where(Lead.client_id == lead.client_id, Lead.id == lead.id)
        )
    ).one()
    return DatosDisparo(
        source_type=fila[0],
        stage_slug=fila[1],
        temperature=lead.temperature,
        total_score=lead.total_score,
        fit_score=lead.fit_score,
    )


async def inscribir_por_evento(
    session: AsyncSession, lead: Lead, evento: TriggerEvent, *, ahora: datetime
) -> list[LeadSequenceEnrollment]:
    """Inscribe al lead en las secuencias activas cuyo disparador cumple.

    Lo llama Dev B tras crear un lead, cambiarle la etapa o el score. Se saltan sin error las
    secuencias en las que no puede entrar y **las que ya recorrio alguna vez**: un disparador
    automatico no vuelve a meter a nadie en una secuencia de la que salio (p. ej. al contestar,
    que a su vez cambia su score). Reinscribir es una decision de una persona (`inscribir`).

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead.
        evento: Lo que acaba de pasar.
        ahora: Instante actual.

    Returns:
        Las inscripciones nuevas.
    """
    secuencias = (
        await session.execute(
            select(LeadSequence)
            .where(LeadSequence.client_id == lead.client_id, LeadSequence.is_active.is_(True))
            .order_by(LeadSequence.created_at, LeadSequence.id)
        )
    ).scalars()
    lista = secuencias.all()
    if not lista or await se_dio_de_baja(session, lead):
        return []
    datos = await datos_de_disparo(session, lead)
    ya_recorridas = set(
        (
            await session.execute(
                select(LeadSequenceEnrollment.sequence_id).where(
                    LeadSequenceEnrollment.client_id == lead.client_id,
                    LeadSequenceEnrollment.lead_id == lead.id,
                )
            )
        ).scalars()
    )
    nuevas: list[LeadSequenceEnrollment] = []
    # La baja y el horario se miran una sola vez, no una por secuencia candidata.
    primer_paso: datetime | None = None
    for secuencia in lista:
        if secuencia.id in ya_recorridas:
            continue
        try:
            condiciones = TriggerConditions.model_validate(secuencia.trigger_conditions)
        except ValidationError:
            continue  # editada a mano en la base: mejor no disparar que disparar mal
        if not cumple_disparador(condiciones, evento, datos):
            continue
        if lead.client_id != secuencia.client_id or motivo_para_no_inscribir(lead, secuencia):
            continue
        if primer_paso is None:
            primer_paso = siguiente_apertura(
                ahora, await ventana_del_tenant(session, lead.client_id)
            )
        try:
            nuevas.append(
                await _crear_inscripcion(
                    session, lead, secuencia, ahora=ahora, user_id=None, next_step_at=primer_paso
                )
            )
        except YaInscritoError:
            continue
    return nuevas


async def inscripciones_para_export(
    session: AsyncSession, client_id: UUID, lead_ids: Sequence[UUID]
) -> dict[UUID, list[dict[str, Any]]]:
    """Las secuencias por las que paso cada lead, para el export RGPD.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        lead_ids: Leads del titular.

    Returns:
        Lead -> sus inscripciones (de la mas antigua a la mas reciente).
    """
    resultado: dict[UUID, list[dict[str, Any]]] = {lead_id: [] for lead_id in lead_ids}
    if not lead_ids:
        return resultado
    filas = await session.execute(
        select(LeadSequenceEnrollment, LeadSequence.name)
        .join(LeadSequence, LeadSequence.id == LeadSequenceEnrollment.sequence_id)
        .where(
            LeadSequenceEnrollment.client_id == client_id,
            LeadSequence.client_id == client_id,
            LeadSequenceEnrollment.lead_id.in_(list(lead_ids)),
        )
        .order_by(LeadSequenceEnrollment.created_at, LeadSequenceEnrollment.id)
    )
    for inscripcion, nombre in filas.tuples().all():
        resultado[inscripcion.lead_id].append(
            {
                "sequence_id": str(inscripcion.sequence_id),
                "sequence_name": nombre,
                "status": inscripcion.status,
                "current_step": inscripcion.current_step,
                "enrolled_at": inscripcion.created_at.isoformat(),
                "completed_at": (
                    inscripcion.completed_at.isoformat() if inscripcion.completed_at else None
                ),
                "exit_reason": inscripcion.exit_reason,
            }
        )
    return resultado
