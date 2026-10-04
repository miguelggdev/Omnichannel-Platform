"""Operacion de la plataforma: Celery, Redis y salud del sistema — solo `super_admin`.

GET  /api/v1/platform/celery/workers                 workers que responden al `inspect`
GET  /api/v1/platform/celery/queues                  mensajes pendientes por cola
GET  /api/v1/platform/celery/tasks                   tareas en curso (activas, reservadas, programadas)
POST /api/v1/platform/celery/tasks/{id}/revoke       revoca una tarea
GET  /api/v1/platform/redis                          memoria, clientes y aciertos de Redis
GET  /api/v1/platform/system                         estado de la base, Redis y los workers

Decisiones:

- **Las tareas no devuelven sus argumentos.** Los de `process_incoming_message`, `ai_*` o los
  envios llevan texto de contactos (a veces datos de salud, ADR-072); este panel muestra *que*
  corre, no *con que datos*.
- **`broker_ok=False` no es "cero workers".** Si no se puede hablar con el broker no se sabe
  cuantos hay, y mostrar "0" mandaria a alguien a reiniciar workers que estan bien.
- **Revocar no mata una tarea que ya empezo** (`terminate=False`): solo evita que arranque una
  que espera o esta programada. Terminar a la fuerza un proceso a medias puede dejar una escritura
  a medias; si hace falta, es una decision de operaciones, no un boton.
- El historial de tareas del spec (tabla + signals) no esta: necesita una migracion propia.
"""

import asyncio
import logging
import time
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.dependencies import require_role
from app.schemas.platform import (
    ComponentStatus,
    QueueDepth,
    RedisInfo,
    SystemStatus,
    TaskInfo,
    WorkerInfo,
    WorkersResponse,
)
from app.tasks.celery_config import celery_app

logger = logging.getLogger(__name__)

router = APIRouter()

_ROLES = ("super_admin",)

# Cuanto espera cada `inspect` a que los workers contesten. Se lanzan en paralelo (hilos), asi
# que el peor caso (sin workers) es ~1 s y no la suma.
_INSPECT_TIMEOUT = 1.0
# Cuanto espera la comprobacion previa de conexion con el broker.
_CONNECT_TIMEOUT = 2.0


_METODOS_INSPECT = ("stats", "active", "reserved", "scheduled", "active_queues")


def _comprobar_broker() -> None:
    """Falla si no se puede conectar con el broker (bloqueante: se llama desde un hilo).

    `inspect()` NO lanza si el broker esta caido: devuelve `None`, igual que cuando no hay
    workers. Sin esta comprobacion explicita el panel diria "broker OK, 0 workers" justo cuando
    el problema es el broker. Una conexion que falla lanza `OperationalError`.
    """
    with celery_app.connection_for_read() as conexion:
        conexion.ensure_connection(max_retries=1, interval_start=0, timeout=_CONNECT_TIMEOUT)


def _inspeccionar(metodo: str) -> dict[str, Any] | None:
    """Una de las consultas de `inspect` (bloqueante: se llama desde un hilo).

    Args:
        metodo: Uno de `_METODOS_INSPECT`.

    Returns:
        `{hostname: ...}` o `None` si ningun worker contesto.
    """
    resultado: dict[str, Any] | None = getattr(
        celery_app.control.inspect(timeout=_INSPECT_TIMEOUT), metodo
    )()
    return resultado


async def _preguntar() -> tuple[bool, dict[str, dict[str, Any] | None]]:
    """Pregunta a los workers fuera del event loop; devuelve `(broker_ok, datos)`.

    Las cinco consultas esperan cada una hasta `_INSPECT_TIMEOUT` a que contesten los workers;
    van en paralelo (un hilo cada una) para que sin workers la respuesta tarde ~1 s y no 5.
    """
    try:
        await asyncio.to_thread(_comprobar_broker)
        respuestas = await asyncio.gather(
            *(asyncio.to_thread(_inspeccionar, m) for m in _METODOS_INSPECT)
        )
    except Exception:
        logger.exception("No se pudo consultar a los workers de Celery")
        return False, {}
    claves = ("stats", "active", "reserved", "scheduled", "queues")
    return True, dict(zip(claves, respuestas, strict=True))


def construir_workers(datos: dict[str, dict[str, Any] | None]) -> list[WorkerInfo]:
    """Une la salida de los cinco `inspect` en una fila por worker.

    Args:
        datos: Lo que devuelve `_inspeccionar()`.

    Returns:
        Los workers, ordenados por nombre. Vacia si ninguno contesto.
    """
    stats = datos.get("stats") or {}
    activas = datos.get("active") or {}
    reservadas = datos.get("reserved") or {}
    colas = datos.get("queues") or {}
    workers = []
    for nombre in sorted(stats):
        s = stats[nombre] or {}
        pool = s.get("pool") or {}
        total = s.get("total") or {}
        workers.append(
            WorkerInfo(
                hostname=nombre,
                pid=s.get("pid"),
                concurrency=pool.get("max-concurrency"),
                active_tasks=len(activas.get(nombre) or []),
                reserved_tasks=len(reservadas.get(nombre) or []),
                processed_total=sum(total.values()) if total else None,
                queues=sorted(q.get("name", "") for q in (colas.get(nombre) or [])),
                uptime_seconds=s.get("uptime"),
            )
        )
    return workers


def construir_tareas(datos: dict[str, dict[str, Any] | None]) -> list[TaskInfo]:
    """Normaliza activas, reservadas y programadas, **sin argumentos**.

    Args:
        datos: Lo que devuelve `_inspeccionar()`.

    Returns:
        Las tareas; las activas primero (las que llevan mas tiempo, antes).
    """
    tareas: list[TaskInfo] = []
    for estado in ("active", "reserved", "scheduled"):
        for worker, lista in (datos.get(estado) or {}).items():
            for t in lista or []:
                # Las programadas traen la tarea dentro de `request`.
                t = t.get("request", t)
                tareas.append(
                    TaskInfo(
                        id=str(t.get("id", "")),
                        name=str(t.get("name", "")),
                        worker=worker,
                        state=estado,
                        queue=(t.get("delivery_info") or {}).get("routing_key"),
                        started_at=t.get("time_start"),
                    )
                )
    orden = {"active": 0, "reserved": 1, "scheduled": 2}
    tareas.sort(key=lambda x: (orden[x.state], x.started_at or 0))
    return tareas


@router.get("/celery/workers", response_model=WorkersResponse)
async def celery_workers(user: dict[str, Any] = Depends(require_role(*_ROLES))) -> WorkersResponse:
    """Workers de Celery que contestan ahora mismo.

    Args:
        user: Usuario autenticado; solo super_admin.

    Returns:
        `broker_ok` y los workers; con `broker_ok=False` no se sabe cuantos hay.
    """
    broker_ok, datos = await _preguntar()
    return WorkersResponse(broker_ok=broker_ok, workers=construir_workers(datos))


@router.get("/celery/tasks", response_model=list[TaskInfo])
async def celery_tasks(user: dict[str, Any] = Depends(require_role(*_ROLES))) -> list[TaskInfo]:
    """Tareas en curso: activas, reservadas (a punto de empezar) y programadas.

    Args:
        user: Usuario autenticado; solo super_admin.

    Returns:
        Las tareas sin sus argumentos (pueden llevar datos de contactos).
    """
    _, datos = await _preguntar()
    return construir_tareas(datos)


@router.post("/celery/tasks/{task_id}/revoke")
async def revoke_task(
    task_id: UUID, user: dict[str, Any] = Depends(require_role(*_ROLES))
) -> dict[str, str]:
    """Revoca una tarea: si aun no ha empezado, no se ejecutara.

    No termina una tarea que ya esta corriendo (`terminate=False`).

    Args:
        task_id: Id de la tarea (UUID de Celery).
        user: Usuario autenticado; solo super_admin.

    Returns:
        El id revocado.
    """
    await asyncio.to_thread(celery_app.control.revoke, str(task_id), terminate=False)
    logger.warning("Tarea %s revocada por el super_admin %s", task_id, user.get("user_id"))
    return {"revoked": str(task_id)}


def _nombres_de_colas() -> list[str]:
    """Las colas declaradas en `celery_config`."""
    return [q.name for q in (celery_app.conf.task_queues or [])]


@router.get("/celery/queues", response_model=list[QueueDepth])
async def celery_queues(user: dict[str, Any] = Depends(require_role(*_ROLES))) -> list[QueueDepth]:
    """Mensajes esperando en cada cola (largo de la lista en Redis, el broker).

    Args:
        user: Usuario autenticado; solo super_admin.

    Returns:
        Una entrada por cola declarada, aunque este vacia.
    """
    cliente = aioredis.from_url(str(celery_app.conf.broker_url))
    try:
        pendientes = [(n, int(await cliente.llen(n))) for n in _nombres_de_colas()]
    finally:
        await cliente.close()
    return [QueueDepth(name=n, pending=p) for n, p in pendientes]


@router.get("/redis", response_model=RedisInfo)
async def redis_info(user: dict[str, Any] = Depends(require_role(*_ROLES))) -> RedisInfo:
    """Salud de Redis: memoria, clientes, claves y aciertos de la cache.

    Args:
        user: Usuario autenticado; solo super_admin.

    Returns:
        Las cifras; `hit_ratio` es `None` si todavia no hubo lecturas.
    """
    cliente = aioredis.from_url(get_settings().REDIS_URL)
    try:
        info = await cliente.info()
        claves = int(await cliente.dbsize())
    finally:
        await cliente.close()
    aciertos = int(info.get("keyspace_hits", 0))
    fallos = int(info.get("keyspace_misses", 0))
    return RedisInfo(
        version=str(info.get("redis_version", "")),
        uptime_seconds=int(info.get("uptime_in_seconds", 0)),
        connected_clients=int(info.get("connected_clients", 0)),
        used_memory=int(info.get("used_memory", 0)),
        max_memory=int(info.get("maxmemory", 0)),
        total_keys=claves,
        hit_ratio=round(aciertos / (aciertos + fallos), 4) if aciertos + fallos else None,
    )


async def _medir(nombre: str, comprobar: Any) -> ComponentStatus:
    """Ejecuta una comprobacion y mide cuanto tarda; un fallo no tumba el resto."""
    inicio = time.perf_counter()
    try:
        detalle = await comprobar()
        ok = True
    except Exception as exc:
        logger.warning("Comprobacion de %s fallida: %s", nombre, exc)
        # El detalle al cliente es el tipo de error, no el mensaje (puede llevar hosts o credenciales).
        detalle, ok = type(exc).__name__, False
    return ComponentStatus(
        name=nombre,
        ok=ok,
        latency_ms=round((time.perf_counter() - inicio) * 1000, 1),
        detail=detalle,
    )


async def _comprobar_db() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT 1"))


async def _comprobar_redis() -> None:
    cliente = aioredis.from_url(get_settings().REDIS_URL)
    try:
        await cliente.ping()
    finally:
        await cliente.close()


async def _comprobar_celery() -> str:
    broker_ok, datos = await _preguntar()
    if not broker_ok:
        raise ConnectionError("broker inalcanzable")
    workers = construir_workers(datos)
    if not workers:
        raise RuntimeError("sin workers")
    return f"{len(workers)} worker(s)"


@router.get("/system", response_model=SystemStatus)
async def system_status(user: dict[str, Any] = Depends(require_role(*_ROLES))) -> SystemStatus:
    """Estado de la base de datos, de Redis y de los workers, con su latencia.

    Args:
        user: Usuario autenticado; solo super_admin.

    Returns:
        Version, entorno y un componente por comprobacion. Un componente caido no tumba la
        respuesta: aparece con `ok=false`.
    """
    ajustes = get_settings()
    componentes = await asyncio.gather(
        _medir("database", _comprobar_db),
        _medir("redis", _comprobar_redis),
        _medir("celery", _comprobar_celery),
    )
    return SystemStatus(
        version=ajustes.APP_VERSION,
        environment=ajustes.APP_ENV,
        components=list(componentes),
    )
