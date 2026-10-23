"""Configuracion de scoring y automatizacion de leads del tenant (Sprint 17, Dev B).

GET/PUT /api/v1/clients/icp              perfil de cliente ideal (FIT)
GET/PUT /api/v1/clients/lead-automation  calificacion automatica y asignacion round-robin

Leer es de `supervisor` para arriba; cambiar, de `admin` y `super_admin`. Cambiar el ICP encola
el recalculo del FIT de los leads activos (tarea `app.tasks.enrichment_recalc_fit_icp`): hacerlo
en la peticion bloquearia a un tenant con miles de leads.
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.core.database import tenant_session
from app.core.dependencies import require_leads_module
from app.core.exceptions import VALIDATION_ERROR, AppException
from app.models.client import Client
from app.schemas.lead_automation import IcpUpdateResponse, LeadAutomationConfig
from app.schemas.lead_scoring import IcpConfig
from app.services.lead_assignment import CLAVE_CURSOR, candidatos
from app.services.lead_fit import cargar_icp
from app.services.lead_qualification import cargar_automatizacion, validar_reglas

logger = logging.getLogger(__name__)

router = APIRouter()

_READ_ROLES = ("super_admin", "admin", "supervisor")
_WRITE_ROLES = ("super_admin", "admin")


def _uid(user: dict[str, Any]) -> UUID:
    return UUID(str(user["user_id"]))


@router.get("/icp", response_model=IcpConfig)
async def get_icp(
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> IcpConfig:
    """El ICP del tenant (vacio si no lo ha configurado).

    Args:
        user: Usuario autenticado.

    Returns:
        El ICP guardado, o uno vacio.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        bruto = (
            await session.execute(select(Client.icp_config).where(Client.id == client_id))
        ).scalar_one_or_none()
    return cargar_icp(bruto, client_id=client_id) or IcpConfig()


@router.put("/icp", response_model=IcpUpdateResponse)
async def put_icp(
    data: IcpConfig,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> IcpUpdateResponse:
    """Guarda el ICP del tenant y encola el recalculo del FIT de sus leads activos.

    Args:
        data: ICP completo (sustituye al anterior).
        user: Usuario autenticado.

    Returns:
        El ICP guardado y si se encolo el recalculo.

    Raises:
        AppException: 422 si el ICP no es valido (contradicciones, pesos que suman 0...).
    """
    client_id: UUID = user["client_id"]
    nuevo = data.model_dump(mode="json")
    async with tenant_session(client_id, user_id=_uid(user)) as session:
        cliente = (await session.execute(select(Client).where(Client.id == client_id))).scalar_one()
        cambia = (cliente.icp_config or {}) != nuevo
        cliente.icp_config = nuevo
    if not cambia:
        return IcpUpdateResponse(icp=data, recalculation="not_needed")

    from app.tasks.enrichment_tasks import recalc_fit_icp

    try:
        recalc_fit_icp.delay(str(client_id), str(_uid(user)))
    except Exception:
        # El ICP ya esta guardado: no se deshace por no poder encolar el recalculo.
        logger.exception("No se pudo encolar el recalculo del FIT (client_id=%s)", client_id)
        return IcpUpdateResponse(icp=data, recalculation="unavailable")
    return IcpUpdateResponse(icp=data, recalculation="queued")


@router.get("/lead-automation", response_model=LeadAutomationConfig)
async def get_lead_automation(
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> LeadAutomationConfig:
    """Calificacion automatica y asignacion del tenant (todo apagado si no la configuro).

    Args:
        user: Usuario autenticado.

    Returns:
        La configuracion vigente.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        bruto = (
            await session.execute(
                select(Client.lead_automation_config).where(Client.id == client_id)
            )
        ).scalar_one_or_none()
    return cargar_automatizacion(bruto, client_id=client_id)


@router.put("/lead-automation", response_model=LeadAutomationConfig)
async def put_lead_automation(
    data: LeadAutomationConfig,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> LeadAutomationConfig:
    """Guarda la calificacion automatica y la asignacion del tenant.

    Las etapas de las reglas deben existir y no ser terminales; los usuarios de `user_ids` deben
    ser agentes o supervisores activos del tenant.

    Args:
        data: Configuracion completa (sustituye a la anterior).
        user: Usuario autenticado.

    Returns:
        La configuracion guardada.

    Raises:
        AppException: 400 si una etapa o un usuario no son validos.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id, user_id=_uid(user)) as session:
        problemas = await validar_reglas(session, client_id, data.qualification)
        if data.assignment.user_ids is not None:
            elegibles = set(await candidatos(session, client_id, data.assignment))
            ajenos = [u for u in data.assignment.user_ids if u not in elegibles]
            if ajenos:
                problemas.append(
                    f"{len(ajenos)} usuario(s) no existen, estan desactivados o no son "
                    "agentes ni supervisores"
                )
        if problemas:
            raise AppException(
                status_code=400, error_code=VALIDATION_ERROR, message="; ".join(problemas)
            )
        cliente = (await session.execute(select(Client).where(Client.id == client_id))).scalar_one()
        nuevo = data.model_dump(mode="json")
        cursor = (cliente.lead_automation_config or {}).get(CLAVE_CURSOR)
        if cursor is not None:
            nuevo[CLAVE_CURSOR] = cursor  # lo escribe el servidor, no esta API
        cliente.lead_automation_config = nuevo
    return data
