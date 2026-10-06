"""Etapas del pipeline de leads (Sprint 16, ADR-083).

GET    /api/v1/lead-pipeline-stages            las etapas del tenant, en orden
POST   /api/v1/lead-pipeline-stages            crea una (al final, o en la posicion indicada)
PUT    /api/v1/lead-pipeline-stages/order      reordena: recibe todas las etapas en el orden nuevo
PUT    /api/v1/lead-pipeline-stages/{id}       cambia nombre, color, acciones o si es terminal
DELETE /api/v1/lead-pipeline-stages/{id}       la borra si ningun lead la usa

Leer lo puede cualquier rol operativo; configurar el pipeline, solo `admin` y `super_admin`.
El `slug` no se cambia: las automatizaciones de los Sprints 17-19 lo referencian.

Las posiciones son un `UNIQUE DEFERRABLE` (migracion 025): dentro de una transaccion se pueden
dejar repetidas de forma transitoria, y se comprueban al cerrarla. Por eso insertar en medio o
reordenar es un `UPDATE` normal, sin trucos de posiciones negativas.
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.dependencies import require_leads_module
from app.core.exceptions import CONFLICT, NOT_FOUND, VALIDATION_ERROR, AppException
from app.models.lead import Lead
from app.models.lead_pipeline_stage import LeadPipelineStage
from app.schemas.lead import StageCreate, StageReorder, StageResponse, StageUpdate

logger = logging.getLogger(__name__)

router = APIRouter()

_READ_ROLES = ("super_admin", "admin", "supervisor", "agent")
_CONFIG_ROLES = ("super_admin", "admin")


async def _etapas(session: AsyncSession, client_id: UUID) -> list[LeadPipelineStage]:
    resultado: list[LeadPipelineStage] = list(
        (
            await session.execute(
                select(LeadPipelineStage)
                .where(LeadPipelineStage.client_id == client_id)
                .order_by(LeadPipelineStage.position)
            )
        ).scalars()
    )
    return resultado


async def _etapa_o_404(session: AsyncSession, stage_id: UUID, client_id: UUID) -> LeadPipelineStage:
    etapa: LeadPipelineStage | None = (
        await session.execute(
            select(LeadPipelineStage).where(
                LeadPipelineStage.id == stage_id, LeadPipelineStage.client_id == client_id
            )
        )
    ).scalar_one_or_none()
    if etapa is None:
        raise AppException(status_code=404, error_code=NOT_FOUND, message="Etapa no encontrada")
    return etapa


@router.get("", response_model=list[StageResponse])
async def list_stages(
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> list[StageResponse]:
    """Lista las etapas del pipeline, ordenadas por posicion.

    Args:
        user: Usuario autenticado.

    Returns:
        Las etapas del tenant (vacio si todavia no tiene pipeline).
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        return [StageResponse.model_validate(e) for e in await _etapas(session, client_id)]


@router.post("", response_model=StageResponse, status_code=201)
async def create_stage(
    data: StageCreate,
    user: dict[str, Any] = Depends(require_leads_module(*_CONFIG_ROLES)),
) -> StageResponse:
    """Crea una etapa; las que ocupan su posicion o van detras se desplazan una.

    Args:
        data: Datos de la etapa. Sin `position` va al final.
        user: Usuario autenticado; solo admin o super_admin.

    Returns:
        La etapa creada.

    Raises:
        AppException: 409 si ya hay una etapa con ese slug; 400 si la posicion deja un hueco.
    """
    client_id: UUID = user["client_id"]
    try:
        async with tenant_session(client_id) as session:
            existentes = await _etapas(session, client_id)
            posicion = len(existentes) if data.position is None else data.position
            if posicion > len(existentes):
                raise AppException(
                    status_code=400,
                    error_code=VALIDATION_ERROR,
                    message=f"La posicion maxima es {len(existentes)}",
                )
            await session.execute(
                update(LeadPipelineStage)
                .where(
                    LeadPipelineStage.client_id == client_id, LeadPipelineStage.position >= posicion
                )
                .values(position=LeadPipelineStage.position + 1)
            )
            etapa = LeadPipelineStage(
                client_id=client_id,
                name=data.name,
                slug=data.slug,
                position=posicion,
                color=data.color,
                auto_actions=data.auto_actions,
                is_terminal=data.is_terminal,
            )
            session.add(etapa)
            await session.flush()
            respuesta = StageResponse.model_validate(etapa)
    except IntegrityError as exc:
        if "uq_lead_stage_slug" not in str(exc.orig):
            raise
        raise AppException(
            status_code=409,
            error_code=CONFLICT,
            message=f"Ya existe una etapa con el slug {data.slug!r}",
        ) from exc
    return respuesta


@router.put("/order", response_model=list[StageResponse])
async def reorder_stages(
    data: StageReorder,
    user: dict[str, Any] = Depends(require_leads_module(*_CONFIG_ROLES)),
) -> list[StageResponse]:
    """Reordena el pipeline: recibe **todas** las etapas del tenant en el orden nuevo.

    Pedir todas (y no un par) evita que dos reordenaciones simultaneas dejen huecos o
    posiciones repetidas: o coincide el conjunto de etapas o se rechaza.

    Args:
        data: Ids de las etapas en el orden deseado.
        user: Usuario autenticado; solo admin o super_admin.

    Returns:
        Las etapas ya reordenadas.

    Raises:
        AppException: 400 si faltan etapas, sobran o alguna no es del tenant.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        existentes = await _etapas(session, client_id)
        if {e.id for e in existentes} != set(data.stage_ids):
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message="Envia todas las etapas del pipeline, sin repetir ni anadir otras",
            )
        orden = {stage_id: i for i, stage_id in enumerate(data.stage_ids)}
        await session.execute(
            update(LeadPipelineStage)
            .where(LeadPipelineStage.client_id == client_id)
            .values(
                position=case(
                    *[(LeadPipelineStage.id == sid, pos) for sid, pos in orden.items()],
                    else_=LeadPipelineStage.position,
                )
            )
        )
        session.expire_all()
        return [StageResponse.model_validate(e) for e in await _etapas(session, client_id)]


@router.put("/{stage_id}", response_model=StageResponse)
async def update_stage(
    stage_id: UUID,
    data: StageUpdate,
    user: dict[str, Any] = Depends(require_leads_module(*_CONFIG_ROLES)),
) -> StageResponse:
    """Cambia nombre, color, acciones automaticas o si la etapa es terminal.

    Args:
        stage_id: Etapa a modificar.
        data: Campos a cambiar; `null` en `color` lo quita.
        user: Usuario autenticado; solo admin o super_admin.

    Returns:
        La etapa actualizada.

    Raises:
        AppException: 404 si no existe en este tenant.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        etapa = await _etapa_o_404(session, stage_id, client_id)
        for campo in data.model_fields_set:
            valor = getattr(data, campo)
            if campo in ("name", "auto_actions", "is_terminal") and valor is None:
                continue  # columnas NOT NULL: `null` no las borra
            setattr(etapa, campo, valor)
        await session.flush()
        return StageResponse.model_validate(etapa)


@router.delete("/{stage_id}", status_code=204)
async def delete_stage(
    stage_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_CONFIG_ROLES)),
) -> None:
    """Borra una etapa que ningun lead usa y cierra el hueco de posiciones.

    Cuenta tambien los leads con soft delete: la FK los sigue apuntando.

    Args:
        stage_id: Etapa a borrar.
        user: Usuario autenticado; solo admin o super_admin.

    Raises:
        AppException: 404 si no existe; 409 si algun lead la usa (hay que moverlos antes).
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        etapa = await _etapa_o_404(session, stage_id, client_id)
        usados = (
            await session.execute(
                select(func.count())
                .select_from(Lead)
                .where(Lead.client_id == client_id, Lead.pipeline_stage_id == stage_id)
            )
        ).scalar_one()
        if usados:
            raise AppException(
                status_code=409,
                error_code=CONFLICT,
                message=f"La etapa tiene {usados} lead(s): muevelos a otra etapa antes de borrarla",
            )
        posicion = etapa.position
        await session.delete(etapa)
        await session.flush()
        await session.execute(
            update(LeadPipelineStage)
            .where(LeadPipelineStage.client_id == client_id, LeadPipelineStage.position > posicion)
            .values(position=LeadPipelineStage.position - 1)
        )
    logger.info("Etapa %s borrada por %s (tenant %s)", stage_id, user["user_id"], client_id)
