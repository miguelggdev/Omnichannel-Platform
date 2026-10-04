"""Perfil del negocio del tenant (Sprint 15, fase 2).

GET /api/v1/admin/business-profile   el perfil completo
PUT /api/v1/admin/business-profile   cambia los campos que se envien

Donde vive cada cosa (sin migracion, todo en columnas que ya existen):

- `business_name`            -> `clients.name`
- colores y `logo_url`       -> `clients.theme_config`
- el resto de datos          -> `clients.settings["business_profile"]`
- `welcome_message` y
  `handoff_message`          -> el `agent_configs` activo, que es el que lee el asistente

De todo esto el asistente solo **usa** hoy los dos mensajes. El horario, los datos de
contacto y los colores se guardan para que otras funciones (fuera de horario, el widget, la
marca del panel) los lean; hasta entonces no cambian el comportamiento de la plataforma.
"""

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import CONFLICT, NOT_FOUND, AppException
from app.models.agent_config import AgentConfig
from app.models.client import Client
from app.schemas.business_profile import (
    DIAS,
    BusinessProfile,
    BusinessProfileUpdate,
    DaySchedule,
    _horario_por_defecto,
)

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)

router = APIRouter()

_ROLES = ("super_admin", "admin")
_CLAVE = "business_profile"

# Campos de `settings["business_profile"]`, los que no tienen otra casa.
_CAMPOS_DE_PERFIL = (
    "business_type",
    "description",
    "phone",
    "email",
    "website",
    "address",
    "city",
    "country",
    "timezone",
)
_CAMPOS_DE_TEMA = ("primary_color", "secondary_color", "logo_url")
_CAMPOS_DE_AGENTE = ("welcome_message", "handoff_message")


async def _agente_activo(session: Any) -> AgentConfig | None:
    """El `agent_configs` que lee el asistente: el activo mas antiguo (como `get_agent_settings`)."""
    agente: AgentConfig | None = (
        await session.execute(
            select(AgentConfig)
            .where(AgentConfig.is_active.is_(True))
            .order_by(AgentConfig.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return agente


def _componer(cliente: Client, agente: AgentConfig | None) -> BusinessProfile:
    """Arma el perfil a partir de las tres fuentes."""
    perfil: dict[str, Any] = (cliente.settings or {}).get(_CLAVE, {})
    tema: dict[str, Any] = cliente.theme_config or {}
    horario = _horario_por_defecto()
    for dia in DIAS:
        guardado = (perfil.get("operating_hours") or {}).get(dia)
        if guardado:
            horario[dia] = DaySchedule.model_validate(guardado)
    return BusinessProfile(
        business_name=cliente.name,
        social_media=perfil.get("social_media") or {},
        operating_hours=horario,
        has_agent=agente is not None,
        welcome_message=agente.welcome_message if agente else None,
        handoff_message=agente.handoff_message if agente else None,
        **{campo: perfil.get(campo) for campo in _CAMPOS_DE_PERFIL},
        **{campo: tema.get(campo) for campo in _CAMPOS_DE_TEMA},
    )


async def _cargar_cliente(session: Any, client_id: "UUID") -> Client:
    cliente: Client | None = (
        await session.execute(select(Client).where(Client.id == client_id))
    ).scalar_one_or_none()
    if cliente is None:
        raise AppException(status_code=404, error_code=NOT_FOUND, message="Negocio no encontrado")
    return cliente


@router.get("", response_model=BusinessProfile)
async def get_business_profile(
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> BusinessProfile:
    """Devuelve el perfil del negocio.

    Args:
        user: Usuario autenticado; solo admin o super_admin.

    Returns:
        El perfil; lo que no se ha configurado es `null` (y el horario, el de por defecto).
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        cliente = await _cargar_cliente(session, client_id)
        agente = await _agente_activo(session)
        return _componer(cliente, agente)


@router.put("", response_model=BusinessProfile)
async def update_business_profile(
    data: BusinessProfileUpdate,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> BusinessProfile:
    """Cambia los campos enviados del perfil y devuelve el perfil completo.

    `social_media` y `operating_hours` se mezclan por clave (un dia o una red a la vez), y los
    demas campos se reemplazan. Un campo enviado como `null` se borra.

    Args:
        data: Campos a cambiar.
        user: Usuario autenticado; solo admin o super_admin.

    Returns:
        El perfil ya actualizado.

    Raises:
        AppException: 404 si el negocio no existe; 409 si se envia `welcome_message` o
            `handoff_message` y el tenant no tiene un agente activo donde guardarlos.
    """
    client_id: UUID = user["client_id"]
    enviados = data.model_fields_set
    # Las URL llegan como objetos de pydantic; en el JSONB se guardan como texto.
    cambios = {
        campo: (str(valor) if valor is not None and campo in ("website", "logo_url") else valor)
        for campo in enviados
        for valor in (getattr(data, campo),)
    }

    async with tenant_session(client_id) as session:
        cliente = await _cargar_cliente(session, client_id)
        agente = await _agente_activo(session)

        if any(c in enviados for c in _CAMPOS_DE_AGENTE) and agente is None:
            raise AppException(
                status_code=409,
                error_code=CONFLICT,
                message="El tenant no tiene un agente activo donde guardar los mensajes",
            )

        if "business_name" in enviados:
            cliente.name = data.business_name or cliente.name

        perfil = dict((cliente.settings or {}).get(_CLAVE, {}))
        for campo in _CAMPOS_DE_PERFIL:
            if campo in enviados:
                perfil[campo] = cambios[campo]
        if "social_media" in enviados:
            redes = dict(perfil.get("social_media") or {})
            if data.social_media is None:
                redes = {}
            else:
                for red in data.social_media.model_fields_set:
                    valor = getattr(data.social_media, red)
                    redes[red] = str(valor) if valor is not None else None
            perfil["social_media"] = {k: v for k, v in redes.items() if v is not None}
        if "operating_hours" in enviados:
            horario = dict(perfil.get("operating_hours") or {})
            if data.operating_hours is not None:
                for dia in data.operating_hours.model_fields_set:
                    valor = getattr(data.operating_hours, dia)
                    if valor is not None:
                        horario[dia] = valor.model_dump()
            perfil["operating_hours"] = horario
        # Se asigna un dict nuevo: SQLAlchemy no detecta la mutacion en sitio de un JSONB.
        cliente.settings = {**(cliente.settings or {}), _CLAVE: perfil}

        tema = dict(cliente.theme_config or {})
        for campo in _CAMPOS_DE_TEMA:
            if campo in enviados:
                tema[campo] = cambios[campo]
        cliente.theme_config = {k: v for k, v in tema.items() if v is not None}

        if agente is not None:
            for campo in _CAMPOS_DE_AGENTE:
                if campo in enviados:
                    setattr(agente, campo, cambios[campo])

        await session.flush()
        respuesta = _componer(cliente, agente)

    logger.info(
        "Perfil del negocio %s modificado por %s: campos=%s",
        client_id,
        user.get("user_id"),
        sorted(enviados),
    )
    return respuesta
