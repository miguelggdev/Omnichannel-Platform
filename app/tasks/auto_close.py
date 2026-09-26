"""Auto-cierre periodico de conversaciones inactivas (Celery Beat).

Dos reglas, las de `specs/sprint-07-scheduling-crm.md` §14:

    waiting_client sin movimiento > 24 h  ->  resolved
    resolved       desde       > 7 dias   ->  archived

Ambas son transiciones validas de `ConversationLifecycle`; el test
`test_auto_close.py::test_las_dos_reglas_son_transiciones_validas` lo comprueba
para que la maquina de estados y esta tarea no se separen con el tiempo.

Nombre de la tarea
------------------
`app.tasks.bulk_auto_close_conversations`, no el `app.tasks.auto_close_conversations`
de la spec: `celery_config.py` (Sprint 2) ya declara ese nombre exacto en
`beat_schedule`, y el routing automatico manda a la cola `bulk` lo que empieza por
`app.tasks.bulk_*`. Con el nombre de la spec la tarea no habria tenido ni entrada
en Beat ni cola.

RLS y alcance multi-tenant
--------------------------
La tarea es cross-tenant por naturaleza, pero el rol de la aplicacion esta sujeto
a RLS (`FORCE ROW LEVEL SECURITY` en las 18 tablas, y en CI se corre con
`app_user`, `NOBYPASSRLS`). La spec propone crear un rol de servicio con
BYPASSRLS; eso es una decision de infraestructura que toca migracion y
`docker-compose.yml`, y no esta tomada.

Se itera tenant por tenant con `tenant_session()`, que deja la RLS activa y
genuina. La lista de tenants sale de `public.list_active_client_ids()`
(migracion 015, BUG-045), una funcion `SECURITY DEFINER` que solo devuelve los
UUID de los tenants activos — mismo patron que `auth_lookup_user()` del login.
Antes se leia `clients` directamente, la RLS lo bloqueaba y se caia siempre a
`DEFAULT_CLIENT_ID`: las campanas programadas y el auto-cierre de cualquier
otro tenant no corrian nunca. Esa caida se conserva solo como red para una base
sin la migracion 015.
"""

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Interval, func, literal, text
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, run_isolated, tenant_session
from app.core.metrics import record_conversation_resolved
from app.models.conversation import Conversation
from app.tasks.celery_config import celery_app

logger = logging.getLogger(__name__)

# Horas que una conversacion puede quedarse esperando al contacto antes de darse
# por resuelta.
WAITING_CLIENT_HOURS = 24

# Dias que una conversacion resuelta permanece visible antes de archivarse.
RESOLVED_DAYS = 7


@celery_app.task(
    name="app.tasks.bulk_auto_close_conversations",
    queue="bulk",
    acks_late=True,
    time_limit=300,
    soft_time_limit=270,
)
def auto_close_conversations() -> dict[str, Any]:
    """Cierra y archiva conversaciones inactivas de todos los tenants alcanzables.

    Corre cada 15 minutos por Celery Beat. No reintenta: si falla, la siguiente
    pasada vuelve a encontrar las mismas conversaciones (las condiciones son por
    antiguedad, no por una marca que se consuma), asi que un reintento inmediato
    no aporta nada.

    Returns:
        Conteo de conversaciones resueltas y archivadas, y cuantos tenants se
        recorrieron.
    """
    return run_isolated(_auto_close())


async def _auto_close() -> dict[str, Any]:
    """Recorre los tenants aplicando las dos reglas de cierre.

    Un fallo en un tenant no corta el recorrido: se registra y se sigue con el
    siguiente, para que un tenant con datos raros no deje sin barrer a los demas.

    Returns:
        Conteo de conversaciones resueltas y archivadas, y tenants recorridos.
    """
    client_ids = await _load_active_client_ids()

    resueltas = 0
    archivadas = 0
    tenants_con_error = 0

    for client_id in client_ids:
        try:
            async with tenant_session(client_id) as session:
                resueltas += await _resolve_stale_waiting_client(session, client_id)
                archivadas += await _archive_old_resolved(session, client_id)
        except Exception:
            tenants_con_error += 1
            logger.exception("Auto-cierre fallido para el tenant %s", client_id)

    if resueltas or archivadas:
        logger.info(
            "Auto-cierre: %s conversaciones a resolved (waiting_client > %sh), "
            "%s a archived (resolved > %sd), sobre %s tenant(s)",
            resueltas,
            WAITING_CLIENT_HOURS,
            archivadas,
            RESOLVED_DAYS,
            len(client_ids),
        )

    return {
        "resolved": resueltas,
        "archived": archivadas,
        "tenants": len(client_ids),
        "tenants_failed": tenants_con_error,
    }


async def _load_active_client_ids() -> list[UUID]:
    """Devuelve los tenants activos a barrer.

    Los lee con `public.list_active_client_ids()` (migracion 015), la unica via
    sin contexto de tenant hacia `clients`: la politica de RLS de esa tabla
    filtra por `app.current_client_id`, asi que un `SELECT` directo falla. Si la
    funcion no existe (base sin la 015) se cae a `DEFAULT_CLIENT_ID` con un
    ERROR en el log, porque entonces los demas tenants quedan sin barrer.

    Returns:
        Lista de UUIDs de tenants; vacia si no hay ninguno alcanzable.
    """
    try:
        async with AsyncSessionLocal() as session:
            filas = (
                await session.execute(text("SELECT * FROM public.list_active_client_ids()"))
            ).all()
        client_ids = [
            fila[0] if isinstance(fila[0], UUID) else UUID(str(fila[0])) for fila in filas
        ]
        if client_ids:
            return client_ids
        logger.warning("No hay tenants activos en `clients`; no hay nada que barrer")
        return []
    except Exception as exc:
        logger.error(
            "No se pudo listar los tenants con list_active_client_ids() (falta la "
            "migracion 015?): %s. Se usa solo DEFAULT_CLIENT_ID; el resto de los "
            "tenants queda sin barrer",
            exc,
        )

    raw = get_settings().DEFAULT_CLIENT_ID
    if not raw:
        logger.error(
            "Auto-cierre sin tenants: `clients` no es legible y DEFAULT_CLIENT_ID esta vacio"
        )
        return []
    try:
        return [UUID(raw)]
    except ValueError:
        logger.error("DEFAULT_CLIENT_ID no es un UUID valido: %s", raw)
        return []


async def _resolve_stale_waiting_client(session: AsyncSession, client_id: UUID) -> int:
    """Pasa a `resolved` lo que lleva demasiado esperando al contacto.

    El corte se calcula con `now()` de PostgreSQL, no con la hora del proceso de
    Python: `updated_at` lo escribe el servidor (`onupdate=func.now()`) y comparar
    contra otro reloj haria que el umbral se corriera con cualquier desfase.

    Args:
        session: Sesion con el contexto de tenant ya aplicado.
        client_id: Tenant a barrer.

    Returns:
        Cuantas conversaciones cambiaron de estado.
    """
    corte = func.now() - literal(timedelta(hours=WAITING_CLIENT_HOURS), Interval)

    # `resultado` se anota como Any a proposito: `AsyncSession.execute()` esta
    # tipado como `Result[Any]`, y segun la version de SQLAlchemy ese tipo expone
    # `rowcount` (un DML siempre devuelve un `CursorResult`) o no. Con `cast` a
    # `CursorResult` mypy falla en una version por atributo inexistente y en la
    # otra por cast redundante; `Any` es lo unico que ambas aceptan.
    resultado: Any = await session.execute(
        sa_update(Conversation)
        .where(
            Conversation.client_id == client_id,
            Conversation.status == "waiting_client",
            Conversation.updated_at < corte,
        )
        .values(status="resolved", resolved_at=func.now(), updated_at=func.now())
    )
    cerradas = int(resultado.rowcount or 0)
    # El auto-cierre no pasa por `ConversationLifecycle.transition()` (es un
    # UPDATE masivo), asi que registra su propia metrica. Va con
    # `resolved_by="auto_close"` y no como resolucion del agente: una
    # conversacion que se cierra sola por inactividad no es una atendida.
    record_conversation_resolved(str(client_id), "auto_close", cerradas)
    return cerradas


async def _archive_old_resolved(session: AsyncSession, client_id: UUID) -> int:
    """Archiva las conversaciones resueltas hace mas de `RESOLVED_DAYS` dias.

    Se exige `resolved_at IS NOT NULL`: una conversacion en `resolved` sin sello
    de resolucion es un dato inconsistente y archivarla a ciegas esconderia el
    problema en vez de mostrarlo.

    Args:
        session: Sesion con el contexto de tenant ya aplicado.
        client_id: Tenant a barrer.

    Returns:
        Cuantas conversaciones se archivaron.
    """
    corte = func.now() - literal(timedelta(days=RESOLVED_DAYS), Interval)

    resultado: Any = await session.execute(  # Any: ver _resolve_stale_waiting_client
        sa_update(Conversation)
        .where(
            Conversation.client_id == client_id,
            Conversation.status == "resolved",
            Conversation.resolved_at.is_not(None),
            Conversation.resolved_at < corte,
        )
        .values(status="archived", updated_at=func.now())
    )
    return int(resultado.rowcount or 0)
