"""Sesion del panel: refresh token en cookie `httpOnly`, rotacion y revocacion (ADR-089).

Antes el refresh token viajaba en el cuerpo y el frontend lo guardaba en `localStorage`: un
XSS lo leia y tenia 7 dias de acceso sin forma de anularlo. Ahora:

- Va en una cookie `httpOnly` (JavaScript no la lee), `Secure`, `SameSite=Strict` (no viaja
  en peticiones de otros sitios) y con `Path=/api/v1/auth` (solo la reciben login, refresh y
  logout, no el resto de la API).
- Es de un solo uso: cada `/auth/refresh` la rota. Si llega un refresh ya usado fuera de un
  margen corto (dos pestanas refrescando a la vez), es indicio de robo: se suben las versiones
  de sesion del usuario (`users.token_version`) y se anulan todas.
- `users.token_version` va en el token (`ver`): subirla revoca todas las sesiones del usuario
  (logout en todos los dispositivos, cambio de contrasena o de rol, desactivacion).
"""

import logging
import time
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from sqlalchemy import text

from app.core.config import get_settings
from app.services.dedup import get_redis

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from fastapi import Response
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

REFRESH_COOKIE = "refresh_token"
COOKIE_PATH = "/api/v1/auth"

Uso = Literal["primero", "margen", "reutilizado"]

_SUBIR_VERSION = text(
    "UPDATE users SET token_version = token_version + 1 "
    "WHERE id = :user_id AND client_id = :client_id"
)


def poner_cookie(response: "Response", refresh_token: str) -> None:
    """Pone el refresh token en su cookie.

    Args:
        response: Respuesta a la que se anade `Set-Cookie`.
        refresh_token: Token a guardar.
    """
    ajustes = get_settings()
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=ajustes.JWT_REFRESH_EXPIRATION_DAYS * 24 * 3600,
        path=COOKIE_PATH,
        httponly=True,
        secure=ajustes.AUTH_COOKIE_SECURE,
        samesite="strict",
    )


def borrar_cookie(response: "Response") -> None:
    """Borra la cookie del refresh token (mismos atributos con los que se puso).

    Args:
        response: Respuesta a la que se anade el `Set-Cookie` de borrado.
    """
    response.delete_cookie(
        REFRESH_COOKIE,
        path=COOKIE_PATH,
        httponly=True,
        secure=get_settings().AUTH_COOKIE_SECURE,
        samesite="strict",
    )


async def registrar_uso(jti: str, expira: int) -> Uso:
    """Anota que un refresh token se ha usado y dice si ya lo estaba.

    Args:
        jti: Identificador del token.
        expira: Su `exp` (epoch): la marca vive hasta entonces, ni un segundo mas.

    Returns:
        `primero` si es su primer uso; `margen` si ya se uso hace menos de
        `REFRESH_REUSE_GRACE_SECONDS` (dos pestanas a la vez); `reutilizado` si no. Si Redis no
        responde se trata como `primero`: la version de sesion sigue protegiendo, y dejar a
        todos sin poder refrescar por una caida de Redis es peor.
    """
    ahora = int(time.time())
    clave = f"refresh-usado:{jti}"
    try:
        redis = get_redis()
        nuevo = await cast(
            "Awaitable[bool | None]",
            redis.set(clave, ahora, ex=max(1, expira - ahora), nx=True),
        )
        if nuevo:
            return "primero"
        primero = await cast("Awaitable[str | None]", redis.get(clave))
    except Exception:
        logger.exception("Redis no responde al rotar un refresh token")
        return "primero"
    margen = get_settings().REFRESH_REUSE_GRACE_SECONDS
    if primero is not None and primero.isdigit() and ahora - int(primero) <= margen:
        return "margen"
    return "reutilizado"


async def revocar_sesiones(session: "AsyncSession", client_id: UUID, user_id: UUID) -> None:
    """Sube `users.token_version`: todos los refresh tokens del usuario dejan de valer.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant del usuario.
        user_id: Usuario.
    """
    await session.execute(_SUBIR_VERSION, {"user_id": user_id, "client_id": client_id})
