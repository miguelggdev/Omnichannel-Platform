"""Onboarding publico: auto-registro de un tenant (Sprint 15, Fase 2).

    POST /api/v1/onboarding/register       crea tenant + administrador y devuelve su sesion
    POST /api/v1/onboarding/verify-email   confirma el email con el token del enlace
    GET  /api/v1/onboarding/verification   si el email del usuario autenticado esta verificado
    POST /api/v1/onboarding/resend-verification   reenvia el enlace al usuario autenticado

Ambos son publicos (sin JWT). Para que no sean una puerta abierta:
- El registro esta apagado salvo `ONBOARDING_ENABLED=true` (responde 404, como si no existiera).
  La verificacion y el reenvio **no**: un enlace ya enviado debe seguir funcionando aunque el
  registro se cierre despues, y solo aceptan un token firmado o un usuario autenticado.
- El reenvio se limita por usuario (3 por hora): sin eso seria un grifo de correo.
- El registro se limita por IP (`ONBOARDING_MAX_PER_IP_PER_HOUR`).
- La verificacion de email no bloquea el uso de la cuenta: es informativa por ahora.
"""

import asyncio
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import (
    CONFLICT,
    NOT_FOUND,
    VALIDATION_ERROR,
    AppException,
    JWTExpiredError,
    JWTInvalidError,
)
from app.core.rate_limit import client_ip, hit
from app.core.security import decode_jwt
from app.models.user import User
from app.schemas.onboarding import (
    OnboardingRequest,
    OnboardingResponse,
    ResendVerificationResponse,
    VerificationStatus,
    VerifyEmailRequest,
    VerifyEmailResponse,
)
from app.services.onboarding import mark_email_verified, register_new_client

logger = logging.getLogger(__name__)

router = APIRouter()

_ENQUEUE_TIMEOUT_SECONDS = 5.0


def _exigir_habilitado() -> None:
    """Corta con 404 si el onboarding esta apagado.

    Raises:
        AppException: 404 si `ONBOARDING_ENABLED` es falso.
    """
    if not get_settings().ONBOARDING_ENABLED:
        raise AppException(status_code=404, error_code=NOT_FOUND, message="No encontrado")


async def _encolar_verificacion(client_id: UUID, user_id: UUID, email: str) -> None:
    """Encola el email de verificacion sin que un fallo tumbe el registro.

    Args:
        client_id: Tenant nuevo.
        user_id: Administrador nuevo.
        email: Destinatario.
    """
    from app.tasks.onboarding_tasks import send_verification_email

    try:
        # `apply_async` habla con Redis de forma sincrona: en un hilo y con tope de tiempo.
        await asyncio.wait_for(
            asyncio.to_thread(
                send_verification_email.apply_async,
                args=(str(client_id), str(user_id), email),
            ),
            timeout=_ENQUEUE_TIMEOUT_SECONDS,
        )
    except Exception:
        # La cuenta ya existe y funciona; el usuario podra pedir otro enlace mas adelante.
        logger.exception("No se pudo encolar la verificacion de %s", user_id)


@router.post("/register", response_model=OnboardingResponse, status_code=201)
async def register(request: Request, payload: OnboardingRequest) -> OnboardingResponse:
    """Registra un negocio nuevo y a su administrador.

    Args:
        request: Peticion, para limitar por IP.
        payload: Datos del formulario.

    Returns:
        Ids del tenant y del usuario, y la sesion lista para usar.

    Raises:
        AppException: 404 si esta apagado; 429 si la IP supero el limite; 409 si el email
            ya existe.
    """
    _exigir_habilitado()
    await hit(
        f"onboarding:{client_ip(request)}", get_settings().ONBOARDING_MAX_PER_IP_PER_HOUR, 3600
    )
    respuesta = await register_new_client(payload)
    await _encolar_verificacion(respuesta.client_id, respuesta.user_id, payload.admin_email.lower())
    return respuesta


@router.post("/verify-email", response_model=VerifyEmailResponse)
async def verify_email(payload: VerifyEmailRequest) -> VerifyEmailResponse:
    """Confirma el email de un usuario con el token que recibio por correo.

    Args:
        payload: Token del enlace.

    Returns:
        `verified=True` si quedo confirmado.

    Raises:
        AppException: 400 si el token es invalido, caduco, no es de verificacion, o el
            usuario ya no existe o cambio de email.
    """
    invalido = AppException(
        status_code=400,
        error_code=VALIDATION_ERROR,
        message="Enlace de verificación inválido o caducado",
    )
    try:
        datos: dict[str, Any] = decode_jwt(payload.token)
    except (JWTExpiredError, JWTInvalidError) as exc:
        raise invalido from exc
    if datos.get("type") != "email_verification":
        raise invalido
    try:
        client_id = UUID(datos["client_id"])
        user_id = UUID(datos["user_id"])
        email = str(datos["email"])
    except (KeyError, ValueError, TypeError) as exc:
        raise invalido from exc
    if not await mark_email_verified(client_id, user_id, email):
        raise invalido
    return VerifyEmailResponse(verified=True)


_TODOS_LOS_ROLES = ("super_admin", "admin", "supervisor", "agent", "medical")
_REENVIOS_POR_HORA = 3


async def _leer_usuario(user: dict[str, Any]) -> tuple[str, bool | None]:
    """Email actual del usuario del token y su estado de verificacion.

    Args:
        user: Usuario autenticado.

    Returns:
        `(email, verificado)`. `verificado` es `None` si la cuenta no pasa por el registro
        publico (la creo un administrador o una plantilla): no hay nada que verificar.

    Raises:
        AppException: 404 si el usuario del token ya no existe.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        fila = (
            await session.execute(
                select(User.email, User.settings).where(
                    User.id == UUID(str(user["user_id"])), User.client_id == client_id
                )
            )
        ).first()
    if fila is None:
        raise AppException(status_code=404, error_code=NOT_FOUND, message="Usuario no encontrado")
    verificado = fila.settings.get("email_verified")
    return fila.email, verificado if isinstance(verificado, bool) else None


@router.get("/verification", response_model=VerificationStatus)
async def verification_status(
    user: dict[str, Any] = Depends(require_role(*_TODOS_LOS_ROLES)),
) -> VerificationStatus:
    """Si el email del usuario autenticado esta verificado.

    Args:
        user: Usuario autenticado.

    Returns:
        `verified` es `true`/`false` para quien se registro solo y `null` para el resto.
    """
    email, verificado = await _leer_usuario(user)
    return VerificationStatus(email=email, verified=verificado)


@router.post("/resend-verification", response_model=ResendVerificationResponse)
async def resend_verification(
    user: dict[str, Any] = Depends(require_role(*_TODOS_LOS_ROLES)),
) -> ResendVerificationResponse:
    """Reenvia el enlace de verificacion al email actual del usuario autenticado.

    Args:
        user: Usuario autenticado.

    Returns:
        `sent=true` cuando se encolo el envio.

    Raises:
        AppException: 409 si el email ya esta verificado o la cuenta no lo requiere; 429 si
            ya pidio demasiados reenvios esta hora.
    """
    email, verificado = await _leer_usuario(user)
    if verificado is not False:
        raise AppException(
            status_code=409,
            error_code=CONFLICT,
            message="El correo ya está verificado o no requiere verificación",
        )
    await hit(f"verify-resend:{user['user_id']}", _REENVIOS_POR_HORA, 3600)
    await _encolar_verificacion(user["client_id"], UUID(str(user["user_id"])), email)
    return ResendVerificationResponse(sent=True)
