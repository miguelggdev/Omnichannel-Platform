"""Endpoints de autenticación — Login y refresh token.

POST /api/v1/auth/login — email + password → access_token + refresh_token
POST /api/v1/auth/refresh — refresh_token → nuevo access_token

Por qué el login no usa el ORM (BUG-025)
----------------------------------------
Autenticar es la única operación del sistema que necesita mirar `users` **sin**
saber a qué tenant pertenece la fila: el tenant se deduce del usuario, y el
usuario es justo lo que se está buscando. La RLS de `users` y de `clients`
filtra por `app.current_client_id`, que en esa transacción no puede estar
puesto, así que un `select(User)` normal no devuelve nada contra un rol sujeto a
las políticas.

La búsqueda va por `auth_lookup_user()`, una función `SECURITY DEFINER`
(migración 007) que es el único punto con ese acceso y solo sabe resolver un
email exacto. Todo lo que viene después del login —incluida la escritura de
`last_login_at`— ya conoce el tenant y vuelve a pasar por `tenant_session()`,
con la RLS aplicándose con normalidad.
"""

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import text

from app.core.database import AsyncSessionLocal, tenant_session
from app.core.exceptions import FORBIDDEN, INVALID_TOKEN, AppException
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_jwt,
    verify_password,
)
from app.schemas.auth import LoginRequest, RefreshRequest, TokenResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# La función devuelve como mucho una fila: `users.email` es UNIQUE.
_AUTH_LOOKUP = text(
    "SELECT id, client_id, email, password_hash, first_name, last_name, "
    "role, is_active, client_is_active FROM auth_lookup_user(:email)"
)

_LAST_LOGIN = text("UPDATE users SET last_login_at = :ahora WHERE id = :user_id")


@router.post("/login", response_model=TokenResponse)
async def login(credentials: LoginRequest) -> TokenResponse:
    """Autenticar usuario con email y password.

    1. Busca al usuario por email vía `auth_lookup_user()` (ver el docstring del
       módulo: es la única consulta del sistema sin contexto de tenant).
    2. Verifica el password con bcrypt.
    3. Verifica que usuario y tenant estén activos.
    4. Genera access_token y refresh_token.
    5. Actualiza `last_login_at`, ya dentro del contexto del tenant.

    Args:
        credentials: Email y password del usuario.

    Returns:
        TokenResponse con access_token, refresh_token y token_type.

    Raises:
        AppException: 401 si las credenciales son inválidas o si el usuario o su
            organización están desactivados.
    """
    async with AsyncSessionLocal() as session, session.begin():
        user = (await session.execute(_AUTH_LOOKUP, {"email": credentials.email})).one_or_none()

    if user is None:
        raise AppException(
            status_code=401,
            error_code=INVALID_TOKEN,
            message="Credenciales inválidas",
        )

    if not verify_password(credentials.password, user.password_hash):
        raise AppException(
            status_code=401,
            error_code=INVALID_TOKEN,
            message="Credenciales inválidas",
        )

    if not user.is_active:
        raise AppException(
            status_code=401,
            error_code=FORBIDDEN,
            message="Usuario desactivado",
        )

    # El estado del tenant viene en la misma consulta: `clients` también tiene
    # RLS, así que una segunda consulta desde aquí chocaría con lo mismo.
    if not user.client_is_active:
        raise AppException(
            status_code=401,
            error_code=FORBIDDEN,
            message="Organización suspendida",
        )

    token_data = {
        "user_id": str(user.id),
        "client_id": str(user.client_id),
        "email": user.email,
        "role": user.role,
    }

    # A partir de aquí ya se conoce el tenant, así que la escritura vuelve al
    # camino normal: con RLS aplicada y firmada por el propio usuario, que es lo
    # que el trigger de auditoría registrará el día que `users` lleve uno.
    await _registrar_ultimo_acceso(user.client_id, user.id)

    logger.info(
        "Login exitoso: user=%s client=%s role=%s",
        user.id,
        user.client_id,
        user.role,
    )

    return TokenResponse(
        access_token=create_access_token(token_data),
        refresh_token=create_refresh_token(token_data),
    )


async def _registrar_ultimo_acceso(client_id: UUID, user_id: UUID) -> None:
    """Anota la fecha del último acceso del usuario.

    Es contabilidad, no parte de la autenticación: si falla, el login ya es
    válido y negarlo dejaría al usuario fuera por un dato accesorio. Se registra
    el fallo y se sigue.

    Args:
        client_id: Tenant del usuario, ya conocido.
        user_id: Usuario que acaba de entrar.
    """
    try:
        async with tenant_session(client_id, user_id=user_id) as session:
            await session.execute(
                _LAST_LOGIN, {"ahora": datetime.now(timezone.utc), "user_id": user_id}
            )
    except Exception:
        logger.exception("No se pudo actualizar last_login_at de %s", user_id)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(refresh: RefreshRequest) -> TokenResponse:
    """Renovar access_token usando un refresh_token válido.

    1. Decodifica el refresh_token.
    2. Verifica que type == 'refresh'.
    3. Genera nuevo access_token (el refresh_token se mantiene).

    Args:
        refresh: Refresh token JWT.

    Returns:
        TokenResponse con nuevo access_token y el mismo refresh_token.

    Raises:
        AppException: 401 si el refresh_token es inválido o no es tipo refresh.
    """
    payload = decode_jwt(refresh.refresh_token)

    if payload.get("type") != "refresh":
        raise AppException(
            status_code=401,
            error_code=INVALID_TOKEN,
            message="Token no es de tipo refresh",
        )

    # Generar nuevo access_token con los mismos datos
    token_data = {
        "user_id": payload["user_id"],
        "client_id": payload["client_id"],
        "email": payload["email"],
        "role": payload["role"],
    }

    return TokenResponse(
        access_token=create_access_token(token_data),
        refresh_token=refresh.refresh_token,
    )
