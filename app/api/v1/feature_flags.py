"""Endpoints de feature flags del tenant (Sprint 14, ADR-076).

    GET  /api/v1/admin/feature-flags          lista las flags y su valor
    PUT  /api/v1/admin/feature-flags/{flag}   cambia una (bool, o 0-100 en las que lo admiten)

Solo `admin` y `super_admin`. El tenant sale del token; un `super_admin` puede indicar
otro con `?client_id=`. Las flags de `SUPER_ADMIN_FLAGS` (p. ej. `enable_sandbox`) solo
las cambia un `super_admin`: el `admin` del tenant puede verlas, no activarlas.
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import require_role
from app.core.exceptions import FORBIDDEN, NOT_FOUND, VALIDATION_ERROR, AppException
from app.core.feature_flags import (
    KNOWN_FLAGS,
    SUPER_ADMIN_FLAGS,
    UNENFORCED_FLAGS,
    FeatureFlags,
    SinAgenteConfigurableError,
    validar_valor,
)
from app.schemas.feature_flags import FeatureFlagItem, FeatureFlagsResponse, FeatureFlagUpdate

logger = logging.getLogger(__name__)

router = APIRouter()

_ROLES = ("super_admin", "admin")


def _tenant_objetivo(user: dict[str, Any], client_id: UUID | None) -> UUID:
    """Tenant sobre el que se actua: el del token, o el pedido si es `super_admin`.

    Args:
        user: Usuario autenticado.
        client_id: Tenant pedido por query, si lo hay.

    Returns:
        El tenant a leer o modificar.

    Raises:
        AppException: 403 si un rol distinto de `super_admin` pide otro tenant.
    """
    propio: UUID = user["client_id"]
    if client_id is None or client_id == propio:
        return propio
    if user["role"] != "super_admin":
        raise AppException(
            status_code=403,
            error_code=FORBIDDEN,
            message="Solo un super_admin puede gestionar las flags de otro tenant",
        )
    return client_id


def _respuesta(guardadas: dict[str, bool | int], rol: str) -> FeatureFlagsResponse:
    """Une las flags conocidas con lo que el tenant tiene guardado.

    Args:
        guardadas: Las flags con valor del tenant.
        rol: Rol de quien consulta, para marcar cuales puede cambiar.

    Returns:
        Una entrada por flag de `KNOWN_FLAGS`, con `enforced` segun si alguna feature la lee.
    """
    return FeatureFlagsResponse(
        flags=[
            FeatureFlagItem(
                flag=flag,
                value=guardadas.get(flag),
                enforced=flag not in UNENFORCED_FLAGS,
                editable=rol == "super_admin" or flag not in SUPER_ADMIN_FLAGS,
            )
            for flag in KNOWN_FLAGS
        ]
    )


@router.get("", response_model=FeatureFlagsResponse)
async def list_feature_flags(
    client_id: UUID | None = Query(default=None, description="Solo super_admin"),
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> FeatureFlagsResponse:
    """Lista las feature flags del tenant.

    Args:
        client_id: Otro tenant; solo lo admite un super_admin.
        user: Usuario autenticado; solo admin o super_admin.

    Returns:
        Todas las flags conocidas; `value` es `null` en las que el tenant no ha tocado.
    """
    objetivo = _tenant_objetivo(user, client_id)
    return _respuesta(await FeatureFlags().get_all(objetivo), user["role"])


@router.put("/{flag}", response_model=FeatureFlagsResponse)
async def update_feature_flag(
    flag: str,
    data: FeatureFlagUpdate,
    client_id: UUID | None = Query(default=None, description="Solo super_admin"),
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> FeatureFlagsResponse:
    """Cambia una feature flag del tenant e invalida su cache.

    Args:
        flag: Una de las flags conocidas.
        data: Nuevo valor.
        client_id: Otro tenant; solo lo admite un super_admin.
        user: Usuario autenticado; solo admin o super_admin.

    Returns:
        Todas las flags con el valor ya actualizado.

    Raises:
        AppException: 403 si un admin cambia una flag de `SUPER_ADMIN_FLAGS` o pide otro
            tenant; 404 si la flag no existe; 400 si el valor no es admisible
            para esa flag o el tenant no tiene un agente activo donde guardarla.
    """
    objetivo = _tenant_objetivo(user, client_id)
    if flag not in KNOWN_FLAGS:
        raise AppException(
            status_code=404,
            error_code=NOT_FOUND,
            message=f"Flag desconocida: {flag}. Disponibles: {', '.join(KNOWN_FLAGS)}",
        )
    if flag in SUPER_ADMIN_FLAGS and user["role"] != "super_admin":
        raise AppException(
            status_code=403,
            error_code=FORBIDDEN,
            message=f"La flag {flag} solo la cambia un super_admin",
        )
    try:
        valor = validar_valor(flag, data.value)
    except ValueError as exc:
        raise AppException(status_code=400, error_code=VALIDATION_ERROR, message=str(exc)) from exc

    servicio = FeatureFlags()
    try:
        await servicio.set_flag(objetivo, flag, valor)
    except SinAgenteConfigurableError as exc:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message="El tenant no tiene un agente activo donde guardar la flag",
        ) from exc

    logger.info(
        "Flag %s=%s del tenant %s cambiada por %s", flag, valor, objetivo, user.get("user_id")
    )
    return _respuesta(await servicio.get_all(objetivo), user["role"])
