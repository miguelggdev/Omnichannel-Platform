"""API de secuencias de follow-up de leads (Sprint 18, Dev B).

Secuencias (`/api/v1/lead-sequences`):

    GET    /                 lista con pasos e inscripciones vivas
    POST   /                 crea una secuencia (definicion completa)
    GET    /templates        secuencias predefinidas que se pueden copiar
    POST   /from-template    copia una predefinida (queda APAGADA hasta que se revise)
    GET    /{id}             detalle con sus pasos
    PUT    /{id}             reemplaza la definicion (avisa si hay inscripciones vivas)
    DELETE /{id}             borra (409 si ya tuvo inscripciones: se apaga con PUT)
    GET    /{id}/steps       pasos en orden

Sobre un lead (`/api/v1/leads`):

    POST   /{id}/enroll/{seq}          inscribe
    DELETE /{id}/enroll/{seq}          saca (motivo `manual`)
    POST   /{id}/enroll/{seq}/pause    pausa
    POST   /{id}/enroll/{seq}/resume   reanuda (p. ej. al completar la tarea de un paso `task`)
    GET    /{id}/enrollments           sus inscripciones
    GET    /follow-up/kanban           inscripciones agrupadas por situacion
    POST   /{id}/message               mensaje escrito a mano

Quien puede: **leer** todos los roles de leads; **crear, editar y borrar** secuencias,
`supervisor` para arriba (una secuencia escribe a miles de personas en nombre del negocio);
**inscribir, sacar y escribir** cualquier rol de leads, pero un `agent` solo sobre leads sin
responsable o suyos. Todo exige el modulo de leads activo en el tenant.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.dependencies import require_leads_module
from app.core.exceptions import (
    CONFLICT,
    DUPLICATE,
    FORBIDDEN,
    NOT_FOUND,
    VALIDATION_ERROR,
    AppException,
)
from app.core.rate_limit import hit
from app.models.lead import Lead
from app.models.lead_activity import ACTIVITY_MESSAGE_SENT
from app.models.lead_sequence import (
    ENROLLMENT_ACTIVE,
    ENROLLMENT_COMPLETED,
    ENROLLMENT_EXITED,
    ENROLLMENT_PAUSED,
    EXIT_MANUAL,
    EXIT_NEGATIVE_REPLY,
    EXIT_REPLIED,
    EXIT_UNSUBSCRIBED,
    LIVE_STATUSES,
    LeadSequence,
    LeadSequenceEnrollment,
    LeadSequenceStep,
)
from app.schemas.lead_sequence import SequenceDefinition
from app.schemas.lead_sequence_api import (
    EnrollmentResponse,
    FollowUpCard,
    FollowUpColumn,
    FollowUpColumnKey,
    FromTemplateRequest,
    ManualMessageRequest,
    ManualMessageResponse,
    SequenceDetail,
    SequenceSave,
    SequenceSummary,
    StepResponse,
    TemplateInfo,
)
from app.services.lead_activity import registrar_actividad
from app.services.lead_outreach import (
    CanalNoDisponibleError,
    EnvioPermanenteError,
    EnvioTemporalError,
    destino_del_lead,
    enviar_texto,
)
from app.services.lead_privacy import lead_esta_anonimizado
from app.services.lead_sequence_templates import PLANTILLAS
from app.services.lead_sequence_timing import ventana_del_tenant
from app.services.lead_sequences import (
    PasosInvalidosError,
    SecuenciaError,
    cargar_pasos,
    crear_secuencia,
    inscribir,
    pausar,
    reanudar,
    reemplazar_pasos,
    salir_de_secuencias,
    se_dio_de_baja,
)

logger = logging.getLogger(__name__)

router = APIRouter()
leads_router = APIRouter()

_READ_ROLES = ("super_admin", "admin", "supervisor", "agent")
_WRITE_ROLES = ("super_admin", "admin", "supervisor")
_LEAD_ROLES = ("super_admin", "admin", "supervisor", "agent")

SEQUENCE_ACTIVE_AVISO = (
    "Hay inscripciones vivas: siguen por la posicion en la que iban. Si la secuencia ahora es mas "
    "corta terminan en su siguiente turno, y si se reordeno continuan por el paso que ahora "
    "ocupa esa posicion."
)
DELIVERY_REJECTED = "DELIVERY_REJECTED"
CHANNEL_UNAVAILABLE = "CHANNEL_UNAVAILABLE"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

#: Dias que una inscripcion cerrada sigue apareciendo en el Kanban de follow-up.
KANBAN_DIAS = 30
KANBAN_POR_COLUMNA = 20
KANBAN_MAXIMO = 100
_SALIDAS_CON_RESPUESTA = (EXIT_REPLIED, EXIT_NEGATIVE_REPLY, EXIT_UNSUBSCRIBED)

_ESTADO_HTTP: dict[str, tuple[int, str]] = {
    "duplicate_name": (409, DUPLICATE),
    "already_enrolled": (409, CONFLICT),
    "opted_out": (409, CONFLICT),
    "cannot_enroll": (409, CONFLICT),
    "not_live": (404, NOT_FOUND),
    "not_active": (409, CONFLICT),
    "not_paused": (409, CONFLICT),
}


def _uid(user: dict[str, Any]) -> UUID:
    return UUID(str(user["user_id"]))


def _como_http(exc: SecuenciaError) -> AppException:
    """Traduce un error del servicio de secuencias a la respuesta de la API."""
    estado, codigo = _ESTADO_HTTP.get(exc.code, (409, CONFLICT))
    return AppException(status_code=estado, error_code=codigo, message=str(exc))


async def _secuencia_o_404(
    session: AsyncSession, sequence_id: UUID, client_id: UUID
) -> LeadSequence:
    """Una secuencia del tenant."""
    secuencia: LeadSequence | None = (
        await session.execute(
            select(LeadSequence).where(
                LeadSequence.id == sequence_id, LeadSequence.client_id == client_id
            )
        )
    ).scalar_one_or_none()
    if secuencia is None:
        raise AppException(status_code=404, error_code=NOT_FOUND, message="Secuencia no encontrada")
    return secuencia


async def _lead_accesible(session: AsyncSession, lead_id: UUID, user: dict[str, Any]) -> Lead:
    """Un lead del tenant, sin borrar, sobre el que este usuario puede actuar.

    Un `agent` solo actua sobre leads sin responsable o suyos.

    Raises:
        AppException: 404 si no existe; 403 si es de otro agente y el usuario es `agent`.
    """
    client_id: UUID = user["client_id"]
    lead: Lead | None = (
        await session.execute(
            select(Lead).where(
                Lead.id == lead_id, Lead.client_id == client_id, Lead.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if lead is None:
        raise AppException(status_code=404, error_code=NOT_FOUND, message="Lead no encontrado")
    if user["role"] == "agent" and lead.assigned_user_id not in (None, _uid(user)):
        raise AppException(
            status_code=403,
            error_code=FORBIDDEN,
            message="Un agente solo puede actuar sobre leads sin responsable o suyos",
        )
    return lead


async def _resumenes(
    session: AsyncSession, client_id: UUID, secuencias: list[LeadSequence]
) -> list[SequenceSummary]:
    """Las secuencias con su numero de pasos e inscripciones vivas (dos consultas fijas)."""
    ids = [s.id for s in secuencias]
    pasos = dict(
        tuple(fila)
        for fila in (
            await session.execute(
                select(LeadSequenceStep.sequence_id, func.count())
                .where(
                    LeadSequenceStep.client_id == client_id, LeadSequenceStep.sequence_id.in_(ids)
                )
                .group_by(LeadSequenceStep.sequence_id)
            )
        ).all()
    )
    vivas = dict(
        tuple(fila)
        for fila in (
            await session.execute(
                select(LeadSequenceEnrollment.sequence_id, func.count())
                .where(
                    LeadSequenceEnrollment.client_id == client_id,
                    LeadSequenceEnrollment.sequence_id.in_(ids),
                    LeadSequenceEnrollment.status.in_(LIVE_STATUSES),
                )
                .group_by(LeadSequenceEnrollment.sequence_id)
            )
        ).all()
    )
    return [
        SequenceSummary.model_validate(
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "is_active": s.is_active,
                "trigger_conditions": s.trigger_conditions,
                "channel_priority": s.channel_priority,
                "steps_count": pasos.get(s.id, 0),
                "live_enrollments": vivas.get(s.id, 0),
                "created_at": s.created_at,
                "updated_at": s.updated_at,
            }
        )
        for s in secuencias
    ]


async def _pasos_de(
    session: AsyncSession, client_id: UUID, sequence_id: UUID
) -> list[StepResponse]:
    """Los pasos validados, en orden.

    Raises:
        AppException: 409 si los pasos guardados no forman una secuencia valida (editados a mano).
    """
    try:
        pasos = await cargar_pasos(session, client_id, sequence_id)
    except PasosInvalidosError as exc:
        raise AppException(status_code=409, error_code=CONFLICT, message=str(exc)) from exc
    return [StepResponse(position=i, step=p) for i, p in enumerate(pasos, start=1)]


async def _detalle(
    session: AsyncSession, client_id: UUID, secuencia: LeadSequence, *, avisar: bool = False
) -> SequenceDetail:
    """Detalle de una secuencia; con `avisar`, el aviso de inscripciones vivas."""
    resumen = (await _resumenes(session, client_id, [secuencia]))[0]
    pasos = await _pasos_de(session, client_id, secuencia.id)
    aviso = SEQUENCE_ACTIVE_AVISO if avisar and resumen.live_enrollments else None
    return SequenceDetail(**resumen.model_dump(), steps=pasos, live_enrollments_warning=aviso)


async def _guardar_con_nombre_unico(session: AsyncSession) -> None:
    """`flush` que traduce el choque con el nombre unico por tenant en un 409."""
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        if "uq_lead_sequence_name" not in str(exc.orig):
            raise
        raise AppException(
            status_code=409,
            error_code=DUPLICATE,
            message="Ya existe una secuencia con ese nombre",
        ) from exc


# ── Secuencias ───────────────────────────────────────────────────────────────────────────────


@router.get("", response_model=list[SequenceSummary])
async def list_sequences(
    active: bool | None = None,
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> list[SequenceSummary]:
    """Las secuencias del tenant.

    Args:
        active: Solo las encendidas (`true`) o las apagadas (`false`).
        user: Usuario autenticado.

    Returns:
        Las secuencias por nombre, con su numero de pasos e inscripciones vivas.
    """
    client_id: UUID = user["client_id"]
    consulta = select(LeadSequence).where(LeadSequence.client_id == client_id)
    if active is not None:
        consulta = consulta.where(LeadSequence.is_active.is_(active))
    async with tenant_session(client_id) as session:
        secuencias = list(
            (await session.execute(consulta.order_by(LeadSequence.name))).scalars().all()
        )
        return await _resumenes(session, client_id, secuencias) if secuencias else []


@router.post("", response_model=SequenceDetail, status_code=201)
async def create_sequence(
    data: SequenceSave,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> SequenceDetail:
    """Crea una secuencia con todos sus pasos.

    Args:
        data: Definicion completa (el grafo se valida: saltos a pasos que existen y ningun bucle
            sin espera).
        user: Usuario autenticado.

    Returns:
        La secuencia creada.

    Raises:
        AppException: 409 si ya hay una con ese nombre; 422 si la definicion no valida.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id, user_id=_uid(user)) as session:
        try:
            secuencia = await crear_secuencia(
                session,
                client_id,
                SequenceDefinition(**data.model_dump(exclude={"is_active"})),
                user_id=_uid(user),
            )
        except SecuenciaError as exc:
            raise _como_http(exc) from exc
        secuencia.is_active = data.is_active
        await session.flush()
        await session.refresh(secuencia)
        return await _detalle(session, client_id, secuencia)


@router.get("/templates", response_model=list[TemplateInfo])
async def list_templates(
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> list[TemplateInfo]:
    """Las secuencias predefinidas que el tenant puede copiar.

    Args:
        user: Usuario autenticado.

    Returns:
        Una entrada por plantilla.
    """
    del user
    return [
        TemplateInfo(
            key=clave,
            name=d.name,
            description=d.description,
            steps_count=len(d.steps),
            auto_trigger=bool(d.trigger_conditions.events),
        )
        for clave, d in PLANTILLAS.items()
    ]


@router.post("/from-template", response_model=SequenceDetail, status_code=201)
async def create_from_template(
    data: FromTemplateRequest,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> SequenceDetail:
    """Copia una secuencia predefinida. Queda **apagada**: hay que revisarla y encenderla.

    Una plantilla puede dispararse sola (`lead_created`); copiarla encendida empezaria a escribir a
    los leads nuevos antes de que nadie haya leido los textos.

    Args:
        data: Clave de la plantilla y, opcionalmente, un nombre propio.
        user: Usuario autenticado.

    Returns:
        La secuencia creada.

    Raises:
        AppException: 404 si la plantilla no existe; 409 si el nombre ya esta en uso.
    """
    plantilla = PLANTILLAS.get(data.template)
    if plantilla is None:
        raise AppException(status_code=404, error_code=NOT_FOUND, message="Plantilla no encontrada")
    definicion = plantilla.model_copy(update={"name": data.name or plantilla.name}, deep=True)
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id, user_id=_uid(user)) as session:
        try:
            secuencia = await crear_secuencia(session, client_id, definicion, user_id=_uid(user))
        except SecuenciaError as exc:
            raise _como_http(exc) from exc
        secuencia.is_active = False
        await session.flush()
        await session.refresh(secuencia)
        return await _detalle(session, client_id, secuencia)


@router.get("/{sequence_id}", response_model=SequenceDetail)
async def get_sequence(
    sequence_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> SequenceDetail:
    """Detalle de una secuencia con sus pasos.

    Args:
        sequence_id: Secuencia.
        user: Usuario autenticado.

    Returns:
        La secuencia.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        secuencia = await _secuencia_o_404(session, sequence_id, client_id)
        return await _detalle(session, client_id, secuencia)


@router.put("/{sequence_id}", response_model=SequenceDetail)
async def update_sequence(
    sequence_id: UUID,
    data: SequenceSave,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> SequenceDetail:
    """Reemplaza la definicion de una secuencia (datos, disparador, prioridad y pasos).

    Las inscripciones vivas siguen por la **posicion** en la que iban; la respuesta lo avisa en
    `live_enrollments_warning`.

    Args:
        sequence_id: Secuencia.
        data: Nueva definicion completa.
        user: Usuario autenticado.

    Returns:
        La secuencia actualizada.

    Raises:
        AppException: 404 si no existe; 409 si el nombre ya esta en uso.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id, user_id=_uid(user)) as session:
        secuencia = await _secuencia_o_404(session, sequence_id, client_id)
        secuencia.name = data.name
        secuencia.description = data.description
        secuencia.trigger_conditions = data.trigger_conditions.model_dump(mode="json")
        secuencia.channel_priority = list(data.channel_priority)
        secuencia.is_active = data.is_active
        await _guardar_con_nombre_unico(session)
        await reemplazar_pasos(
            session,
            client_id,
            secuencia,
            SequenceDefinition(**data.model_dump(exclude={"is_active"})),
        )
        await session.refresh(secuencia)
        return await _detalle(session, client_id, secuencia, avisar=True)


@router.delete("/{sequence_id}", status_code=204)
async def delete_sequence(
    sequence_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> None:
    """Borra una secuencia que nunca tuvo inscripciones. Si ya las tuvo, se apaga con `PUT`.

    Args:
        sequence_id: Secuencia.
        user: Usuario autenticado.

    Raises:
        AppException: 404 si no existe; 409 si tiene o tuvo inscripciones (el historial de los
            leads las referencia).
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id, user_id=_uid(user)) as session:
        secuencia = await _secuencia_o_404(session, sequence_id, client_id)
        usadas = (
            await session.execute(
                select(func.count())
                .select_from(LeadSequenceEnrollment)
                .where(
                    LeadSequenceEnrollment.client_id == client_id,
                    LeadSequenceEnrollment.sequence_id == sequence_id,
                )
            )
        ).scalar_one()
        if usadas:
            raise AppException(
                status_code=409,
                error_code=CONFLICT,
                message="La secuencia ya tuvo inscripciones: apagala en lugar de borrarla",
            )
        await session.delete(secuencia)


@router.get("/{sequence_id}/steps", response_model=list[StepResponse])
async def get_steps(
    sequence_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> list[StepResponse]:
    """Los pasos de una secuencia, en orden.

    Args:
        sequence_id: Secuencia.
        user: Usuario autenticado.

    Returns:
        Los pasos con su posicion.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        await _secuencia_o_404(session, sequence_id, client_id)
        return await _pasos_de(session, client_id, sequence_id)


# ── Inscripciones ────────────────────────────────────────────────────────────────────────────


async def _inscripcion_viva(
    session: AsyncSession, client_id: UUID, lead_id: UUID, sequence_id: UUID
) -> LeadSequenceEnrollment:
    """La inscripcion viva (activa o pausada) del lead en la secuencia."""
    inscripcion: LeadSequenceEnrollment | None = (
        await session.execute(
            select(LeadSequenceEnrollment)
            .where(
                LeadSequenceEnrollment.client_id == client_id,
                LeadSequenceEnrollment.lead_id == lead_id,
                LeadSequenceEnrollment.sequence_id == sequence_id,
                LeadSequenceEnrollment.status.in_(LIVE_STATUSES),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if inscripcion is None:
        raise AppException(
            status_code=404, error_code=NOT_FOUND, message="El lead no esta en esa secuencia"
        )
    return inscripcion


@leads_router.post(
    "/{lead_id}/enroll/{sequence_id}", response_model=EnrollmentResponse, status_code=201
)
async def enroll_lead(
    lead_id: UUID,
    sequence_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_LEAD_ROLES)),
) -> EnrollmentResponse:
    """Inscribe un lead en una secuencia; el primer paso toca en la proxima apertura del negocio.

    Args:
        lead_id: Lead.
        sequence_id: Secuencia (debe estar encendida).
        user: Usuario autenticado.

    Returns:
        La inscripcion.

    Raises:
        AppException: 404 si no existe el lead o la secuencia; 409 si ya esta inscrito, si pidio no
            recibir mas mensajes, o si el lead o la secuencia no lo permiten.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id, user_id=_uid(user)) as session:
        lead = await _lead_accesible(session, lead_id, user)
        secuencia = await _secuencia_o_404(session, sequence_id, client_id)
        try:
            inscripcion = await inscribir(
                session, lead, secuencia, ahora=datetime.now(timezone.utc), user_id=_uid(user)
            )
        except SecuenciaError as exc:
            raise _como_http(exc) from exc
        return EnrollmentResponse.model_validate(inscripcion)


@leads_router.delete("/{lead_id}/enroll/{sequence_id}", status_code=204)
async def unenroll_lead(
    lead_id: UUID,
    sequence_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_LEAD_ROLES)),
) -> None:
    """Saca a un lead de una secuencia (motivo `manual`).

    Args:
        lead_id: Lead.
        sequence_id: Secuencia.
        user: Usuario autenticado.

    Raises:
        AppException: 404 si el lead no esta (vivo) en la secuencia.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id, user_id=_uid(user)) as session:
        await _lead_accesible(session, lead_id, user)
        cerradas = await salir_de_secuencias(
            session,
            client_id,
            [lead_id],
            EXIT_MANUAL,
            ahora=datetime.now(timezone.utc),
            user_id=_uid(user),
            sequence_id=sequence_id,
        )
        if not cerradas:
            raise AppException(
                status_code=404, error_code=NOT_FOUND, message="El lead no esta en esa secuencia"
            )


@leads_router.post("/{lead_id}/enroll/{sequence_id}/pause", response_model=EnrollmentResponse)
async def pause_enrollment(
    lead_id: UUID,
    sequence_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_LEAD_ROLES)),
) -> EnrollmentResponse:
    """Pausa la inscripcion hasta que se reanude.

    Args:
        lead_id: Lead.
        sequence_id: Secuencia.
        user: Usuario autenticado.

    Returns:
        La inscripcion pausada.

    Raises:
        AppException: 404 si no esta en la secuencia; 409 si no estaba activa.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id, user_id=_uid(user)) as session:
        await _lead_accesible(session, lead_id, user)
        inscripcion = await _inscripcion_viva(session, client_id, lead_id, sequence_id)
        try:
            pausar(inscripcion)
        except SecuenciaError as exc:
            raise _como_http(exc) from exc
        await session.flush()
        return EnrollmentResponse.model_validate(inscripcion)


@leads_router.post("/{lead_id}/enroll/{sequence_id}/resume", response_model=EnrollmentResponse)
async def resume_enrollment(
    lead_id: UUID,
    sequence_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_LEAD_ROLES)),
) -> EnrollmentResponse:
    """Reanuda una inscripcion pausada (p. ej. tras completar la tarea de un paso `task`).

    El siguiente paso se programa para la proxima apertura del negocio.

    Args:
        lead_id: Lead.
        sequence_id: Secuencia.
        user: Usuario autenticado.

    Returns:
        La inscripcion reanudada.

    Raises:
        AppException: 404 si no esta en la secuencia; 409 si no estaba pausada.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id, user_id=_uid(user)) as session:
        await _lead_accesible(session, lead_id, user)
        inscripcion = await _inscripcion_viva(session, client_id, lead_id, sequence_id)
        try:
            reanudar(
                inscripcion,
                ahora=datetime.now(timezone.utc),
                ventana=await ventana_del_tenant(session, client_id),
            )
        except SecuenciaError as exc:
            raise _como_http(exc) from exc
        await session.flush()
        return EnrollmentResponse.model_validate(inscripcion)


@leads_router.get("/{lead_id}/enrollments", response_model=list[EnrollmentResponse])
async def lead_enrollments(
    lead_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> list[EnrollmentResponse]:
    """Las inscripciones de un lead, las mas recientes primero.

    Args:
        lead_id: Lead.
        user: Usuario autenticado.

    Returns:
        Sus inscripciones (vivas y cerradas).
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        existe = (
            await session.execute(
                select(Lead.id).where(
                    Lead.id == lead_id, Lead.client_id == client_id, Lead.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if existe is None:
            raise AppException(status_code=404, error_code=NOT_FOUND, message="Lead no encontrado")
        filas = (
            (
                await session.execute(
                    select(LeadSequenceEnrollment)
                    .where(
                        LeadSequenceEnrollment.client_id == client_id,
                        LeadSequenceEnrollment.lead_id == lead_id,
                    )
                    .order_by(LeadSequenceEnrollment.created_at.desc(), LeadSequenceEnrollment.id)
                )
            )
            .scalars()
            .all()
        )
        return [EnrollmentResponse.model_validate(f) for f in filas]


# ── Kanban de follow-up ──────────────────────────────────────────────────────────────────────

_COLUMNAS: tuple[FollowUpColumnKey, ...] = ("overdue", "upcoming", "paused", "replied", "finished")


@leads_router.get("/follow-up/kanban", response_model=list[FollowUpColumn])
async def follow_up_kanban(
    per_column: int = Query(default=KANBAN_POR_COLUMNA, ge=1, le=KANBAN_MAXIMO),
    sequence_id: UUID | None = None,
    assigned_user_id: UUID | None = None,
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> list[FollowUpColumn]:
    """Las inscripciones agrupadas por situacion, siempre las cinco columnas.

    - `overdue`: activas a las que ya les toca (las mas atrasadas primero).
    - `upcoming`: activas programadas (las mas proximas primero).
    - `paused`: esperando a una persona.
    - `replied`: el lead contesto (o pidio que no le escribieran) en los ultimos 30 dias.
    - `finished`: terminaron o salieron por otro motivo en los ultimos 30 dias.

    Cada columna trae el total real y las primeras `per_column` tarjetas. Son dos consultas fijas.

    Args:
        per_column: Tarjetas por columna, de 1 a 100.
        sequence_id: Solo esta secuencia.
        assigned_user_id: Solo leads de este responsable.
        user: Usuario autenticado.

    Returns:
        Las cinco columnas en orden.
    """
    client_id: UUID = user["client_id"]
    ahora = datetime.now(timezone.utc)
    desde = ahora - timedelta(days=KANBAN_DIAS)
    e = LeadSequenceEnrollment
    cubo = case(
        (and_(e.status == ENROLLMENT_ACTIVE, e.next_step_at <= ahora), "overdue"),
        (e.status == ENROLLMENT_ACTIVE, "upcoming"),
        (e.status == ENROLLMENT_PAUSED, "paused"),
        (
            and_(
                e.status == ENROLLMENT_EXITED,
                e.exit_reason.in_(_SALIDAS_CON_RESPUESTA),
                e.completed_at >= desde,
            ),
            "replied",
        ),
        (
            and_(e.status.in_((ENROLLMENT_COMPLETED, ENROLLMENT_EXITED)), e.completed_at >= desde),
            "finished",
        ),
        else_=None,
    ).label("cubo")
    orden = case(
        (e.status == ENROLLMENT_ACTIVE, e.next_step_at),
        else_=func.coalesce(e.completed_at, e.created_at),
    )
    filtros = [e.client_id == client_id, Lead.deleted_at.is_(None)]
    if sequence_id is not None:
        filtros.append(e.sequence_id == sequence_id)
    if assigned_user_id is not None:
        filtros.append(Lead.assigned_user_id == assigned_user_id)

    base = (
        select(e.id.label("id"), cubo, orden.label("orden"))
        .join(Lead, and_(Lead.id == e.lead_id, Lead.client_id == e.client_id))
        .where(*filtros)
        .subquery()
    )
    async with tenant_session(client_id) as session:
        totales = {
            clave: int(cuenta)
            for clave, cuenta in (
                await session.execute(
                    select(base.c.cubo, func.count())
                    .where(base.c.cubo.is_not(None))
                    .group_by(base.c.cubo)
                )
            ).all()
        }
        puesto = (
            func.row_number()
            .over(
                partition_by=base.c.cubo,
                order_by=(
                    case((base.c.cubo.in_(("overdue", "upcoming")), base.c.orden))
                    .asc()
                    .nulls_last(),
                    base.c.orden.desc(),
                    base.c.id,
                ),
            )
            .label("puesto")
        )
        ranking = select(base.c.id, base.c.cubo, puesto).where(base.c.cubo.is_not(None)).subquery()
        filas = (
            await session.execute(
                select(e, Lead, LeadSequence.name, ranking.c.cubo, ranking.c.puesto)
                .join(ranking, ranking.c.id == e.id)
                .join(Lead, and_(Lead.id == e.lead_id, Lead.client_id == e.client_id))
                .join(LeadSequence, LeadSequence.id == e.sequence_id)
                .where(ranking.c.puesto <= per_column)
                .order_by(ranking.c.cubo, ranking.c.puesto)
            )
        ).all()
    tarjetas: dict[str, list[FollowUpCard]] = {c: [] for c in _COLUMNAS}
    for inscripcion, lead, nombre, clave, _ in filas:
        tarjetas[clave].append(
            FollowUpCard(
                enrollment_id=inscripcion.id,
                lead_id=lead.id,
                sequence_id=inscripcion.sequence_id,
                sequence_name=nombre,
                first_name=lead.first_name,
                last_name=lead.last_name,
                company_name=lead.company_name,
                total_score=lead.total_score,
                temperature=lead.temperature,
                assigned_user_id=lead.assigned_user_id,
                status=inscripcion.status,
                current_step=inscripcion.current_step,
                next_step_at=inscripcion.next_step_at,
                exit_reason=inscripcion.exit_reason,
                last_step_at=inscripcion.last_step_at,
            )
        )
    return [
        FollowUpColumn(key=c, total=int(totales.get(c, 0)), cards=tarjetas[c]) for c in _COLUMNAS
    ]


# ── Mensaje manual ───────────────────────────────────────────────────────────────────────────


@leads_router.post("/{lead_id}/message", response_model=ManualMessageResponse)
async def send_manual_message(
    lead_id: UUID,
    data: ManualMessageRequest,
    user: dict[str, Any] = Depends(require_leads_module(*_LEAD_ROLES)),
) -> ManualMessageResponse:
    """Envia a un lead un mensaje escrito a mano por una persona.

    No lleva el pie de baja de las secuencias (ahi habla alguien con el cliente) y no se manda a un
    lead que pidio no recibir mas mensajes. Queda en el historial del lead (`message_sent`, sin el
    texto). Hay un limite por usuario y hora.

    Args:
        lead_id: Lead.
        data: Canal, texto y, en email, asunto.
        user: Usuario autenticado.

    Returns:
        Confirmacion del envio.

    Raises:
        AppException: 404 si el lead no existe; 403 si es de otro agente; 400 si el lead no tiene
            destino en ese canal; 409 si pidio no recibir mas mensajes o esta cerrado/suprimido;
            422 si el proveedor lo rechaza; 429 por el limite; 503 si el proveedor no responde.
    """
    client_id: UUID = user["client_id"]
    uid = _uid(user)
    await hit(
        f"lead-msg:{client_id}:{uid}", get_settings().LEAD_MANUAL_MESSAGES_PER_USER_PER_HOUR, 3600
    )
    async with tenant_session(client_id) as session:
        lead = await _lead_accesible(session, lead_id, user)
        if lead_esta_anonimizado(lead):
            raise AppException(
                status_code=409, error_code=CONFLICT, message="El lead fue anonimizado"
            )
        if await se_dio_de_baja(session, lead):
            raise AppException(
                status_code=409,
                error_code=CONFLICT,
                message="El lead pidio no recibir mas mensajes",
            )
        destino = await destino_del_lead(session, lead, data.channel)
    if destino is None:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message=f"El lead no tiene destino por el canal {data.channel}",
        )
    try:
        externo = await enviar_texto(
            canal=data.channel, destino=destino, texto=data.text, asunto=data.subject
        )
    except CanalNoDisponibleError as exc:
        raise AppException(
            status_code=400, error_code=CHANNEL_UNAVAILABLE, message="El canal no esta configurado"
        ) from exc
    except EnvioPermanenteError as exc:
        raise AppException(
            status_code=422,
            error_code=DELIVERY_REJECTED,
            message=f"El proveedor rechazo el mensaje ({exc.code})",
        ) from exc
    except EnvioTemporalError as exc:
        raise AppException(
            status_code=503,
            error_code=PROVIDER_UNAVAILABLE,
            message="El proveedor no responde; intentalo de nuevo",
        ) from exc

    async with tenant_session(client_id, user_id=uid) as session:
        registrar_actividad(
            session,
            client_id=client_id,
            lead_id=lead_id,
            tipo=ACTIVITY_MESSAGE_SENT,
            user_id=uid,
            channel=data.channel,
            external_id=externo,
        )
    return ManualMessageResponse(lead_id=lead_id, channel=data.channel)
