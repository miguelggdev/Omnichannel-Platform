"""API de leads: CRUD, filtros, Kanban, cambio de etapa y enlace con contactos (Sprint 16).

GET    /api/v1/leads                    lista con filtros, orden y paginacion
POST   /api/v1/leads                    crea un lead (409 si el email o telefono ya existe)
GET    /api/v1/leads/kanban             leads agrupados por etapa, con totales
GET    /api/v1/leads/{id}               detalle
PUT    /api/v1/leads/{id}               edita los campos que se envien
DELETE /api/v1/leads/{id}               soft delete (NO es una supresion RGPD: ver admin.py)
PATCH  /api/v1/leads/{id}/stage         mueve de etapa (ganado/perdido/descalificado cambian el estado)
PUT    /api/v1/leads/{id}/contact       enlaza con un contacto existente
POST   /api/v1/leads/{id}/contact       crea un contacto a partir del lead y lo enlaza
DELETE /api/v1/leads/{id}/contact       suelta el enlace

(`POST /leads/import` esta en `lead_import.py`.)

Decisiones:

- **Todo bajo `tenant_session()`**: etapa, fuente, usuario y contacto de un lead se resuelven con
  el `client_id` explicito *dentro* de la sesion del tenant. Las FK de la base no impiden apuntar
  a una fila de otro tenant; es esta resolucion (y RLS) lo que lo impide.
- **Duplicados:** se pre-comprueba el email/telefono (409 con el id del lead existente) y ademas
  se atrapa la violacion del indice unico, por si dos peticiones llegan a la vez.
- **Agentes ven todos los leads**, como en contactos; configurar el pipeline es de admin.
- Borrar es soft delete: el lead sale de listados, Kanban y duplicados, pero sigue en la base con
  sus datos hasta que se anonimiza (`DELETE /admin/leads/{id}/gdpr-delete`).
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.dependencies import require_leads_module
from app.core.encryption import blind_index
from app.core.exceptions import DUPLICATE, NOT_FOUND, VALIDATION_ERROR, AppException
from app.models.contact import Contact
from app.models.lead import Lead, normalizar_telefono_de_lead
from app.models.lead_pipeline_stage import LeadPipelineStage
from app.models.lead_source import LeadSource
from app.models.user import User
from app.schemas.common import PaginatedResponse
from app.schemas.lead import (
    KanbanColumn,
    LeadContactLink,
    LeadCreate,
    LeadResponse,
    LeadStageMove,
    LeadUpdate,
    StageResponse,
)
from app.services.lead_contact_sync import (
    ContactoYaVinculadoError,
    contacto_inequivoco,
    contactos_candidatos,
    crear_contacto_desde_lead,
    desvincular,
    vincular,
)
from app.services.lead_pipeline import DISQUALIFIED_SLUG, FIRST_STAGE_SLUG

logger = logging.getLogger(__name__)

router = APIRouter()

_READ_ROLES = ("super_admin", "admin", "supervisor", "agent")
_WRITE_ROLES = ("super_admin", "admin", "supervisor", "agent")
_DELETE_ROLES = ("super_admin", "admin", "supervisor")

#: Etapas cuyo slug fija el estado del lead al entrar en ellas.
_ESTADO_POR_SLUG = {"won": "won", "lost": "lost", DISQUALIFIED_SLUG: "disqualified"}

SortField = Literal[
    "created_at",
    "updated_at",
    "total_score",
    "last_activity_at",
    "next_follow_up_at",
    "estimated_value",
    "first_name",
    "company_name",
]
_ORDEN: dict[str, Any] = {
    "created_at": Lead.created_at,
    "updated_at": Lead.updated_at,
    "total_score": Lead.total_score,
    "last_activity_at": Lead.last_activity_at,
    "next_follow_up_at": Lead.next_follow_up_at,
    "estimated_value": Lead.estimated_value,
    "first_name": Lead.first_name,
    "company_name": Lead.company_name,
}
KANBAN_POR_COLUMNA = 20
KANBAN_MAXIMO = 100


def _patron(texto: str) -> str:
    """Patron `ILIKE` que trata `%`, `_` y `\\` del usuario como texto, no como comodines."""
    escapado = texto.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escapado}%"


async def _lead_o_404(session: AsyncSession, lead_id: UUID, client_id: UUID) -> Lead:
    """Un lead del tenant que no este borrado."""
    lead: Lead | None = (
        await session.execute(
            select(Lead).where(
                Lead.id == lead_id, Lead.client_id == client_id, Lead.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if lead is None:
        raise AppException(status_code=404, error_code=NOT_FOUND, message="Lead no encontrado")
    return lead


async def _etapa_o_400(session: AsyncSession, stage_id: UUID, client_id: UUID) -> LeadPipelineStage:
    etapa: LeadPipelineStage | None = (
        await session.execute(
            select(LeadPipelineStage).where(
                LeadPipelineStage.id == stage_id, LeadPipelineStage.client_id == client_id
            )
        )
    ).scalar_one_or_none()
    if etapa is None:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message="La etapa no existe en este negocio",
        )
    return etapa


async def _etapa_inicial(session: AsyncSession, client_id: UUID) -> LeadPipelineStage:
    """La etapa `new`, o la primera del pipeline si el tenant renombro/borro `new`."""
    etapas = (
        (
            await session.execute(
                select(LeadPipelineStage)
                .where(LeadPipelineStage.client_id == client_id)
                .order_by(LeadPipelineStage.position)
            )
        )
        .scalars()
        .all()
    )
    if not etapas:
        raise AppException(
            status_code=409,
            error_code=VALIDATION_ERROR,
            message="El negocio no tiene pipeline: crea al menos una etapa antes de crear leads",
        )
    return next((e for e in etapas if e.slug == FIRST_STAGE_SLUG), etapas[0])


async def _validar_referencias(
    session: AsyncSession,
    client_id: UUID,
    *,
    source_id: UUID | None = None,
    assigned_user_id: UUID | None = None,
) -> None:
    """Comprueba que fuente y usuario existen **en este tenant** (y el usuario esta activo)."""
    if source_id is not None:
        existe = (
            await session.execute(
                select(LeadSource.id).where(
                    LeadSource.id == source_id, LeadSource.client_id == client_id
                )
            )
        ).first()
        if existe is None:
            raise AppException(
                status_code=400, error_code=VALIDATION_ERROR, message="La fuente no existe"
            )
    if assigned_user_id is not None:
        usuario = (
            await session.execute(
                select(User.role, User.is_active).where(
                    User.id == assigned_user_id, User.client_id == client_id
                )
            )
        ).first()
        if usuario is None or not usuario.is_active or usuario.role == "medical":
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message="El usuario no existe, esta desactivado o no puede atender leads",
            )


async def _comprobar_duplicado(
    session: AsyncSession,
    client_id: UUID,
    *,
    email: str | None,
    phone: str | None,
    excluir: UUID | None = None,
) -> None:
    """409 si otro lead no borrado del tenant ya tiene ese email o ese telefono."""
    comprobaciones = []
    if email:
        comprobaciones.append(("email", Lead.email_hash == blind_index(email, client_id)))
    digitos = normalizar_telefono_de_lead(phone)
    if digitos:
        comprobaciones.append(("telefono", Lead.phone_hash == blind_index(digitos, client_id)))
    for campo, condicion in comprobaciones:
        consulta = select(Lead.id).where(
            Lead.client_id == client_id, Lead.deleted_at.is_(None), condicion
        )
        if excluir is not None:
            consulta = consulta.where(Lead.id != excluir)
        existente = (await session.execute(consulta.limit(1))).scalar_one_or_none()
        if existente is not None:
            raise AppException(
                status_code=409,
                error_code=DUPLICATE,
                message=f"Ya existe un lead con ese {campo}",
                details={"existing_lead_id": str(existente), "field": campo},
            )


def _duplicado_por_carrera(exc: IntegrityError) -> AppException | None:
    """Traduce la violacion del indice unico (dos peticiones a la vez) a un 409."""
    texto = str(exc.orig)
    for indice, campo in (
        ("uq_leads_client_email_hash", "email"),
        ("uq_leads_client_phone_hash", "telefono"),
    ):
        if indice in texto:
            return AppException(
                status_code=409,
                error_code=DUPLICATE,
                message=f"Ya existe un lead con ese {campo}",
                details={"field": campo},
            )
    return None


async def _responder(session: AsyncSession, lead: Lead) -> LeadResponse:
    """Serializa un lead ya modificado.

    `updated_at` lo reescribe la base (`onupdate=now()`) y SQLAlchemy lo marca como expirado:
    leerlo fuera de la sesion async intentaria una carga perezosa y fallaria. Se refresca antes.
    """
    await session.refresh(lead)
    return LeadResponse.model_validate(lead)


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


# ── Kanban (antes de /{lead_id}: una ruta fija no debe caer en el parametro) ──────────────────


@router.get("/kanban", response_model=list[KanbanColumn])
async def kanban(
    per_column: int = Query(default=KANBAN_POR_COLUMNA, ge=1, le=KANBAN_MAXIMO),
    assigned_user_id: UUID | None = None,
    source_id: UUID | None = None,
    temperature: Literal["cold", "warm", "hot"] | None = None,
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> list[KanbanColumn]:
    """Leads agrupados por etapa: una columna por etapa del pipeline, aunque este vacia.

    Cada columna trae los `per_column` leads de mayor score y, aparte, el total real de la etapa
    y la suma de su valor estimado (calculados sobre **todos** los leads de la etapa, no solo los
    que se devuelven). Son tres consultas fijas, no una por columna.

    Args:
        per_column: Leads por columna, de 1 a 100.
        assigned_user_id: Solo los asignados a este usuario.
        source_id: Solo los de esta fuente.
        temperature: Solo con esta temperatura.
        user: Usuario autenticado.

    Returns:
        Las columnas en el orden del pipeline.
    """
    client_id: UUID = user["client_id"]
    filtros = [Lead.client_id == client_id, Lead.deleted_at.is_(None)]
    if assigned_user_id is not None:
        filtros.append(Lead.assigned_user_id == assigned_user_id)
    if source_id is not None:
        filtros.append(Lead.source_id == source_id)
    if temperature is not None:
        filtros.append(Lead.temperature == temperature)

    async with tenant_session(client_id) as session:
        etapas = (
            (
                await session.execute(
                    select(LeadPipelineStage)
                    .where(LeadPipelineStage.client_id == client_id)
                    .order_by(LeadPipelineStage.position)
                )
            )
            .scalars()
            .all()
        )
        totales = {
            fila[0]: (fila[1], fila[2])
            for fila in (
                await session.execute(
                    select(
                        Lead.pipeline_stage_id,
                        func.count(),
                        func.coalesce(func.sum(Lead.estimated_value), 0),
                    )
                    .where(*filtros)
                    .group_by(Lead.pipeline_stage_id)
                )
            ).all()
        }
        puesto = (
            func.row_number()
            .over(
                partition_by=Lead.pipeline_stage_id,
                order_by=(Lead.total_score.desc(), Lead.created_at.desc(), Lead.id),
            )
            .label("puesto")
        )
        ranking = select(Lead.id.label("id"), puesto).where(*filtros).subquery()
        leads = (
            (
                await session.execute(
                    select(Lead)
                    .join(ranking, ranking.c.id == Lead.id)
                    .where(ranking.c.puesto <= per_column)
                    .order_by(Lead.pipeline_stage_id, ranking.c.puesto)
                )
            )
            .scalars()
            .all()
        )
        por_etapa: dict[UUID | None, list[LeadResponse]] = {}
        for lead in leads:
            por_etapa.setdefault(lead.pipeline_stage_id, []).append(
                LeadResponse.model_validate(lead)
            )
        return [
            KanbanColumn(
                stage=StageResponse.model_validate(e),
                leads=por_etapa.get(e.id, []),
                total=totales.get(e.id, (0, Decimal(0)))[0],
                total_value=Decimal(totales.get(e.id, (0, Decimal(0)))[1]),
            )
            for e in etapas
        ]


# ── Listado ──────────────────────────────────────────────────────────────────────────────────


@router.get("", response_model=PaginatedResponse[LeadResponse])
async def list_leads(
    search: str | None = Query(default=None, max_length=100, description="Nombre, empresa o cargo"),
    email: str | None = Query(default=None, max_length=255, description="Email exacto"),
    phone: str | None = Query(default=None, max_length=50, description="Telefono exacto"),
    stage_id: UUID | None = None,
    stage_slug: str | None = Query(default=None, max_length=50),
    status: Literal["active", "won", "lost", "disqualified"] | None = None,
    temperature: Literal["cold", "warm", "hot"] | None = None,
    source_id: UUID | None = None,
    assigned_user_id: UUID | None = None,
    unassigned: bool = False,
    min_score: int | None = Query(default=None, ge=0, le=100),
    max_score: int | None = Query(default=None, ge=0, le=100),
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    follow_up_before: datetime | None = None,
    sort_by: SortField = "created_at",
    order: Literal["asc", "desc"] = "desc",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> PaginatedResponse[LeadResponse]:
    """Lista los leads del tenant (sin los borrados) con filtros, orden y paginacion.

    `email` y `phone` buscan por igualdad a traves del indice ciego (las columnas estan
    cifradas, no admiten `ILIKE`). `search` mira nombre, apellido, empresa y cargo. El orden
    lleva siempre el id como desempate, para que la paginacion no repita ni salte filas.

    Args:
        search: Texto a buscar (sin distinguir mayusculas; `%` y `_` son texto).
        email: Email exacto.
        phone: Telefono exacto (cualquier formato).
        stage_id: Solo esta etapa.
        stage_slug: Solo la etapa con este slug.
        status: Solo este estado.
        temperature: Solo esta temperatura.
        source_id: Solo esta fuente.
        assigned_user_id: Solo los asignados a este usuario.
        unassigned: Solo los sin asignar (excluye `assigned_user_id`).
        min_score: Score total minimo.
        max_score: Score total maximo.
        created_from: Creados desde esta fecha.
        created_to: Creados hasta esta fecha.
        follow_up_before: Con seguimiento programado antes de esta fecha.
        sort_by: Campo de orden.
        order: `asc` o `desc`.
        page: Pagina, desde 1.
        page_size: Tamano de pagina, hasta 100.
        user: Usuario autenticado.

    Returns:
        La pagina de leads y el total de coincidencias.

    Raises:
        AppException: 400 si `min_score` supera a `max_score` o `unassigned` se combina con
            `assigned_user_id`.
    """
    client_id: UUID = user["client_id"]
    if min_score is not None and max_score is not None and min_score > max_score:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message="min_score no puede superar a max_score",
        )
    if unassigned and assigned_user_id is not None:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message="`unassigned` y `assigned_user_id` son excluyentes",
        )

    filtros: list[Any] = [Lead.client_id == client_id, Lead.deleted_at.is_(None)]
    if search:
        patron = _patron(search.strip())
        filtros.append(
            or_(
                Lead.first_name.ilike(patron, escape="\\"),
                Lead.last_name.ilike(patron, escape="\\"),
                Lead.company_name.ilike(patron, escape="\\"),
                Lead.job_title.ilike(patron, escape="\\"),
            )
        )
    if email:
        filtros.append(Lead.email_hash == blind_index(email, client_id))
    digitos = normalizar_telefono_de_lead(phone)
    if digitos:
        filtros.append(Lead.phone_hash == blind_index(digitos, client_id))
    if stage_id is not None:
        filtros.append(Lead.pipeline_stage_id == stage_id)
    if status is not None:
        filtros.append(Lead.status == status)
    if temperature is not None:
        filtros.append(Lead.temperature == temperature)
    if source_id is not None:
        filtros.append(Lead.source_id == source_id)
    if unassigned:
        filtros.append(Lead.assigned_user_id.is_(None))
    elif assigned_user_id is not None:
        filtros.append(Lead.assigned_user_id == assigned_user_id)
    if min_score is not None:
        filtros.append(Lead.total_score >= min_score)
    if max_score is not None:
        filtros.append(Lead.total_score <= max_score)
    if created_from is not None:
        filtros.append(Lead.created_at >= created_from)
    if created_to is not None:
        filtros.append(Lead.created_at <= created_to)
    if follow_up_before is not None:
        filtros.append(Lead.next_follow_up_at <= follow_up_before)

    columna = _ORDEN[sort_by]
    orden = columna.asc().nulls_last() if order == "asc" else columna.desc().nulls_last()

    async with tenant_session(client_id) as session:
        if stage_slug:
            consulta_etapa = select(LeadPipelineStage.id).where(
                LeadPipelineStage.client_id == client_id, LeadPipelineStage.slug == stage_slug
            )
            filtros.append(Lead.pipeline_stage_id.in_(consulta_etapa))
        total = (
            await session.execute(select(func.count()).select_from(Lead).where(*filtros))
        ).scalar_one()
        leads = (
            (
                await session.execute(
                    select(Lead)
                    .where(*filtros)
                    .order_by(orden, Lead.id)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return PaginatedResponse[LeadResponse](
            items=[LeadResponse.model_validate(lead) for lead in leads],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=(total + page_size - 1) // page_size,
        )


# ── Alta, detalle, edicion y borrado ─────────────────────────────────────────────────────────


@router.post("", response_model=LeadResponse, status_code=201)
async def create_lead(
    data: LeadCreate,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> LeadResponse:
    """Crea un lead en la etapa indicada (o en `new`) y lo enlaza al contacto si es inequivoco.

    Args:
        data: Datos del lead; hace falta email, telefono o LinkedIn.
        user: Usuario autenticado.

    Returns:
        El lead creado.

    Raises:
        AppException: 409 `DUPLICATE` si el email o telefono ya existe (con `existing_lead_id`);
            409 si el negocio no tiene pipeline; 400 si etapa, fuente o usuario no existen.
    """
    client_id: UUID = user["client_id"]
    try:
        async with tenant_session(client_id) as session:
            await _validar_referencias(
                session,
                client_id,
                source_id=data.source_id,
                assigned_user_id=data.assigned_user_id,
            )
            etapa = (
                await _etapa_o_400(session, data.pipeline_stage_id, client_id)
                if data.pipeline_stage_id
                else await _etapa_inicial(session, client_id)
            )
            await _comprobar_duplicado(session, client_id, email=data.email, phone=data.phone)

            lead = Lead(
                client_id=client_id,
                pipeline_stage_id=etapa.id,
                last_activity_at=_ahora(),
                **data.model_dump(exclude={"pipeline_stage_id"}),
            )
            session.add(lead)
            await session.flush()

            contacto_id = await contacto_inequivoco(session, client_id, data.email, data.phone)
            if contacto_id is not None:
                contacto = (
                    await session.execute(
                        select(Contact).where(
                            Contact.id == contacto_id, Contact.client_id == client_id
                        )
                    )
                ).scalar_one()
                await vincular(session, lead, contacto)
            respuesta = await _responder(session, lead)
    except IntegrityError as exc:
        if (conflicto := _duplicado_por_carrera(exc)) is not None:
            raise conflicto from exc
        raise
    logger.info("Lead %s creado por %s (tenant %s)", respuesta.id, user["user_id"], client_id)
    return respuesta


@router.get("/{lead_id}", response_model=LeadResponse)
async def get_lead(
    lead_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> LeadResponse:
    """Detalle de un lead.

    Args:
        lead_id: Lead a leer.
        user: Usuario autenticado.

    Returns:
        El lead.

    Raises:
        AppException: 404 si no existe en este tenant o esta borrado.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        return LeadResponse.model_validate(await _lead_o_404(session, lead_id, client_id))


@router.put("/{lead_id}", response_model=LeadResponse)
async def update_lead(
    lead_id: UUID,
    data: LeadUpdate,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> LeadResponse:
    """Cambia los campos enviados de un lead; `null` borra un campo opcional.

    La etapa se cambia con `PATCH /leads/{id}/stage`, y los scores los calculan los Sprints
    17-18: aqui no se aceptan. Quitar el ultimo medio de contacto (email, telefono y LinkedIn)
    se rechaza.

    Args:
        lead_id: Lead a modificar.
        data: Campos a cambiar.
        user: Usuario autenticado.

    Returns:
        El lead actualizado.

    Raises:
        AppException: 404 si no existe; 409 si el nuevo email o telefono ya es de otro lead;
            400 si el usuario no existe o el lead se quedaria sin forma de contacto.
    """
    client_id: UUID = user["client_id"]
    enviados = data.model_fields_set
    try:
        async with tenant_session(client_id) as session:
            lead = await _lead_o_404(session, lead_id, client_id)
            if "assigned_user_id" in enviados and data.assigned_user_id is not None:
                await _validar_referencias(
                    session, client_id, assigned_user_id=data.assigned_user_id
                )

            nuevo_email = data.email if "email" in enviados else lead.email
            nuevo_phone = data.phone if "phone" in enviados else lead.phone
            nuevo_linkedin = data.linkedin_url if "linkedin_url" in enviados else lead.linkedin_url
            if not (nuevo_email or nuevo_phone or nuevo_linkedin):
                raise AppException(
                    status_code=400,
                    error_code=VALIDATION_ERROR,
                    message="El lead se quedaria sin email, telefono ni LinkedIn",
                )
            await _comprobar_duplicado(
                session,
                client_id,
                email=nuevo_email if "email" in enviados else None,
                phone=nuevo_phone if "phone" in enviados else None,
                excluir=lead.id,
            )

            for campo in enviados:
                valor = getattr(data, campo)
                if valor is None and campo in ("currency", "temperature"):
                    continue  # NOT NULL con valor por defecto: `null` no los borra
                setattr(lead, campo, valor)
            await session.flush()
            respuesta = await _responder(session, lead)
    except IntegrityError as exc:
        if (conflicto := _duplicado_por_carrera(exc)) is not None:
            raise conflicto from exc
        raise
    return respuesta


@router.delete("/{lead_id}", status_code=204)
async def delete_lead(
    lead_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_DELETE_ROLES)),
) -> None:
    """Soft delete: el lead sale de listados, Kanban y duplicados, y suelta su contacto.

    No es una supresion de datos personales: sigue en la base hasta que se anonimice con
    `DELETE /admin/leads/{id}/gdpr-delete`.

    Args:
        lead_id: Lead a borrar.
        user: Usuario autenticado; admin, supervisor o super_admin.

    Raises:
        AppException: 404 si no existe o ya estaba borrado.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        lead = await _lead_o_404(session, lead_id, client_id)
        if lead.contact_id is not None:
            contacto = (
                await session.execute(
                    select(Contact).where(
                        Contact.id == lead.contact_id, Contact.client_id == client_id
                    )
                )
            ).scalar_one_or_none()
            if contacto is not None and contacto.lead_id == lead.id:
                contacto.lead_id = None
                contacto.is_lead = False
        lead.deleted_at = _ahora()
    logger.info("Lead %s borrado (soft) por %s (tenant %s)", lead_id, user["user_id"], client_id)


# ── Etapa ────────────────────────────────────────────────────────────────────────────────────


@router.patch("/{lead_id}/stage", response_model=LeadResponse)
async def move_stage(
    lead_id: UUID,
    data: LeadStageMove,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> LeadResponse:
    """Mueve el lead a otra etapa y ajusta su estado segun la etapa de destino.

    - `won`, `lost` y `disqualified` (por slug) fijan el estado correspondiente; `won` sella
      `converted_at` y `disqualified` exige `reason` y sella `disqualified_at`.
    - Mover a cualquier etapa **no terminal** devuelve el lead a `active` y limpia esas marcas
      (reabrir un lead perdido).
    - Una etapa terminal personalizada deja el estado como estaba.

    Args:
        lead_id: Lead a mover.
        data: Etapa de destino y motivo.
        user: Usuario autenticado.

    Returns:
        El lead movido.

    Raises:
        AppException: 404 si el lead no existe; 400 si la etapa no existe o falta el motivo de
            descalificacion.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        lead = await _lead_o_404(session, lead_id, client_id)
        etapa = await _etapa_o_400(session, data.stage_id, client_id)
        estado = _ESTADO_POR_SLUG.get(etapa.slug)
        if estado == "disqualified" and not (data.reason and data.reason.strip()):
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message="Indica el motivo para descalificar al lead",
            )

        ahora = _ahora()
        lead.pipeline_stage_id = etapa.id
        lead.last_activity_at = ahora
        if estado is not None:
            lead.status = estado
        elif not etapa.is_terminal:
            lead.status = "active"
        lead.converted_at = ahora if estado == "won" else None
        if estado == "disqualified":
            lead.disqualified_at = ahora
            lead.disqualified_reason = (data.reason or "").strip()
        else:
            lead.disqualified_at = None
            lead.disqualified_reason = None
        await session.flush()
        return await _responder(session, lead)


# ── Enlace con contactos ─────────────────────────────────────────────────────────────────────


@router.put("/{lead_id}/contact", response_model=LeadResponse)
async def link_contact(
    lead_id: UUID,
    data: LeadContactLink,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> LeadResponse:
    """Enlaza el lead con un contacto existente del mismo negocio.

    Args:
        lead_id: Lead.
        data: Contacto con el que se enlaza.
        user: Usuario autenticado.

    Returns:
        El lead enlazado.

    Raises:
        AppException: 404 si el lead no existe; 400 si el contacto no existe, esta fusionado o
            anonimizado; 409 si el contacto ya esta enlazado a otro lead.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        lead = await _lead_o_404(session, lead_id, client_id)
        contacto = (
            await session.execute(
                select(Contact).where(Contact.id == data.contact_id, Contact.client_id == client_id)
            )
        ).scalar_one_or_none()
        if contacto is None or contacto.merged_into_id is not None or contacto.is_gdpr_deleted:
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message="El contacto no existe, esta fusionado o fue anonimizado",
            )
        try:
            await vincular(session, lead, contacto)
        except ContactoYaVinculadoError as exc:
            raise AppException(
                status_code=409,
                error_code=DUPLICATE,
                message="El contacto ya esta vinculado a otro lead",
                details={"existing_lead_id": str(exc.lead_id)},
            ) from exc
        return await _responder(session, lead)


@router.post("/{lead_id}/contact", response_model=LeadResponse, status_code=201)
async def create_contact_from_lead(
    lead_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> LeadResponse:
    """Crea un contacto con los datos del lead (con su email y telefono como identificadores).

    Args:
        lead_id: Lead de origen.
        user: Usuario autenticado.

    Returns:
        El lead, ya enlazado al contacto nuevo.

    Raises:
        AppException: 404 si el lead no existe; 409 si ya tiene contacto o si ya existe un
            contacto con ese email o telefono (hay que enlazarlo, no duplicarlo).
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        lead = await _lead_o_404(session, lead_id, client_id)
        if lead.contact_id is not None:
            raise AppException(
                status_code=409, error_code=DUPLICATE, message="El lead ya tiene un contacto"
            )
        if await contactos_candidatos(session, client_id, lead.email, lead.phone):
            raise AppException(
                status_code=409,
                error_code=DUPLICATE,
                message="Ya existe un contacto con ese email o telefono: enlazalo en vez de crear otro",
            )
        await crear_contacto_desde_lead(session, lead)
        return await _responder(session, lead)


@router.delete("/{lead_id}/contact", response_model=LeadResponse)
async def unlink_contact(
    lead_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> LeadResponse:
    """Suelta el enlace con el contacto (no borra ninguno de los dos).

    Args:
        lead_id: Lead.
        user: Usuario autenticado.

    Returns:
        El lead sin contacto (sin error si no tenia).

    Raises:
        AppException: 404 si el lead no existe.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        lead = await _lead_o_404(session, lead_id, client_id)
        await desvincular(session, lead)
        return await _responder(session, lead)
