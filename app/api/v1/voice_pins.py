"""API de los PIN de voz: `PUT/DELETE /api/v1/voice/pins` (Sprint 13, ADR-073).

Un profesional (o un operador de marketing) que quiera usar los agentes por
telefono necesita un PIN, porque el caller ID se puede falsificar. Lo registra
un `admin`; el PIN viaja una vez en el cuerpo, se guarda con hash y **nunca** se
devuelve ni se registra en un log.

El contacto tiene que estar ya declarado profesional
(`PUT /api/v1/clinical/settings`) u operador de marketing
(`PUT /api/v1/marketing/settings`): un PIN para alguien que ningun agente
autoriza no abre nada, y dejarlo registrado solo confunde.
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Response

from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import VALIDATION_ERROR, AppException
from app.schemas.voice import VoicePinSet
from app.services.campaigns import es_operador_de_marketing
from app.services.clinical import es_profesional_clinico
from app.services.voice.pin_auth import PinInvalidoError, fijar_pin, quitar_pines

logger = logging.getLogger(__name__)

router = APIRouter()

_ROLES = ("super_admin", "admin")


@router.put("/pins", status_code=204)
async def set_voice_pin(
    data: VoicePinSet,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> Response:
    """Registra o reemplaza el PIN con el que un contacto autorizado se identifica por voz.

    Args:
        data: Contacto, numero desde el que llamara y PIN.
        user: Usuario autenticado.

    Returns:
        204 sin cuerpo.

    Raises:
        AppException: 400 si el PIN o el telefono no son validos, o si el contacto
            no es profesional ni operador de marketing de este tenant.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        autorizado = await es_profesional_clinico(
            session, client_id, data.contact_id
        ) or await es_operador_de_marketing(session, client_id, str(data.contact_id))
        if not autorizado:
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message="El contacto no es profesional ni operador de marketing de este tenant",
            )
        try:
            await fijar_pin(
                session, client_id, data.contact_id, data.phone, data.pin.get_secret_value()
            )
        except PinInvalidoError as exc:
            raise AppException(
                status_code=400, error_code=VALIDATION_ERROR, message=str(exc)
            ) from exc
    logger.info(
        "PIN de voz del contacto %s del tenant %s fijado por %s",
        data.contact_id,
        client_id,
        user.get("user_id"),
    )
    return Response(status_code=204)


@router.delete("/pins/{contact_id}", status_code=204)
async def delete_voice_pins(
    contact_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> Response:
    """Borra los PIN de voz de un contacto (de todos sus numeros).

    Args:
        contact_id: Contacto.
        user: Usuario autenticado.

    Returns:
        204 sin cuerpo, tenga o no PIN registrados.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        borrados = await quitar_pines(session, client_id, contact_id)
    logger.info(
        "PIN de voz del contacto %s del tenant %s borrado por %s (%d)",
        contact_id,
        client_id,
        user.get("user_id"),
        borrados,
    )
    return Response(status_code=204)
