"""Dependencies de FastAPI para inyección de dependencias.

Provee: get_current_user, require_role, get_tenant_session.
Jerarquía de roles: super_admin > admin > supervisor > agent.
"""

from collections.abc import AsyncGenerator, Callable
from typing import Any

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.exceptions import FORBIDDEN, MISSING_TOKEN, MODULE_DISABLED, AppException


async def get_current_user(request: Request) -> dict[str, Any]:
    """Extrae el usuario actual del request.state (inyectado por middleware).

    Args:
        request: Request de FastAPI con state poblado por TenantContextMiddleware.

    Returns:
        Dict con user_id, client_id y role del usuario autenticado.

    Raises:
        AppException: Si no hay usuario autenticado en request.state.
    """
    if not hasattr(request.state, "user_id"):
        raise AppException(
            status_code=401,
            error_code=MISSING_TOKEN,
            message="No autenticado",
        )
    return {
        "user_id": request.state.user_id,
        "client_id": request.state.client_id,
        "role": request.state.user_role,
    }


def require_role(*allowed_roles: str) -> Callable[..., Any]:
    """Factory de dependency que verifica que el usuario tenga un rol permitido.

    Jerarquía de roles:
        - super_admin: acceso total a todos los tenants
        - admin: gestión completa de su tenant
        - supervisor: visualización de métricas y aprobación de respuestas
        - agent: operación básica (ver contactos, responder mensajes)

    Args:
        *allowed_roles: Roles que tienen acceso al endpoint.

    Returns:
        Dependency de FastAPI que valida el rol.

    Raises:
        AppException: Si el rol del usuario no está en allowed_roles (403).
    """

    async def role_checker(
        current_user: dict[str, Any] = Depends(get_current_user),
    ) -> dict[str, Any]:
        """Valida que el rol del usuario actual esté en los roles permitidos."""
        if current_user["role"] not in allowed_roles:
            raise AppException(
                status_code=403,
                error_code=FORBIDDEN,
                message=(
                    f"Rol '{current_user['role']}' no tiene acceso. "
                    f"Roles requeridos: {allowed_roles}"
                ),
            )
        return current_user

    return role_checker


def require_leads_module(*allowed_roles: str) -> Callable[..., Any]:
    """Como `require_role`, y ademas exige que el tenant tenga activo el modulo de leads.

    La bandera es `clients.lead_management_enabled` (ADR-021). Se lee en cada peticion, no
    del token: asi apagarla o encenderla surte efecto al instante, sin esperar a que caduque
    ninguna sesion. Es una consulta por clave primaria dentro de una transaccion corta.

    Args:
        *allowed_roles: Roles con acceso al endpoint.

    Returns:
        Dependency de FastAPI que valida el rol y el modulo.

    Raises:
        AppException: 403 `FORBIDDEN` si el rol no esta permitido; 403 `MODULE_DISABLED` si el
            tenant no tiene el modulo.
    """
    comprobar_rol = require_role(*allowed_roles)

    async def checker(
        current_user: dict[str, Any] = Depends(comprobar_rol),
    ) -> dict[str, Any]:
        """Valida el rol (via `require_role`) y despues el modulo del tenant."""
        client_id = current_user["client_id"]
        async with tenant_session(client_id) as session:
            activo = (
                await session.execute(
                    text("SELECT lead_management_enabled FROM clients WHERE id = :cid"),
                    {"cid": str(client_id)},
                )
            ).scalar_one_or_none()
        if not activo:
            raise AppException(
                status_code=403,
                error_code=MODULE_DISABLED,
                message="El modulo de leads no esta habilitado para este negocio",
            )
        return current_user

    return checker


async def get_tenant_session(
    request: Request,
) -> AsyncGenerator[AsyncSession, None]:
    """Dependency que provee una sesión de DB con SET LOCAL ya aplicado.

    Usar en endpoints que necesitan acceso a datos del tenant.
    El SET LOCAL se ejecuta automáticamente al abrir la sesión.

    Args:
        request: Request de FastAPI con client_id en state.

    Yields:
        AsyncSession con contexto de tenant configurado vía SET LOCAL.
    """
    client_id = request.state.client_id
    async with tenant_session(client_id) as session:
        yield session
