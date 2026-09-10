"""Servicio de deduplicacion de webhooks entrantes (PAT-001).

Deduplicacion en dos niveles, tal como describe `specs/sprint-04-webhooks.md`:

1. **Redis** (rapido, O(1), TTL 24h): primera linea de defensa. `SET NX` es atomico,
   asi que dos entregas simultaneas del mismo `external_message_id` no pueden pasar
   ambas — solo la primera obtiene el lock.
2. **PostgreSQL** (permanente, tabla `webhook_dedup`): auditoria y respaldo si Redis
   se reinicia o pierde la clave antes de que expire el TTL.

El flujo es: check Redis -> si es nuevo, encolar -> el worker persiste en `webhook_dedup`.

Nota de resiliencia: si Redis no esta disponible NO se rechaza el webhook. Se deja pasar
y la unicidad la garantiza el `UniqueConstraint("channel", "external_message_id")` de la
tabla `webhook_dedup` en el worker. Perder mensajes es peor que procesar un duplicado.
"""

import logging
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import tenant_session
from app.models.webhook_dedup import WebhookDedup

logger = logging.getLogger(__name__)

# Prefijo y TTL de las claves de deduplicacion en Redis.
DEDUP_KEY_PREFIX = "webhook_dedup"
DEDUP_TTL_SECONDS = 86_400  # 24 horas

# Cliente Redis propio del servicio (el worker de Celery no tiene `app.state`).
# Los stubs tipan Redis como generico; con decode_responses=True es Redis[str].
_redis_client: "aioredis.Redis[str] | None" = None


def build_dedup_key(channel: str, external_message_id: str) -> str:
    """Construye la clave de deduplicacion en Redis.

    Args:
        channel: Canal de origen (whatsapp, instagram, facebook, ...).
        external_message_id: ID del mensaje en el proveedor externo.

    Returns:
        Clave con el patron `webhook_dedup:{channel}:{external_message_id}`.
    """
    return f"{DEDUP_KEY_PREFIX}:{channel}:{external_message_id}"


def get_redis() -> "aioredis.Redis[str]":
    """Devuelve el cliente Redis del servicio, creandolo la primera vez.

    Se cachea a nivel de modulo porque el worker de Celery no tiene acceso a
    `app.state.redis` (ese vive en el ciclo de vida de FastAPI).

    Returns:
        Cliente Redis asincrono apuntando a `settings.REDIS_URL`.
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True)
    return _redis_client


async def close_redis() -> None:
    """Cierra el cliente Redis del servicio, si fue creado."""
    global _redis_client
    if _redis_client is not None:
        # close() y no aclose(): los stubs de types-redis que instala el CI todavia
        # no conocen aclose().
        await _redis_client.close()
        _redis_client = None


async def mark_if_new(
    channel: str,
    external_message_id: str,
    *,
    redis_client: "aioredis.Redis[str] | None" = None,
    ttl_seconds: int = DEDUP_TTL_SECONDS,
) -> bool:
    """Marca el mensaje como visto en Redis y dice si era nuevo.

    Usa `SET key 1 NX EX ttl`, que es atomico: solo la primera llamada obtiene el
    lock, incluso con entregas concurrentes del proveedor.

    Args:
        channel: Canal de origen del mensaje.
        external_message_id: ID del mensaje en el proveedor externo.
        redis_client: Cliente a usar. Por defecto, el del servicio.
        ttl_seconds: Vigencia de la marca. Por defecto 24h.

    Returns:
        True si el mensaje no se habia visto (hay que procesarlo), False si es duplicado.
        Ante un fallo de Redis devuelve True (fail-open) y deja la unicidad a PostgreSQL.
    """
    client = redis_client or get_redis()
    key = build_dedup_key(channel, external_message_id)

    try:
        was_set = await client.set(key, "1", nx=True, ex=ttl_seconds)
    except Exception as exc:  # cualquier fallo de Redis es no fatal
        logger.warning(
            "Redis no disponible para deduplicacion (key=%s): %s. "
            "Se continua; la unicidad la garantiza webhook_dedup en el worker.",
            key,
            exc,
        )
        return True

    return bool(was_set)


async def release_mark(
    channel: str,
    external_message_id: str,
    *,
    redis_client: "aioredis.Redis[str] | None" = None,
) -> None:
    """Borra la marca de Redis para que el proveedor pueda reintentar.

    Se llama cuando el webhook se marco como visto pero NO se logro encolar en Celery.
    Sin esto el mensaje quedaria bloqueado 24h y se perderia definitivamente.

    Args:
        channel: Canal de origen del mensaje.
        external_message_id: ID del mensaje en el proveedor externo.
        redis_client: Cliente a usar. Por defecto, el del servicio.
    """
    client = redis_client or get_redis()
    key = build_dedup_key(channel, external_message_id)

    try:
        await client.delete(key)
    except Exception as exc:  # el TTL acabara limpiando la clave igual
        logger.warning("No se pudo liberar la marca de dedup %s: %s", key, exc)


async def persist_dedup(
    client_id: UUID,
    channel: str,
    external_message_id: str,
    *,
    session: AsyncSession | None = None,
) -> bool:
    """Registra el mensaje en `webhook_dedup` (nivel 2, persistente).

    El `UniqueConstraint("channel", "external_message_id")` de la tabla es la
    garantia real de idempotencia: si Redis perdio la clave, aqui se detecta.

    Args:
        client_id: Tenant propietario del mensaje.
        channel: Canal de origen del mensaje.
        external_message_id: ID del mensaje en el proveedor externo.
        session: Sesion existente con SET LOCAL ya aplicado. El worker la pasa para
            que el registro entre en la MISMA transaccion que el mensaje: o se
            guardan ambos o ninguno. Sin ella se abre una transaccion propia.

    Returns:
        True si se inserto (mensaje nuevo), False si ya existia.
    """
    row = WebhookDedup(
        client_id=client_id,
        channel=channel,
        external_message_id=external_message_id,
    )

    if session is not None:
        # Sin try/except a proposito: un IntegrityError aqui debe abortar la
        # transaccion del worker, no quedar tragado a medio guardar el mensaje.
        session.add(row)
        await session.flush()
        return True

    try:
        async with tenant_session(client_id) as own_session:
            own_session.add(row)
            await own_session.flush()
    except IntegrityError:
        logger.info(
            "Mensaje ya registrado en webhook_dedup: channel=%s external_message_id=%s",
            channel,
            external_message_id,
        )
        return False

    return True


async def is_duplicate_persisted(
    client_id: UUID,
    channel: str,
    external_message_id: str,
    *,
    session: AsyncSession | None = None,
) -> bool:
    """Consulta si el mensaje ya esta en `webhook_dedup`.

    Args:
        client_id: Tenant propietario del mensaje.
        channel: Canal de origen del mensaje.
        external_message_id: ID del mensaje en el proveedor externo.
        session: Sesion existente con SET LOCAL ya aplicado. Sin ella se abre una
            transaccion propia.

    Returns:
        True si ya existe un registro para ese (channel, external_message_id).
    """
    stmt = select(WebhookDedup.id).where(
        WebhookDedup.client_id == client_id,
        WebhookDedup.channel == channel,
        WebhookDedup.external_message_id == external_message_id,
    )

    if session is not None:
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async with tenant_session(client_id) as own_session:
        result = await own_session.execute(stmt)
        return result.scalar_one_or_none() is not None
