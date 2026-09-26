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
from app.api.internal.metrics import router as metrics_router
from app.api.v1.admin import router as admin_router
from app.api.v1.agent_logs import router as agent_logs_router
from app.api.v1.auth import router as auth_router
from app.api.v1.campaigns import router as campaigns_router
from app.api.v1.contacts import router as contacts_router
from app.api.v1.conversations import router as conversations_router
from app.api.v1.csat import router as csat_router
from app.api.v1.documents import router as documents_router
from app.api.v1.notes import router as notes_router
from app.api.v1.quick_replies import router as quick_replies_router
from app.api.v1.tags import contact_tags_router
from app.api.v1.tags import router as tags_router
from app.api.v1.templates import router as templates_router
from app.api.v1.webchat import router as webchat_router
from app.api.v1.webhooks import router as webhooks_router
from app.api.v1.webhooks_config import router as webhooks_config_router
from app.core.config import get_settings
from app.core.database import dispose_db, engine, init_db
from app.core.exceptions import (
    AppException,
    app_exception_handler,
    unhandled_exception_handler,
)
from app.core.logging import setup_logging
from app.core.telemetry import setup_telemetry, shutdown_telemetry
from app.middleware.audit import AuditContextMiddleware
from app.middleware.observability import ObservabilityMiddleware
from app.middleware.tenant_context import TenantContextMiddleware

# Se importa por su efecto: registra `_on_conversation_resolved` como handler
# de `EventEmitter.on(EVENT_CONVERSATION_RESOLVED, ...)` (Sprint 11, Dev B).
# `conversation.resolved` lo emite este proceso (la API, en
# `PUT /conversations/{id}/status`), no el worker de Celery — sin este import
# el CSAT nunca se programa, aunque el modulo si este en `TASK_MODULES`.
from app.tasks import csat_tasks  # noqa: F401

# Loguru sustituye a `logging.basicConfig()` (Sprint 8): `setup_logging()` deja
# un InterceptHandler en la raiz de `logging`, asi que los ~40 modulos que usan
# la stdlib siguen funcionando y sus lineas salen con trace_id y client_id.
setup_logging()
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

    # Tracing: se instrumenta aqui y no en create_app() porque `lifespan` corre
    # una sola vez por proceso, mientras que create_app() lo llama tambien cada
    # test que arma su propia app — instrumentar 40 veces el mismo engine deja
    # 40 listeners sobre la misma conexion.
    setup_telemetry(app=app, engine=engine)

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
    shutdown_telemetry()
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
    # AuditContextMiddleware va ANTES que TenantContextMiddleware en el registro
    # para quedar por dentro de el en la ejecucion: necesita leer el
    # `request.state.user_id` que TenantContextMiddleware acaba de poner.
    app.add_middleware(AuditContextMiddleware)
    app.add_middleware(TenantContextMiddleware)
    # El ultimo registrado es el primero en ejecutarse: ObservabilityMiddleware
    # envuelve tambien a TenantContextMiddleware, de modo que un 401 por JWT
    # invalido tambien queda medido y logueado con su trace_id.
    app.add_middleware(ObservabilityMiddleware)

    # ── Exception handlers ──
    app.add_exception_handler(AppException, app_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # ── Routers ──
    app.include_router(health_router, prefix="/internal", tags=["internal"])
    app.include_router(metrics_router, prefix="/internal", tags=["internal"])
    app.include_router(auth_router, prefix="/api/v1/auth", tags=["auth"])
    # Los webhooks NO pasan por TenantContextMiddleware: se autentican por firma
    # HMAC. El prefijo debe coincidir con WEBHOOK_PATHS_PREFIX del middleware.
    app.include_router(webhooks_router, prefix="/api/v1/webhooks", tags=["webhooks"])
    app.include_router(webchat_router, prefix="/api/v1/webchat", tags=["webchat"])
    app.include_router(documents_router, prefix="/api/v1/documents", tags=["documents"])
    app.include_router(contacts_router, prefix="/api/v1/contacts", tags=["contacts"])
    # Notas y etiquetas de un contacto cuelgan del propio contacto:
    # /api/v1/contacts/{id}/notes y /api/v1/contacts/{id}/tags/{tag_id}.
    app.include_router(notes_router, prefix="/api/v1/contacts", tags=["notes"])
    app.include_router(contact_tags_router, prefix="/api/v1/contacts", tags=["tags"])
    app.include_router(tags_router, prefix="/api/v1/tags", tags=["tags"])
    app.include_router(conversations_router, prefix="/api/v1/conversations", tags=["conversations"])
    app.include_router(csat_router, prefix="/api/v1/csat", tags=["csat"])
    app.include_router(agent_logs_router, prefix="/api/v1/agent-logs", tags=["agent-logs"])
    app.include_router(quick_replies_router, prefix="/api/v1/quick-replies", tags=["quick-replies"])
    app.include_router(admin_router, prefix="/api/v1/admin", tags=["admin"])
    app.include_router(templates_router, prefix="/api/v1/admin/templates", tags=["templates"])
    app.include_router(campaigns_router, prefix="/api/v1/campaigns", tags=["campaigns"])
    # NO "/api/v1/webhooks/outgoing" (el path del spec): TenantContextMiddleware
    # trata cualquier ruta bajo "/api/v1/webhooks/" como webhook entrante (firma
    # HMAC, no JWT) y la salta por completo — un CRUD ahi quedaria sin
    # autenticacion. "outgoing-webhooks" evita la colision (ver ADR-065).
    app.include_router(
        webhooks_config_router, prefix="/api/v1/outgoing-webhooks", tags=["outgoing-webhooks"]
    )

    return app


app = create_app()
