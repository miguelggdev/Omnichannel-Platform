"""Activacion del modulo de leads (Sprint 16, ADR-083).

GET /api/v1/lead-management/status                   estado del modulo para el tenant autenticado
PUT /api/v1/platform/clients/{client_id}/lead-management   lo activa o desactiva (solo super_admin)

Quien lo activa es el operador de la plataforma, no el admin del tenant: es una decision
comercial del plan (como `enable_sandbox`, ADR-078/079), y un admin que pudiera encenderlo
se daria a si mismo un modulo que no contrato. Al activarlo se crean las etapas por defecto del
pipeline si el tenant aun no tiene ninguna; al apagarlo **no se borra nada**: los leads y el
pipeline se conservan y el modulo responde 403 `MODULE_DISABLED` hasta que se reactive.
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import NOT_FOUND, AppException
from app.models.client import Client
from app.models.lead_pipeline_stage import LeadPipelineStage
from app.schemas.lead import LeadModuleStatus, LeadModuleUpdate, LeadModuleUpdated
from app.services.lead_pipeline import ensure_default_stages

logger = logging.getLogger(__name__)

router = APIRouter()
platform_router = APIRouter()

_READ_ROLES = ("super_admin", "admin", "supervisor", "agent")


@router.get("/status", response_model=LeadModuleStatus)
async def lead_module_status(
    user: dict[str, Any] = Depends(require_role(*_READ_ROLES)),
) -> LeadModuleStatus:
    """Si el tenant tiene el modulo de leads y cuantas etapas tiene su pipeline.

    Es lo que el frontend consulta para decidir si muestra la seccion de leads.

    Args:
        user: Usuario autenticado.

    Returns:
        Estado del modulo.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        activo = (
            await session.execute(
                select(Client.lead_management_enabled).where(Client.id == client_id)
            )
        ).scalar_one_or_none()
        etapas = (
            await session.execute(
                select(func.count())
                .select_from(LeadPipelineStage)
                .where(LeadPipelineStage.client_id == client_id)
            )
        ).scalar_one()
    return LeadModuleStatus(enabled=bool(activo), stages=etapas)


@platform_router.put("/clients/{client_id}/lead-management", response_model=LeadModuleUpdated)
async def set_lead_module(
    client_id: UUID,
    data: LeadModuleUpdate,
    user: dict[str, Any] = Depends(require_role("super_admin")),
) -> LeadModuleUpdated:
    """Activa o desactiva el modulo de leads de un cliente.

    Usa `tenant_session(client_id)` con el tenant elegido (el mismo cambio de contexto que el
    resto de acciones del operador sobre un cliente); RLS se cumple en cada paso.

    Args:
        client_id: Cliente a modificar.
        data: Estado deseado.
        user: Usuario autenticado; solo super_admin.

    Returns:
        Estado final y cuantas etapas por defecto se crearon.

    Raises:
        AppException: 404 si el cliente no existe.
    """
    creadas = 0
    async with tenant_session(client_id) as session:
        cliente = (
            await session.execute(select(Client).where(Client.id == client_id))
        ).scalar_one_or_none()
        if cliente is None:
            raise AppException(
                status_code=404, error_code=NOT_FOUND, message="Cliente no encontrado"
            )
        cliente.lead_management_enabled = data.enabled
        if data.enabled:
            antes = (
                await session.execute(
                    select(func.count())
                    .select_from(LeadPipelineStage)
                    .where(LeadPipelineStage.client_id == client_id)
                )
            ).scalar_one()
            etapas = await ensure_default_stages(session, client_id)
            creadas = len(etapas) - antes
    logger.info(
        "Modulo de leads del cliente %s %s por %s (etapas creadas: %s)",
        client_id,
        "activado" if data.enabled else "desactivado",
        user["user_id"],
        creadas,
    )
    return LeadModuleUpdated(client_id=client_id, enabled=data.enabled, stages_created=creadas)
