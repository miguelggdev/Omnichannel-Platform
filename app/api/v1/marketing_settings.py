"""Configuracion del agente de marketing (ADR-070).

GET /api/v1/marketing/settings   operadores y plantillas aprobadas vigentes
PUT /api/v1/marketing/settings   reemplaza las listas que vengan en el body

Hasta ahora `marketing.operator_contact_ids` (BUG-045) y
`marketing.approved_templates` (ADR-067) solo se podian cambiar editando
`agent_configs` a mano: un tenant que habilitaba el agente de marketing no
tenia forma de autorizar a nadie a usarlo.

Solo `super_admin` y `admin`: quien decide quien puede lanzar envios masivos
es quien administra el tenant, no un agente de atencion.

La escritura es un `UPDATE` con concatenacion JSONB sobre la clave
`marketing`, no un read-modify-write del `config` entero: esa columna guarda
tambien `enabled_agents`, los umbrales del RAG y demas, y otro cambio
concurrente no puede perderse (misma leccion que BUG-022).
"""

import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import VALIDATION_ERROR, AppException
from app.models.agent_config import AgentConfig
from app.models.contact import Contact
from app.schemas.marketing_settings import MarketingSettings, MarketingSettingsUpdate
from app.services.campaigns import leer_config_marketing

logger = logging.getLogger(__name__)

router = APIRouter()

_ROLES = ("super_admin", "admin")

#: Mezcla el parche dentro de `config.marketing` sin tocar el resto de `config`.
_ACTUALIZAR_MARKETING = text(
    """
    UPDATE agent_configs
    SET config = COALESCE(config, '{}'::jsonb) || jsonb_build_object(
        'marketing',
        COALESCE(config -> 'marketing', '{}'::jsonb) || CAST(:parche AS jsonb)
    )
    WHERE id = :id AND client_id = :client_id
    """
)


def _a_respuesta(marketing: dict[str, Any]) -> MarketingSettings:
    """Normaliza lo guardado en el JSONB a la respuesta de la API.

    Lo que no tenga forma valida (escrito a mano en la base) se descarta en vez
    de romper el GET.

    Args:
        marketing: `agent_configs.config.marketing`.

    Returns:
        La configuracion vigente.
    """
    operadores: list[UUID] = []
    crudos = marketing.get("operator_contact_ids")
    for valor in crudos if isinstance(crudos, list) else []:
        try:
            operadores.append(UUID(str(valor)))
        except ValueError:
            continue
    plantillas = marketing.get("approved_templates")
    return MarketingSettings(
        operator_contact_ids=operadores,
        approved_templates=[str(p) for p in plantillas] if isinstance(plantillas, list) else [],
    )


@router.get("/settings", response_model=MarketingSettings)
async def get_marketing_settings(
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> MarketingSettings:
    """Devuelve la configuracion vigente del agente de marketing.

    Args:
        user: Usuario autenticado.

    Returns:
        Operadores y plantillas aprobadas.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        return _a_respuesta(await leer_config_marketing(session, client_id))


@router.put("/settings", response_model=MarketingSettings)
async def update_marketing_settings(
    data: MarketingSettingsUpdate,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> MarketingSettings:
    """Reemplaza las listas de operadores y/o plantillas aprobadas.

    Args:
        data: Listas nuevas; las que no vengan quedan como estaban.
        user: Usuario autenticado.

    Returns:
        La configuracion resultante.

    Raises:
        AppException: 400 si el tenant no tiene agente activo o si algun
            operador no es un contacto vigente del tenant.
    """
    client_id: UUID = user["client_id"]
    parche: dict[str, Any] = {}
    if data.operator_contact_ids is not None:
        parche["operator_contact_ids"] = [str(c) for c in data.operator_contact_ids]
    if data.approved_templates is not None:
        parche["approved_templates"] = data.approved_templates

    async with tenant_session(client_id) as session:
        config_id = (
            await session.execute(
                select(AgentConfig.id)
                .where(AgentConfig.client_id == client_id, AgentConfig.is_active.is_(True))
                .order_by(AgentConfig.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if config_id is None:
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message="El tenant no tiene un agente activo que configurar",
            )

        if data.operator_contact_ids:
            # Un operador tiene que ser un contacto vigente de ESTE tenant: un
            # id de otro tenant, uno fusionado o uno borrado por RGPD nunca
            # podria escribir, y dejarlo en la lista solo confunde.
            vigentes = set(
                (
                    await session.execute(
                        select(Contact.id).where(
                            Contact.client_id == client_id,
                            Contact.id.in_(data.operator_contact_ids),
                            Contact.merged_into_id.is_(None),
                            Contact.is_gdpr_deleted.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )
            desconocidos = [str(c) for c in data.operator_contact_ids if c not in vigentes]
            if desconocidos:
                raise AppException(
                    status_code=400,
                    error_code=VALIDATION_ERROR,
                    message=f"Contactos inexistentes en este tenant: {', '.join(desconocidos)}",
                )

        if parche:
            await session.execute(
                _ACTUALIZAR_MARKETING,
                {"parche": json.dumps(parche), "id": str(config_id), "client_id": str(client_id)},
            )
        respuesta = _a_respuesta(await leer_config_marketing(session, client_id))

    logger.info(
        "Configuracion de marketing del tenant %s actualizada por %s (%s)",
        client_id,
        user.get("user_id"),
        ", ".join(sorted(parche)) or "sin cambios",
    )
    return respuesta
