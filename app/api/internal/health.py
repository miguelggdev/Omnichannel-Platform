"""Health check endpoint — Sin autenticación.

Verifica conectividad con DB (Supavisor) y Redis.
Retorna 200 si todo ok, 503 si algo falla.
"""

import logging

import redis.asyncio as aioredis
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health_check() -> JSONResponse:
    """Health check para Docker y load balancers.

    Verifica conectividad con la base de datos (vía Supavisor) y Redis.
    No requiere autenticación.

    Returns:
        JSONResponse con status 200 (ok) o 503 (degraded).
    """
    checks: dict[str, str] = {}

    # Check DB vía Supavisor (Supabase Cloud)
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        logger.warning("Health check DB failed: %s", exc)
        checks["database"] = "error"

    # Check Redis
    try:
        redis_client = aioredis.from_url(get_settings().REDIS_URL)
        await redis_client.ping()
        await redis_client.close()
        checks["redis"] = "ok"
    except Exception as exc:
        logger.warning("Health check Redis failed: %s", exc)
        checks["redis"] = "error"

    all_ok = all(v == "ok" for v in checks.values())
    status = "ok" if all_ok else "degraded"
    status_code = 200 if all_ok else 503

    return JSONResponse(
        status_code=status_code,
        content={"status": status, "checks": checks},
    )
