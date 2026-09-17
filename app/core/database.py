"""Database layer — SQLAlchemy 2.0 async con Supavisor.

El engine se conecta a Supabase Cloud vía el Transaction Pooler (Supavisor,
puerto 6543). El helper tenant_session() aplica el contexto de tenant para RLS
con set_config(..., is_local=true) — el equivalente parametrizable de
SET LOCAL (PostgreSQL no admite bind params en SET).

REGLA CRÍTICA: el contexto SIEMPRE debe quedar con scope de transacción
(is_local=true), NUNCA de sesión. Supavisor resetea variables de sesión entre
transacciones.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

# Usuario autenticado de la petición en curso, para el rastro de auditoría.
# Lo puebla `AuditContextMiddleware` (app/middleware/audit.py) y lo lee
# `tenant_session()` para exponerlo a PostgreSQL como `app.current_user_id`,
# que es de donde lo toma el trigger `audit_trigger_function()`.
#
# Va en un ContextVar y no en un parámetro obligatorio para que las ~30 llamadas
# a `tenant_session(client_id)` que ya existen no tengan que cambiar: cada una
# hereda el usuario de su petición sola. Fuera de una petición (un worker de
# Celery) queda en None, que es justo lo que el rastro debe registrar — una
# acción del sistema no la hizo ninguna persona.
current_user_id: ContextVar[UUID | None] = ContextVar("current_user_id", default=None)

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
async def tenant_session(
    client_id: UUID, user_id: UUID | None = None
) -> AsyncGenerator[AsyncSession, None]:
    """Context manager que abre una sesión con el contexto de tenant para RLS.

    Usa set_config('app.current_client_id', ..., true) — is_local=true le da
    scope de transacción, igual que SET LOCAL, compatible con Supavisor en
    transaction mode. is_local=false (o un SET sin LOCAL) persiste por sesión
    y puede causar fuga de datos entre tenants.

    También publica `app.current_user_id`, que es de donde el trigger de
    auditoría (migración 006) saca el autor del cambio. Se manda siempre, aunque
    sea vacío: si no se definiera nunca, el `current_setting(..., true)` del
    trigger lo vería como NULL igual, pero dejarlo explícito evita que una
    transacción herede por accidente el valor de otra en el mismo backend del
    pooler.

    Args:
        client_id: UUID del tenant para filtrado RLS.
        user_id: Usuario autor de los cambios. Por defecto, el de la petición en
            curso (`current_user_id`); None fuera de una petición, que es lo
            correcto para un worker.

    Yields:
        AsyncSession con el contexto de tenant ya configurado.
    """
    if user_id is None:
        user_id = current_user_id.get()

    async with AsyncSessionLocal() as session, session.begin():
        # SET LOCAL no admite parametros bind (error de sintaxis de PostgreSQL:
        # "SET" no acepta placeholders). set_config() si es una funcion normal
        # y su tercer argumento (is_local=true) da el mismo scope de
        # transaccion que SET LOCAL.
        await session.execute(
            text(
                "SELECT set_config('app.current_client_id', :client_id, true), "
                "set_config('app.current_user_id', :user_id, true)"
            ),
            {"client_id": str(client_id), "user_id": str(user_id) if user_id else ""},
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


def run_isolated(coro: Coroutine[Any, Any, _T]) -> _T:
    """Ejecuta una corrutina en un event loop nuevo y descarta los recursos al terminar.

    BUG-006 (ver MEMORY.md) encontro que un engine async de SQLAlchemy queda
    atado al event loop donde se creo: reusarlo desde otro loop revienta con
    "attached to a different loop". Ahi se arreglo en los fixtures de test
    (engine de scope de funcion / dispose antes de cada test), pero el mismo
    riesgo existe en produccion: los workers de Celery (`bind=True`, funciones
    sincronas) llaman `asyncio.run()` una vez por ejecucion de tarea, y un
    worker prefork procesa muchas tareas secuenciales en el mismo proceso —
    cada `asyncio.run()` abre un loop nuevo, pero `engine` es un singleton de
    modulo cuyo pool de conexiones sobrevive entre llamadas.

    BUG-021 (ver MEMORY.md): el mismo riesgo existia para el cliente Redis de
    `app/services/dedup.py` (tambien un singleton de modulo, con conexiones
    atadas al loop en que se creo) y no se limpiaba en ningun lado — a
    diferencia del engine, que si tenia este `dispose()`. Import perezoso
    (no al tope del modulo): `dedup.py` importa `tenant_session` de aca, y un
    import a nivel de modulo en el otro sentido seria un ciclo.

    Todo el codigo de tareas de Celery (`app/tasks/*.py`) debe llamar a esta
    funcion en vez de `asyncio.run()` directamente.

    Args:
        coro: Corrutina a ejecutar de punta a punta.

    Returns:
        El resultado de la corrutina.
    """

    async def _con_limpieza() -> _T:
        try:
            return await coro
        finally:
            await engine.dispose()
            from app.services.dedup import close_redis

            try:
                await close_redis()
            except Exception:
                # No debe tumbar una tarea que ya termino bien solo porque el
                # cierre del cliente Redis fallo (ej. Redis ya estaba caido).
                logger.exception("No se pudo cerrar el cliente Redis al final de la tarea")

    return asyncio.run(_con_limpieza())
