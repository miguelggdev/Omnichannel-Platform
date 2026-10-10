"""Limites de frecuencia en Redis con ventana fija.

- `hit()`: para endpoints publicos (onboarding, captura de leads, login). Falla cerrado: si
  Redis no responde devuelve 503 en vez de quedarse sin limite.
- `dentro_del_limite()`: para el procesamiento interno (p. ej. mensajes que llegan a la IA
  por contacto). No lanza; falla abierto, porque perder mensajes de clientes por una caida de
  Redis es peor que no limitar unos minutos.

El contador se crea con su TTL y se incrementa en una sola transaccion (`SET NX EX` + `INCR`):
con `INCR` y despues `EXPIRE`, un fallo entre los dos dejaba la clave sin caducidad y el
origen bloqueado para siempre.
"""

import logging
from typing import TYPE_CHECKING

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.services.dedup import get_redis

if TYPE_CHECKING:
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


async def _contar(key: str, window_seconds: int) -> int:
    """Suma uno al contador de la ventana y devuelve el total, de forma atomica.

    Args:
        key: Identificador del recurso y del origen.
        window_seconds: Duracion de la ventana.

    Returns:
        Las peticiones contadas en la ventana actual, esta incluida.
    """
    clave = f"ratelimit:{key}"
    async with get_redis().pipeline(transaction=True) as pipe:
        pipe.set(clave, 0, ex=window_seconds, nx=True)
        pipe.incr(clave)
        _, cuenta = await pipe.execute()
    return int(cuenta)


async def dentro_del_limite(key: str, limit: int, window_seconds: int) -> bool:
    """Cuenta un evento y dice si sigue dentro del limite. Falla abierto.

    Args:
        key: Identificador del recurso y del origen, p. ej. `ia-contacto:<client>:<contact>`.
        limit: Eventos permitidos por ventana.
        window_seconds: Duracion de la ventana.

    Returns:
        `False` si se supero el limite; `True` si no, o si Redis no responde.
    """
    try:
        return await _contar(key, window_seconds) <= limit
    except Exception:
        logger.exception("Redis no responde al limitar %s", key.split(":", 1)[0])
        return True


async def hit(key: str, limit: int, window_seconds: int) -> None:
    """Cuenta una peticion y corta si se supera el limite de la ventana.

    Args:
        key: Identificador del recurso y del origen, p. ej. `onboarding:1.2.3.4`.
        limit: Peticiones permitidas por ventana.
        window_seconds: Duracion de la ventana.

    Raises:
        AppException: 429 si se supero el limite; 503 si Redis no responde.
    """
    try:
        cuenta = await _contar(key, window_seconds)
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
