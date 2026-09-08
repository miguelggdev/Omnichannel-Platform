"""Endpoints de autenticación — Login y refresh token.

POST /api/v1/auth/login — email + password → access_token + refresh_token
POST /api/v1/auth/refresh — refresh_token → nuevo access_token
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.core.exceptions import AppException, INVALID_TOKEN
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_jwt,
    verify_password,
)
from app.models.client import Client
from app.models.user import User
from app.schemas.auth import LoginRequest, RefreshRequest, TokenResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/login", response_model=TokenResponse)
async def login(credentials: LoginRequest) -> TokenResponse:
    """Autenticar usuario con email y password.

    1. Busca usuario por email (sin RLS, consulta directa).
    2. Verifica password con bcrypt.
    3. Verifica que usuario y tenant estén activos.
    4. Genera access_token y refresh_token.
    5. Actualiza last_login_at.

    Args:
        credentials: Email y password del usuario.

    Returns:
        TokenResponse con access_token, refresh_token y token_type.

    Raises:
        AppException: 401 si credenciales inválidas, usuario/tenant inactivo.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Buscar usuario por email
            result = await session.execute(
                select(User).where(User.email == credentials.email)
            )
            user = result.scalar_one_or_none()

            if not user:
                raise AppException(
                    status_code=401,
                    error_code=INVALID_TOKEN,
                    message="Credenciales inválidas",
                )

            # Verificar password
            if not verify_password(credentials.password, user.password_hash):
                raise AppException(
                    status_code=401,
                    error_code=INVALID_TOKEN,
                    message="Credenciales inválidas",
                )

            # Verificar que el usuario esté activo
            if not user.is_active:
                raise AppException(
                    status_code=401,
                    error_code="FORBIDDEN",
                    message="Usuario desactivado",
                )

            # Verificar que el tenant esté activo
            client_result = await session.execute(
                select(Client).where(Client.id == user.client_id)
            )
            client = client_result.scalar_one_or_none()

            if not client or not client.is_active:
                raise AppException(
                    status_code=401,
                    error_code="FORBIDDEN",
                    message="Organización suspendida",
                )

            # Generar payload JWT
            token_data = {
                "user_id": str(user.id),
                "client_id": str(user.client_id),
                "email": user.email,
                "role": user.role,
            }

            # Actualizar last_login_at
            user.last_login_at = datetime.now(timezone.utc)

            logger.info(
                "Login exitoso: user=%s client=%s role=%s",
                user.id, user.client_id, user.role,
            )

            return TokenResponse(
                access_token=create_access_token(token_data),
                refresh_token=create_refresh_token(token_data),
            )


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
