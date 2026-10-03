"""Endpoints de feature flags del tenant (Sprint 14, ADR-076).

    GET  /api/v1/admin/feature-flags          lista las flags y su valor
    PUT  /api/v1/admin/feature-flags/{flag}   cambia una (bool, o 0-100 en las que lo admiten)

Solo `admin` y `super_admin`. El tenant sale del token.
"""

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends

from app.core.dependencies import require_role
from app.core.exceptions import NOT_FOUND, VALIDATION_ERROR, AppException
from app.core.feature_flags import (
    KNOWN_FLAGS,
    UNENFORCED_FLAGS,
    FeatureFlags,
    SinAgenteConfigurableError,
    validar_valor,
)
from app.schemas.feature_flags import FeatureFlagItem, FeatureFlagsResponse, FeatureFlagUpdate

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)

router = APIRouter()

_ROLES = ("super_admin", "admin")


def _respuesta(guardadas: dict[str, bool | int]) -> FeatureFlagsResponse:
    """Une las flags conocidas con lo que el tenant tiene guardado."""
    return FeatureFlagsResponse(
        flags=[
            FeatureFlagItem(
                flag=flag,
                value=guardadas.get(flag),
                enforced=flag not in UNENFORCED_FLAGS,
            )
            for flag in KNOWN_FLAGS
        ]
    )


@router.get("", response_model=FeatureFlagsResponse)
async def list_feature_flags(
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> FeatureFlagsResponse:
    """Lista las feature flags del tenant.

    Args:
        user: Usuario autenticado; solo admin o super_admin.

    Returns:
        Todas las flags conocidas; `value` es `null` en las que el tenant no ha tocado.
    """
    client_id: UUID = user["client_id"]
    return _respuesta(await FeatureFlags().get_all(client_id))


@router.put("/{flag}", response_model=FeatureFlagsResponse)
async def update_feature_flag(
    flag: str,
    data: FeatureFlagUpdate,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> FeatureFlagsResponse:
    """Cambia una feature flag del tenant e invalida su cache.

    Args:
        flag: Una de las flags conocidas.
        data: Nuevo valor.
        user: Usuario autenticado; solo admin o super_admin.

    Returns:
        Todas las flags con el valor ya actualizado.

    Raises:
        AppException: 404 si la flag no existe; 400 si el valor no es admisible
            para esa flag o el tenant no tiene un agente activo donde guardarla.
    """
    client_id: UUID = user["client_id"]
    if flag not in KNOWN_FLAGS:
        raise AppException(
            status_code=404,
            error_code=NOT_FOUND,
            message=f"Flag desconocida: {flag}. Disponibles: {', '.join(KNOWN_FLAGS)}",
        )
    try:
        valor = validar_valor(flag, data.value)
    except ValueError as exc:
        raise AppException(status_code=400, error_code=VALIDATION_ERROR, message=str(exc)) from exc

    servicio = FeatureFlags()
    try:
        await servicio.set_flag(client_id, flag, valor)
    except SinAgenteConfigurableError as exc:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message="El tenant no tiene un agente activo donde guardar la flag",
        ) from exc

    logger.info(
        "Flag %s=%s del tenant %s cambiada por %s", flag, valor, client_id, user.get("user_id")
    )
    return _respuesta(await servicio.get_all(client_id))
