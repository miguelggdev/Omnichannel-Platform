"""App factory — FastAPI con lifespan, middleware y routers.

Punto de entrada de la aplicación. Patrón factory para crear instancias
aisladas (producción y testing).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.internal.health import router as health_router
from app.api.v1.auth import router as auth_router
from app.api.v1.webhooks import router as webhooks_router
from app.core.config import get_settings
from app.core.database import dispose_db, init_db
from app.core.exceptions import (
    AppException,
    app_exception_handler,
    unhandled_exception_handler,
)
from app.middleware.tenant_context import TenantContextMiddleware

logging.basicConfig(
    level=getattr(logging, get_settings().LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
if TYPE_CHECKING:
    from collections.abc import Awaitable

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Inicialización y limpieza de recursos.

    Startup: verifica conexión a DB y Redis.
    Shutdown: cierra pool de DB y conexión Redis.

    Args:
        app: Instancia de FastAPI.

    Yields:
        Control al framework mientras la app está activa.
    """
    # ── Startup ──
    logger.info("Iniciando aplicación — env=%s", get_settings().APP_ENV)

    # Verificar DB
    await init_db()

    # Verificar Redis
    redis_client = aioredis.from_url(get_settings().REDIS_URL)
    try:
        # redis-py tipa ping() como `Awaitable[bool] | bool`; con el cliente
        # asincrono siempre es awaitable.
        await cast("Awaitable[bool]", redis_client.ping())
        logger.info("Conexión a Redis verificada")
    except Exception as exc:
        logger.warning("Redis no disponible al inicio: %s", exc)
    app.state.redis = redis_client

    logger.info("Aplicación lista")

    yield

    # ── Shutdown ──
    logger.info("Cerrando aplicación...")
    await redis_client.close()
    await dispose_db()
    logger.info("Aplicación cerrada")


def create_app() -> FastAPI:
    """Crea y configura la instancia de FastAPI.

    Registra middleware, exception handlers y routers.
    El patrón factory permite crear instancias aisladas para testing.

    Returns:
        Instancia configurada de FastAPI.
    """
    app = FastAPI(
        title="Omnichannel Conversational AI",
        description="Plataforma SaaS omnicanal multi-tenant de IA conversacional",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    # ── Middleware stack ──
    # Orden importa: último registrado = primero ejecutado
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(TenantContextMiddleware)

    # ── Exception handlers ──
    app.add_exception_handler(AppException, app_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # ── Routers ──
    app.include_router(health_router, prefix="/internal", tags=["internal"])
    app.include_router(auth_router, prefix="/api/v1/auth", tags=["auth"])
    # Los webhooks NO pasan por TenantContextMiddleware: se autentican por firma
    # HMAC. El prefijo debe coincidir con WEBHOOK_PATHS_PREFIX del middleware.
    app.include_router(webhooks_router, prefix="/api/v1/webhooks", tags=["webhooks"])

    return app


app = create_app()
