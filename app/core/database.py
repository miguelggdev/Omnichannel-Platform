"""Database layer — SQLAlchemy 2.0 async con Supavisor.

El engine se conecta a Supabase Cloud vía el Transaction Pooler (Supavisor,
puerto 6543). El helper tenant_session() aplica SET LOCAL para RLS.

REGLA CRÍTICA: SIEMPRE SET LOCAL, NUNCA SET.
Supavisor resetea variables de sesión entre transacciones.
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Motor asíncrono contra Supavisor (Transaction Pooler de Supabase Cloud)
engine = create_async_engine(
    get_settings().DATABASE_URL,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=300,
    echo=get_settings().APP_ENV == "development",
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def tenant_session(client_id: UUID) -> AsyncGenerator[AsyncSession, None]:
    """Context manager que abre una sesión con SET LOCAL para RLS.

    SIEMPRE usar SET LOCAL, NUNCA SET. SET LOCAL tiene scope de transacción,
    compatible con Supavisor en transaction mode. SET sin LOCAL persiste por
    sesión y puede causar fuga de datos entre tenants.

    Args:
        client_id: UUID del tenant para filtrado RLS.

    Yields:
        AsyncSession con el contexto de tenant ya configurado.
    """
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(
            text("SET LOCAL app.current_client_id = :client_id"),
            {"client_id": str(client_id)},
        )
        yield session


async def get_raw_session() -> AsyncGenerator[AsyncSession, None]:
    """Genera una sesión sin contexto de tenant.

    Usar solo para operaciones que no requieren RLS (login, health check).

    Yields:
        AsyncSession sin SET LOCAL aplicado.
    """
    async with AsyncSessionLocal() as session, session.begin():
        yield session


async def init_db() -> None:
    """Verifica la conectividad con la base de datos al iniciar la app."""
    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT 1"))
    logger.info("Conexión a base de datos verificada (Supavisor)")


async def dispose_db() -> None:
    """Cierra el pool de conexiones al apagar la app."""
    await engine.dispose()
    logger.info("Pool de conexiones cerrado")
