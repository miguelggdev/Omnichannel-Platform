"""Limite de peticiones por IP para endpoints publicos (Redis, ventana fija).

Solo lo usa el onboarding: es el unico endpoint que crea datos sin autenticar.
Falla cerrado: si Redis no responde, el endpoint devuelve 503 en vez de quedarse
sin limite, porque un registro sin freno es justo lo que este modulo evita.
"""

import logging
from typing import TYPE_CHECKING, cast

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.services.dedup import get_redis

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from fastapi import Request

logger = logging.getLogger(__name__)

RATE_LIMITED = "RATE_LIMITED"


def client_ip(request: "Request") -> str:
    """IP del cliente, segun los proxies de confianza que hay delante de la API.

    Con `TRUSTED_PROXY_HOPS = n`, la IP real es la n-esima empezando por la derecha de
    `X-Forwarded-For` (lo que anadio nuestro proxy mas externo); lo que el cliente
    escribio a la izquierda no cuenta. Con 0 se usa la direccion del socket.

    Args:
        request: Peticion entrante.

    Returns:
        La IP del cliente, o `unknown` si no se puede determinar.
    """
    hops = get_settings().TRUSTED_PROXY_HOPS
    if hops > 0:
        cabecera = request.headers.get("x-forwarded-for", "")
        partes = [p.strip() for p in cabecera.split(",") if p.strip()]
        if len(partes) >= hops:
            return partes[-hops]
    return request.client.host if request.client else "unknown"


async def hit(key: str, limit: int, window_seconds: int) -> None:
    """Cuenta una peticion y corta si se supera el limite de la ventana.

    Args:
        key: Identificador del recurso y del origen, p. ej. `onboarding:1.2.3.4`.
        limit: Peticiones permitidas por ventana.
        window_seconds: Duracion de la ventana.

    Raises:
        AppException: 429 si se supero el limite; 503 si Redis no responde.
    """
    redis = get_redis()
    clave = f"ratelimit:{key}"
    try:
        cuenta = await cast("Awaitable[int]", redis.incr(clave))
        if cuenta == 1:
            await cast("Awaitable[bool]", redis.expire(clave, window_seconds))
    except Exception as exc:
        logger.exception("Redis no responde al limitar %s", key.split(":", 1)[0])
        raise AppException(
            status_code=503, error_code="SERVICE_UNAVAILABLE", message="Servicio no disponible"
        ) from exc
    if cuenta > limit:
        raise AppException(
            status_code=429,
            error_code=RATE_LIMITED,
            message="Demasiados intentos. Espera un rato e intentalo de nuevo",
        )
