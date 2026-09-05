# Sprint 8 — Addendum: Operaciones, Observabilidad y Hardening

> **Proyecto:** Plataforma SaaS Multi-Tenant de IA Conversacional  
> **Sprint:** 8 (Observabilidad, Backup & Hardening)  
> **Tipo:** Addendum de especificaciones técnicas  
> **Fecha:** 2026-09-05  
> **Stack:** FastAPI (async) · Supabase self-hosted (PostgreSQL + pgvector + pgBouncer) · Redis · Celery · LangGraph · Docker Compose · Traefik v3 · Prometheus + Grafana

---

## Convenciones generales

| Regla | Detalle |
|---|---|
| Multi-tenant | Toda tabla lleva `client_id UUID` con RLS habilitado, excepto tablas de plataforma (`platform_settings`, `system_logs`) |
| pgBouncer | Usar `SET LOCAL` en cada transacción para variables de sesión RLS |
| Type hints | Obligatorios en todos los parámetros y retornos |
| Docstrings | Estilo Google en todas las funciones públicas |
| Async | Todo endpoint y servicio usa `async/await`; sesiones SQLAlchemy 2.0 async |
| RBAC | Jerarquía: `super_admin` > `admin` > `supervisor` > `agent` |

---

## Tabla de contenidos

1. [Feature 1: Panel de Administración Celery/Redis](#feature-1-panel-de-administración-celeryredis)
2. [Feature 2: Bot de Telegram para Monitoreo (Super Admin)](#feature-2-bot-de-telegram-para-monitoreo-super-admin)
3. [Feature 3: Backup Avanzado y Replicación](#feature-3-backup-avanzado-y-replicación)
4. [Feature 4: Políticas de Seguridad](#feature-4-políticas-de-seguridad)

---

## Feature 1: Panel de Administración Celery/Redis

### 1.1 Descripción

Panel de administración exclusivo para `super_admin` que expone endpoints REST para monitorear y gestionar workers de Celery, colas de tareas y el estado de Redis. Incluye dashboard de Grafana y métricas de Prometheus.

### 1.2 Endpoints

| Método | Ruta | Descripción | Rol requerido |
|---|---|---|---|
| `GET` | `/api/v1/admin/celery/workers` | Lista workers activos con estado, tareas activas, hostname, colas asignadas | `super_admin` |
| `GET` | `/api/v1/admin/celery/queues` | Estadísticas por cola: tareas pendientes, consumidores activos, tasa de mensajes | `super_admin` |
| `GET` | `/api/v1/admin/celery/tasks` | Tareas recientes con filtros (estado, cola, rango de fechas) y paginación | `super_admin` |
| `POST` | `/api/v1/admin/celery/tasks/{task_id}/revoke` | Revocar una tarea en ejecución | `super_admin` |
| `GET` | `/api/v1/admin/celery/stats` | Estadísticas agregadas: tareas/hora, duración promedio, tasa de fallos, utilización de workers | `super_admin` |
| `GET` | `/api/v1/admin/redis/info` | Info del servidor Redis: memoria, clientes conectados, keys por DB, uptime, ops/sec | `super_admin` |
| `GET` | `/api/v1/admin/redis/queues` | Longitud de colas Redis usadas como broker de Celery | `super_admin` |
| `POST` | `/api/v1/admin/celery/workers/{hostname}/pool/restart` | Reiniciar pool del worker (warm restart) | `super_admin` |

### 1.3 Colas de Celery definidas

| Cola | Propósito | Prioridad |
|---|---|---|
| `webhooks` | Procesamiento de webhooks entrantes (WhatsApp, Telegram, etc.) | Alta |
| `ai_inference` | Invocaciones a LLM vía LangGraph | Alta |
| `documents` | Procesamiento de documentos, embeddings con pgvector | Media |
| `notifications` | Envío de notificaciones (email, push, SMS) | Baja |
| `bulk` | Operaciones masivas (importaciones, exportaciones, campañas) | Baja |

### 1.4 Schemas Pydantic

```python
# app/schemas/admin/celery_schemas.py

from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """Estados posibles de una tarea Celery."""
    PENDING = "PENDING"
    STARTED = "STARTED"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    REVOKED = "REVOKED"
    RETRY = "RETRY"


class CeleryWorkerInfo(BaseModel):
    """Información de un worker de Celery.

    Attributes:
        hostname: Nombre del host del worker.
        status: Estado actual del worker (online/offline).
        active_tasks: Número de tareas actualmente en ejecución.
        processed: Total de tareas procesadas desde el inicio.
        queues: Lista de colas a las que está suscrito.
        pid: PID del proceso principal del worker.
        concurrency: Nivel de concurrencia configurado.
        pool: Tipo de pool (prefork, gevent, solo).
        uptime_seconds: Segundos desde el inicio del worker.
        cpu_usage_percent: Porcentaje de uso de CPU.
        memory_mb: Uso de memoria en MB.
    """
    hostname: str
    status: str = Field(..., pattern="^(online|offline)$")
    active_tasks: int = Field(ge=0)
    processed: int = Field(ge=0)
    queues: list[str]
    pid: int
    concurrency: int
    pool: str
    uptime_seconds: float
    cpu_usage_percent: float = Field(ge=0, le=100)
    memory_mb: float = Field(ge=0)


class CeleryQueueStats(BaseModel):
    """Estadísticas de una cola de Celery.

    Attributes:
        name: Nombre de la cola.
        pending_tasks: Tareas en espera de ser procesadas.
        active_consumers: Número de consumidores activos.
        message_rate: Tasa de mensajes por segundo.
        avg_wait_time_ms: Tiempo promedio de espera en milisegundos.
    """
    name: str
    pending_tasks: int = Field(ge=0)
    active_consumers: int = Field(ge=0)
    message_rate: float = Field(ge=0)
    avg_wait_time_ms: float = Field(ge=0)


class CeleryTaskInfo(BaseModel):
    """Información detallada de una tarea Celery.

    Attributes:
        task_id: ID único de la tarea.
        name: Nombre completo de la tarea (módulo.función).
        status: Estado actual de la tarea.
        queue: Cola donde fue enviada.
        worker: Hostname del worker que la ejecuta.
        args: Argumentos posicionales (truncados a 500 chars).
        kwargs: Argumentos nombrados (truncados a 500 chars).
        result: Resultado de la tarea (si completada).
        exception: Excepción lanzada (si falló).
        traceback: Traceback de la excepción (si falló).
        started_at: Fecha/hora de inicio.
        completed_at: Fecha/hora de finalización.
        runtime_seconds: Duración de ejecución.
        retries: Número de reintentos realizados.
        eta: Hora programada de ejecución (si aplica).
        client_id: UUID del tenant asociado (si aplica).
    """
    task_id: str
    name: str
    status: TaskStatus
    queue: str | None = None
    worker: str | None = None
    args: str | None = None
    kwargs: str | None = None
    result: str | None = None
    exception: str | None = None
    traceback: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    runtime_seconds: float | None = None
    retries: int = 0
    eta: datetime | None = None
    client_id: str | None = None


class CeleryTaskFilter(BaseModel):
    """Filtros para consulta de tareas.

    Attributes:
        status: Filtrar por estado de la tarea.
        queue: Filtrar por nombre de cola.
        task_name: Filtrar por nombre de tarea (coincidencia parcial).
        date_from: Fecha de inicio del rango.
        date_to: Fecha de fin del rango.
        page: Número de página para paginación.
        page_size: Tamaño de página (máx. 100).
    """
    status: TaskStatus | None = None
    queue: str | None = None
    task_name: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=100)


class CeleryAggregateStats(BaseModel):
    """Estadísticas agregadas de Celery.

    Attributes:
        tasks_per_hour: Promedio de tareas procesadas por hora.
        avg_duration_seconds: Duración promedio de ejecución.
        failure_rate_percent: Porcentaje de tareas fallidas.
        worker_utilization_percent: Porcentaje de utilización de workers.
        total_workers: Total de workers activos.
        total_tasks_24h: Total de tareas en las últimas 24 horas.
        tasks_by_queue: Distribución de tareas por cola.
        tasks_by_status: Distribución de tareas por estado.
    """
    tasks_per_hour: float
    avg_duration_seconds: float
    failure_rate_percent: float = Field(ge=0, le=100)
    worker_utilization_percent: float = Field(ge=0, le=100)
    total_workers: int
    total_tasks_24h: int
    tasks_by_queue: dict[str, int]
    tasks_by_status: dict[str, int]


class RevokeTaskRequest(BaseModel):
    """Petición para revocar una tarea.

    Attributes:
        terminate: Si True, envía SIGTERM al proceso.
        signal: Señal a enviar (SIGTERM o SIGKILL).
    """
    terminate: bool = True
    signal: str = Field(default="SIGTERM", pattern="^(SIGTERM|SIGKILL)$")


class RedisInfo(BaseModel):
    """Información del servidor Redis.

    Attributes:
        version: Versión de Redis.
        uptime_seconds: Tiempo de actividad en segundos.
        connected_clients: Número de clientes conectados.
        used_memory_mb: Memoria utilizada en MB.
        used_memory_peak_mb: Pico de memoria utilizada en MB.
        memory_usage_percent: Porcentaje de uso de memoria.
        total_commands_processed: Total de comandos procesados.
        ops_per_second: Operaciones por segundo.
        keyspace: Información por base de datos (keys, expires).
        hit_rate_percent: Tasa de aciertos del caché.
        connected_slaves: Número de réplicas conectadas.
        role: Rol del servidor (master/slave).
    """
    version: str
    uptime_seconds: int
    connected_clients: int
    used_memory_mb: float
    used_memory_peak_mb: float
    memory_usage_percent: float
    total_commands_processed: int
    ops_per_second: int
    keyspace: dict[str, dict[str, int]]
    hit_rate_percent: float
    connected_slaves: int
    role: str


class RedisQueueInfo(BaseModel):
    """Información de una cola Redis.

    Attributes:
        queue_name: Nombre de la cola.
        length: Número de mensajes en la cola.
        consumer_count: Número de consumidores.
    """
    queue_name: str
    length: int = Field(ge=0)
    consumer_count: int = Field(ge=0)
```

### 1.5 Servicios

#### CeleryAdminService

```python
# app/services/admin/celery_admin_service.py

from typing import Any

from celery import Celery
from celery.result import AsyncResult

from app.core.celery_app import celery_app
from app.schemas.admin.celery_schemas import (
    CeleryAggregateStats,
    CeleryQueueStats,
    CeleryTaskFilter,
    CeleryTaskInfo,
    CeleryWorkerInfo,
    RevokeTaskRequest,
    TaskStatus,
)


class CeleryAdminService:
    """Servicio de administración de Celery.

    Provee métodos para inspeccionar workers, colas y tareas,
    así como para revocar tareas y reiniciar pools de workers.

    Attributes:
        app: Instancia de la aplicación Celery.
        inspector: Inspector de Celery para consultar workers.
    """

    def __init__(self, app: Celery | None = None) -> None:
        """Inicializa el servicio con la aplicación Celery.

        Args:
            app: Instancia Celery. Si es None, usa la instancia global.
        """
        self.app = app or celery_app
        self.inspector = self.app.control.inspect()

    async def get_workers(self) -> list[CeleryWorkerInfo]:
        """Obtiene información de todos los workers activos.

        Consulta el estado de los workers usando la API inspect de Celery,
        incluyendo tareas activas, colas asignadas y métricas de recursos.

        Returns:
            Lista de CeleryWorkerInfo con datos de cada worker.

        Raises:
            ConnectionError: Si no se puede conectar al broker.
        """
        import asyncio

        loop = asyncio.get_event_loop()

        # inspect() es síncrono; lo ejecutamos en un thread pool
        ping_result = await loop.run_in_executor(None, self.inspector.ping)
        active_result = await loop.run_in_executor(None, self.inspector.active)
        stats_result = await loop.run_in_executor(None, self.inspector.stats)
        active_queues = await loop.run_in_executor(
            None, self.inspector.active_queues
        )

        if not ping_result:
            return []

        workers: list[CeleryWorkerInfo] = []
        for hostname in ping_result:
            active_tasks = active_result.get(hostname, []) if active_result else []
            stats = stats_result.get(hostname, {}) if stats_result else {}
            queues = active_queues.get(hostname, []) if active_queues else []

            worker_info = CeleryWorkerInfo(
                hostname=hostname,
                status="online",
                active_tasks=len(active_tasks),
                processed=stats.get("total", {}).get(
                    "tasks.total", 0
                ),
                queues=[q["name"] for q in queues],
                pid=stats.get("pid", 0),
                concurrency=stats.get("pool", {}).get(
                    "max-concurrency", 0
                ),
                pool=stats.get("pool", {}).get(
                    "implementation", "unknown"
                ),
                uptime_seconds=stats.get("uptime", 0),
                cpu_usage_percent=0.0,  # Calculado vía Prometheus
                memory_mb=stats.get("rusage", {}).get(
                    "maxrss", 0
                ) / 1024,
            )
            workers.append(worker_info)

        return workers

    async def get_queue_stats(self) -> list[CeleryQueueStats]:
        """Obtiene estadísticas de cada cola definida.

        Consulta Redis directamente para obtener la longitud de cada cola
        y combina con datos del inspector para consumidores activos.

        Returns:
            Lista de CeleryQueueStats por cada cola configurada.
        """
        import asyncio

        from app.core.redis_client import get_redis

        redis = await get_redis()
        queue_names = [
            "webhooks", "ai_inference", "documents",
            "notifications", "bulk",
        ]

        active_queues_result = await asyncio.get_event_loop().run_in_executor(
            None, self.inspector.active_queues
        )

        stats: list[CeleryQueueStats] = []
        for queue_name in queue_names:
            # Las colas de Celery en Redis usan el nombre como key de tipo LIST
            pending = await redis.llen(queue_name)

            # Contar consumidores activos para esta cola
            consumers = 0
            if active_queues_result:
                for _hostname, queues in active_queues_result.items():
                    for q in queues:
                        if q["name"] == queue_name:
                            consumers += 1

            stats.append(
                CeleryQueueStats(
                    name=queue_name,
                    pending_tasks=pending,
                    active_consumers=consumers,
                    message_rate=0.0,  # Calculado vía Prometheus rate()
                    avg_wait_time_ms=0.0,  # Calculado vía Prometheus histogram
                )
            )

        return stats

    async def get_tasks(
        self, filters: CeleryTaskFilter
    ) -> tuple[list[CeleryTaskInfo], int]:
        """Obtiene tareas recientes con filtrado y paginación.

        Consulta el backend de resultados de Celery (PostgreSQL) para
        obtener tareas históricas con los filtros aplicados.

        Args:
            filters: Filtros de búsqueda y paginación.

        Returns:
            Tupla de (lista de tareas, total de resultados).
        """
        from sqlalchemy import select, func, and_
        from sqlalchemy.ext.asyncio import AsyncSession

        from app.core.database import get_async_session
        from app.models.celery_task_log import CeleryTaskLog

        async with get_async_session() as session:
            query = select(CeleryTaskLog)
            count_query = select(func.count()).select_from(CeleryTaskLog)

            conditions = []
            if filters.status:
                conditions.append(
                    CeleryTaskLog.status == filters.status.value
                )
            if filters.queue:
                conditions.append(CeleryTaskLog.queue == filters.queue)
            if filters.task_name:
                conditions.append(
                    CeleryTaskLog.name.ilike(f"%{filters.task_name}%")
                )
            if filters.date_from:
                conditions.append(
                    CeleryTaskLog.started_at >= filters.date_from
                )
            if filters.date_to:
                conditions.append(
                    CeleryTaskLog.started_at <= filters.date_to
                )

            if conditions:
                query = query.where(and_(*conditions))
                count_query = count_query.where(and_(*conditions))

            total = await session.scalar(count_query) or 0

            offset = (filters.page - 1) * filters.page_size
            query = (
                query
                .order_by(CeleryTaskLog.started_at.desc())
                .offset(offset)
                .limit(filters.page_size)
            )

            result = await session.execute(query)
            tasks = [
                CeleryTaskInfo(
                    task_id=row.task_id,
                    name=row.name,
                    status=TaskStatus(row.status),
                    queue=row.queue,
                    worker=row.worker,
                    args=row.args[:500] if row.args else None,
                    kwargs=row.kwargs[:500] if row.kwargs else None,
                    result=str(row.result)[:500] if row.result else None,
                    exception=row.exception,
                    traceback=row.traceback,
                    started_at=row.started_at,
                    completed_at=row.completed_at,
                    runtime_seconds=row.runtime_seconds,
                    retries=row.retries,
                    client_id=str(row.client_id) if row.client_id else None,
                )
                for row in result.scalars()
            ]

        return tasks, total

    async def revoke_task(
        self, task_id: str, request: RevokeTaskRequest
    ) -> dict[str, Any]:
        """Revoca una tarea en ejecución.

        Envía señal de revocación al worker que ejecuta la tarea.

        Args:
            task_id: ID de la tarea a revocar.
            request: Configuración de revocación (terminate, signal).

        Returns:
            Diccionario con el resultado de la operación.

        Raises:
            ValueError: Si la tarea no existe o ya completó.
        """
        import asyncio

        result = AsyncResult(task_id, app=self.app)
        if result.state in ("SUCCESS", "FAILURE"):
            raise ValueError(
                f"La tarea {task_id} ya finalizó con estado {result.state}"
            )

        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self.app.control.revoke(
                task_id,
                terminate=request.terminate,
                signal=request.signal,
            ),
        )

        return {
            "task_id": task_id,
            "revoked": True,
            "signal": request.signal,
            "terminated": request.terminate,
        }

    async def get_aggregate_stats(self) -> CeleryAggregateStats:
        """Calcula estadísticas agregadas de Celery.

        Combina datos del inspector, backend de resultados y Prometheus
        para generar métricas agregadas del sistema de tareas.

        Returns:
            CeleryAggregateStats con métricas consolidadas.
        """
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import select, func, and_
        from sqlalchemy.ext.asyncio import AsyncSession

        from app.core.database import get_async_session
        from app.models.celery_task_log import CeleryTaskLog

        workers = await self.get_workers()
        now = datetime.now(tz=timezone.utc)
        last_24h = now - timedelta(hours=24)

        async with get_async_session() as session:
            # Total de tareas en 24h
            total_24h = await session.scalar(
                select(func.count())
                .select_from(CeleryTaskLog)
                .where(CeleryTaskLog.started_at >= last_24h)
            ) or 0

            # Duración promedio
            avg_duration = await session.scalar(
                select(func.avg(CeleryTaskLog.runtime_seconds))
                .where(
                    and_(
                        CeleryTaskLog.started_at >= last_24h,
                        CeleryTaskLog.runtime_seconds.isnot(None),
                    )
                )
            ) or 0.0

            # Tasa de fallos
            failures = await session.scalar(
                select(func.count())
                .select_from(CeleryTaskLog)
                .where(
                    and_(
                        CeleryTaskLog.started_at >= last_24h,
                        CeleryTaskLog.status == "FAILURE",
                    )
                )
            ) or 0

            # Distribución por cola
            queue_dist_rows = await session.execute(
                select(
                    CeleryTaskLog.queue,
                    func.count().label("count"),
                )
                .where(CeleryTaskLog.started_at >= last_24h)
                .group_by(CeleryTaskLog.queue)
            )
            tasks_by_queue = {
                row.queue or "default": row.count
                for row in queue_dist_rows
            }

            # Distribución por estado
            status_dist_rows = await session.execute(
                select(
                    CeleryTaskLog.status,
                    func.count().label("count"),
                )
                .where(CeleryTaskLog.started_at >= last_24h)
                .group_by(CeleryTaskLog.status)
            )
            tasks_by_status = {
                row.status: row.count for row in status_dist_rows
            }

        failure_rate = (failures / total_24h * 100) if total_24h > 0 else 0.0

        # Utilización: tareas activas / concurrencia total
        total_concurrency = sum(w.concurrency for w in workers) or 1
        total_active = sum(w.active_tasks for w in workers)
        utilization = (total_active / total_concurrency) * 100

        return CeleryAggregateStats(
            tasks_per_hour=total_24h / 24,
            avg_duration_seconds=float(avg_duration),
            failure_rate_percent=failure_rate,
            worker_utilization_percent=min(utilization, 100.0),
            total_workers=len(workers),
            total_tasks_24h=total_24h,
            tasks_by_queue=tasks_by_queue,
            tasks_by_status=tasks_by_status,
        )

    async def restart_worker_pool(self, hostname: str) -> dict[str, Any]:
        """Reinicia el pool de un worker específico (warm restart).

        Envía señal de reinicio al pool del worker. Los procesos
        actuales terminan sus tareas antes de ser reemplazados.

        Args:
            hostname: Nombre del host del worker a reiniciar.

        Returns:
            Diccionario con el resultado de la operación.

        Raises:
            ValueError: Si el worker no existe o no responde.
        """
        import asyncio

        # Verificar que el worker existe
        ping = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self.inspector.ping(destination=[hostname]),
        )
        if not ping or hostname not in ping:
            raise ValueError(f"Worker '{hostname}' no encontrado o no responde")

        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self.app.control.pool_restart(
                destination=[hostname],
                reload=True,
            ),
        )

        return {
            "hostname": hostname,
            "pool_restarted": True,
            "message": "Pool reiniciado. Los procesos actuales terminarán "
                       "sus tareas antes de ser reemplazados.",
        }
```

#### RedisAdminService

```python
# app/services/admin/redis_admin_service.py

from app.core.redis_client import get_redis
from app.schemas.admin.celery_schemas import RedisInfo, RedisQueueInfo


class RedisAdminService:
    """Servicio de administración de Redis.

    Provee métodos para consultar el estado del servidor Redis,
    incluyendo información de memoria, clientes y colas del broker.
    """

    CELERY_QUEUES = [
        "webhooks", "ai_inference", "documents",
        "notifications", "bulk",
    ]

    async def get_server_info(self) -> RedisInfo:
        """Obtiene información completa del servidor Redis.

        Ejecuta el comando INFO de Redis y parsea las secciones
        relevantes: server, clients, memory, stats, keyspace.

        Returns:
            RedisInfo con métricas del servidor.

        Raises:
            ConnectionError: Si no se puede conectar a Redis.
        """
        redis = await get_redis()
        info = await redis.info()

        # Parsear keyspace
        keyspace: dict[str, dict[str, int]] = {}
        for key, value in info.items():
            if key.startswith("db"):
                keyspace[key] = {
                    "keys": value.get("keys", 0),
                    "expires": value.get("expires", 0),
                    "avg_ttl": value.get("avg_ttl", 0),
                }

        # Calcular hit rate
        hits = info.get("keyspace_hits", 0)
        misses = info.get("keyspace_misses", 0)
        total = hits + misses
        hit_rate = (hits / total * 100) if total > 0 else 0.0

        return RedisInfo(
            version=info.get("redis_version", "unknown"),
            uptime_seconds=info.get("uptime_in_seconds", 0),
            connected_clients=info.get("connected_clients", 0),
            used_memory_mb=info.get("used_memory", 0) / (1024 * 1024),
            used_memory_peak_mb=info.get(
                "used_memory_peak", 0
            ) / (1024 * 1024),
            memory_usage_percent=(
                info.get("used_memory", 0)
                / max(info.get("maxmemory", 1), 1)
                * 100
            ) if info.get("maxmemory", 0) > 0 else 0.0,
            total_commands_processed=info.get(
                "total_commands_processed", 0
            ),
            ops_per_second=info.get(
                "instantaneous_ops_per_sec", 0
            ),
            keyspace=keyspace,
            hit_rate_percent=hit_rate,
            connected_slaves=info.get("connected_slaves", 0),
            role=info.get("role", "unknown"),
        )

    async def get_queue_lengths(self) -> list[RedisQueueInfo]:
        """Obtiene las longitudes de las colas de Celery en Redis.

        Consulta directamente las keys de Redis que representan
        las colas del broker de Celery.

        Returns:
            Lista de RedisQueueInfo con información de cada cola.
        """
        redis = await get_redis()
        queues: list[RedisQueueInfo] = []

        for queue_name in self.CELERY_QUEUES:
            length = await redis.llen(queue_name)

            # Contar clientes suscritos (PUBSUB NUMSUB no aplica para listas,
            # se obtiene del inspector de Celery)
            queues.append(
                RedisQueueInfo(
                    queue_name=queue_name,
                    length=length,
                    consumer_count=0,  # Se llena vía CeleryAdminService
                )
            )

        return queues
```

### 1.6 Router de endpoints

```python
# app/api/v1/endpoints/admin/celery_admin.py

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_current_super_admin
from app.schemas.admin.celery_schemas import (
    CeleryAggregateStats,
    CeleryQueueStats,
    CeleryTaskFilter,
    CeleryTaskInfo,
    CeleryWorkerInfo,
    RedisInfo,
    RedisQueueInfo,
    RevokeTaskRequest,
    TaskStatus,
)
from app.services.admin.celery_admin_service import CeleryAdminService
from app.services.admin.redis_admin_service import RedisAdminService

router = APIRouter(
    prefix="/admin",
    tags=["admin-celery-redis"],
    dependencies=[Depends(get_current_super_admin)],
)


@router.get(
    "/celery/workers",
    response_model=list[CeleryWorkerInfo],
    summary="Listar workers de Celery",
    description="Retorna la lista de todos los workers activos con su "
                "estado, tareas activas, hostname y colas asignadas.",
)
async def list_celery_workers() -> list[CeleryWorkerInfo]:
    """Lista todos los workers de Celery activos.

    Returns:
        Lista de workers con su información de estado.
    """
    service = CeleryAdminService()
    return await service.get_workers()


@router.get(
    "/celery/queues",
    response_model=list[CeleryQueueStats],
    summary="Estadísticas de colas Celery",
)
async def get_celery_queues() -> list[CeleryQueueStats]:
    """Obtiene estadísticas de cada cola de Celery.

    Returns:
        Lista de estadísticas por cola.
    """
    service = CeleryAdminService()
    return await service.get_queue_stats()


@router.get(
    "/celery/tasks",
    response_model=dict[str, Any],
    summary="Consultar tareas Celery",
)
async def list_celery_tasks(
    status: TaskStatus | None = Query(None, description="Filtrar por estado"),
    queue: str | None = Query(None, description="Filtrar por cola"),
    task_name: str | None = Query(None, description="Buscar por nombre"),
    date_from: str | None = Query(None, description="Fecha desde (ISO 8601)"),
    date_to: str | None = Query(None, description="Fecha hasta (ISO 8601)"),
    page: int = Query(1, ge=1, description="Número de página"),
    page_size: int = Query(50, ge=1, le=100, description="Tamaño de página"),
) -> dict[str, Any]:
    """Consulta tareas recientes con filtrado y paginación.

    Args:
        status: Filtrar por estado de la tarea.
        queue: Filtrar por nombre de cola.
        task_name: Buscar por nombre de tarea (parcial).
        date_from: Fecha de inicio del rango.
        date_to: Fecha de fin del rango.
        page: Número de página.
        page_size: Tamaño de página.

    Returns:
        Diccionario con items, total, page y page_size.
    """
    from datetime import datetime

    filters = CeleryTaskFilter(
        status=status,
        queue=queue,
        task_name=task_name,
        date_from=datetime.fromisoformat(date_from) if date_from else None,
        date_to=datetime.fromisoformat(date_to) if date_to else None,
        page=page,
        page_size=page_size,
    )
    service = CeleryAdminService()
    tasks, total = await service.get_tasks(filters)

    return {
        "items": tasks,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


@router.post(
    "/celery/tasks/{task_id}/revoke",
    summary="Revocar tarea Celery",
)
async def revoke_celery_task(
    task_id: str,
    request: RevokeTaskRequest,
) -> dict[str, Any]:
    """Revoca una tarea en ejecución.

    Args:
        task_id: ID de la tarea a revocar.
        request: Configuración de revocación.

    Returns:
        Resultado de la operación.
    """
    service = CeleryAdminService()
    try:
        return await service.revoke_task(task_id, request)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.get(
    "/celery/stats",
    response_model=CeleryAggregateStats,
    summary="Estadísticas agregadas de Celery",
)
async def get_celery_stats() -> CeleryAggregateStats:
    """Obtiene estadísticas agregadas del sistema de tareas.

    Returns:
        Métricas consolidadas de las últimas 24 horas.
    """
    service = CeleryAdminService()
    return await service.get_aggregate_stats()


@router.get(
    "/redis/info",
    response_model=RedisInfo,
    summary="Información del servidor Redis",
)
async def get_redis_info() -> RedisInfo:
    """Obtiene información completa del servidor Redis.

    Returns:
        Métricas del servidor Redis.
    """
    service = RedisAdminService()
    return await service.get_server_info()


@router.get(
    "/redis/queues",
    response_model=list[RedisQueueInfo],
    summary="Longitud de colas Redis",
)
async def get_redis_queues() -> list[RedisQueueInfo]:
    """Obtiene las longitudes de las colas de Celery en Redis.

    Returns:
        Lista de colas con su longitud actual.
    """
    service = RedisAdminService()
    return await service.get_queue_lengths()


@router.post(
    "/celery/workers/{hostname}/pool/restart",
    summary="Reiniciar pool del worker",
)
async def restart_worker_pool(hostname: str) -> dict[str, Any]:
    """Reinicia el pool de procesos de un worker específico.

    Args:
        hostname: Nombre del host del worker.

    Returns:
        Resultado de la operación de reinicio.
    """
    service = CeleryAdminService()
    try:
        return await service.restart_worker_pool(hostname)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
```

### 1.7 Modelo de log de tareas (tabla de plataforma, sin `client_id`)

```python
# app/models/celery_task_log.py

import uuid
from datetime import datetime

from sqlalchemy import (
    Column, DateTime, Float, Index, Integer, String, Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CeleryTaskLog(Base):
    """Registro de ejecución de tareas Celery.

    Tabla de plataforma (sin RLS ni client_id).
    Almacena el historial de todas las tareas ejecutadas
    para consulta, auditoría y generación de estadísticas.

    Note:
        Esta tabla NO tiene RLS. Es de plataforma.
        El campo client_id es informativo (indica qué tenant
        originó la tarea), no se usa para filtrado RLS.
    """

    __tablename__ = "celery_task_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    task_id: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False,
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    queue: Mapped[str | None] = mapped_column(String(100), index=True)
    worker: Mapped[str | None] = mapped_column(String(255))
    args: Mapped[str | None] = mapped_column(Text)
    kwargs: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text)
    exception: Mapped[str | None] = mapped_column(Text)
    traceback: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    runtime_seconds: Mapped[float | None] = mapped_column(Float)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), index=True,
    )

    __table_args__ = (
        Index(
            "ix_celery_task_logs_status_started",
            "status",
            "started_at",
        ),
        Index(
            "ix_celery_task_logs_queue_started",
            "queue",
            "started_at",
        ),
    )
```

### 1.8 Migración Alembic

```python
# alembic/versions/xxxx_create_celery_task_logs.py

"""Crear tabla celery_task_logs para panel de administración.

Revision ID: sprint08_celery_001
Revises: <anterior>
Create Date: 2026-09-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "sprint08_celery_001"
down_revision = "<anterior>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "celery_task_logs",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("task_id", sa.String(255), unique=True, index=True, nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, index=True),
        sa.Column("queue", sa.String(100), index=True),
        sa.Column("worker", sa.String(255)),
        sa.Column("args", sa.Text()),
        sa.Column("kwargs", sa.Text()),
        sa.Column("result", sa.Text()),
        sa.Column("exception", sa.Text()),
        sa.Column("traceback", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), index=True),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("runtime_seconds", sa.Float()),
        sa.Column("retries", sa.Integer(), default=0),
        sa.Column("client_id", UUID(as_uuid=True), index=True),
    )
    op.create_index(
        "ix_celery_task_logs_status_started",
        "celery_task_logs",
        ["status", "started_at"],
    )
    op.create_index(
        "ix_celery_task_logs_queue_started",
        "celery_task_logs",
        ["queue", "started_at"],
    )

    # Política de retención: particionamiento por mes (opcional, recomendado)
    # NOTA: Activar pg_partman si el volumen de tareas lo requiere.


def downgrade() -> None:
    op.drop_table("celery_task_logs")
```

### 1.9 Signal handler para capturar eventos de tareas

```python
# app/core/celery_signals.py

"""Handlers de señales de Celery para registro de tareas.

Captura eventos de ciclo de vida de las tareas y los persiste
en la tabla celery_task_logs para consulta desde el panel de admin.
"""

from datetime import datetime, timezone

from celery.signals import (
    task_postrun,
    task_prerun,
    task_failure,
    task_revoked,
    task_retry,
)

from app.core.celery_app import celery_app


@task_prerun.connect
def task_prerun_handler(
    sender: object = None,
    task_id: str | None = None,
    task: object = None,
    args: tuple | None = None,
    kwargs: dict | None = None,
    **kw: object,
) -> None:
    """Registra el inicio de ejecución de una tarea.

    Args:
        sender: Clase de la tarea.
        task_id: ID único de la tarea.
        task: Instancia de la tarea.
        args: Argumentos posicionales.
        kwargs: Argumentos nombrados.
    """
    import asyncio
    from app.services.admin.task_logger import TaskLoggerService

    logger = TaskLoggerService()
    asyncio.get_event_loop().run_until_complete(
        logger.log_task_start(
            task_id=task_id,
            name=sender.name if sender else "unknown",
            queue=getattr(task, "queue", None),
            args=str(args)[:2000] if args else None,
            kwargs=str(kwargs)[:2000] if kwargs else None,
            worker=celery_app.current_worker_task.request.hostname
            if celery_app.current_worker_task else None,
        )
    )


@task_postrun.connect
def task_postrun_handler(
    sender: object = None,
    task_id: str | None = None,
    retval: object = None,
    state: str | None = None,
    **kw: object,
) -> None:
    """Registra la finalización de una tarea.

    Args:
        sender: Clase de la tarea.
        task_id: ID único de la tarea.
        retval: Valor de retorno de la tarea.
        state: Estado final de la tarea.
    """
    import asyncio
    from app.services.admin.task_logger import TaskLoggerService

    logger = TaskLoggerService()
    asyncio.get_event_loop().run_until_complete(
        logger.log_task_complete(
            task_id=task_id,
            status=state or "SUCCESS",
            result=str(retval)[:2000] if retval else None,
        )
    )


@task_failure.connect
def task_failure_handler(
    sender: object = None,
    task_id: str | None = None,
    exception: BaseException | None = None,
    traceback: object = None,
    **kw: object,
) -> None:
    """Registra el fallo de una tarea.

    Args:
        sender: Clase de la tarea.
        task_id: ID único de la tarea.
        exception: Excepción lanzada.
        traceback: Traceback de la excepción.
    """
    import asyncio
    from app.services.admin.task_logger import TaskLoggerService

    logger = TaskLoggerService()
    asyncio.get_event_loop().run_until_complete(
        logger.log_task_failure(
            task_id=task_id,
            exception=str(exception) if exception else None,
            traceback=str(traceback)[:5000] if traceback else None,
        )
    )


@task_revoked.connect
def task_revoked_handler(
    sender: object = None,
    request: object = None,
    terminated: bool = False,
    signum: object = None,
    expired: bool = False,
    **kw: object,
) -> None:
    """Registra la revocación de una tarea.

    Args:
        sender: Clase de la tarea.
        request: Request de la tarea.
        terminated: Si fue terminada con señal.
        signum: Señal enviada.
        expired: Si expiró.
    """
    import asyncio
    from app.services.admin.task_logger import TaskLoggerService

    task_id = getattr(request, "id", None)
    if task_id:
        logger = TaskLoggerService()
        asyncio.get_event_loop().run_until_complete(
            logger.log_task_revoked(task_id=task_id)
        )
```

### 1.10 Métricas Prometheus

```python
# app/core/metrics/celery_metrics.py

"""Métricas de Prometheus para Celery y Redis.

Define contadores, histogramas y gauges para monitorear
el rendimiento del sistema de tareas y el broker Redis.
"""

from prometheus_client import Counter, Gauge, Histogram


# --- Celery ---

celery_task_duration_seconds = Histogram(
    "celery_task_duration_seconds",
    "Duración de ejecución de tareas Celery en segundos",
    labelnames=["task_name", "queue", "status"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
)

celery_tasks_total = Counter(
    "celery_tasks_total",
    "Total de tareas procesadas por Celery",
    labelnames=["task_name", "queue", "status"],
)

celery_queue_length = Gauge(
    "celery_queue_length",
    "Número de tareas pendientes en cada cola de Celery",
    labelnames=["queue"],
)

celery_active_workers = Gauge(
    "celery_active_workers",
    "Número de workers de Celery activos",
)

celery_worker_utilization = Gauge(
    "celery_worker_utilization_percent",
    "Porcentaje de utilización de workers",
)

# --- Redis ---

redis_memory_usage_bytes = Gauge(
    "redis_memory_usage_bytes",
    "Memoria utilizada por Redis en bytes",
)

redis_connected_clients = Gauge(
    "redis_connected_clients",
    "Número de clientes conectados a Redis",
)

redis_ops_per_second = Gauge(
    "redis_ops_per_second",
    "Operaciones por segundo en Redis",
)

redis_keyspace_keys = Gauge(
    "redis_keyspace_keys",
    "Número total de keys en Redis por base de datos",
    labelnames=["db"],
)

redis_hit_rate_percent = Gauge(
    "redis_hit_rate_percent",
    "Tasa de aciertos del caché Redis",
)
```

### 1.11 Tarea periódica de recolección de métricas

```python
# app/tasks/metrics_collector.py

"""Tarea Celery Beat para recolección periódica de métricas.

Ejecuta cada 30 segundos para actualizar los gauges de Prometheus
con datos actuales de Celery y Redis.
"""

from app.core.celery_app import celery_app


@celery_app.task(
    name="metrics.collect_celery_redis",
    queue="notifications",
    ignore_result=True,
)
def collect_celery_redis_metrics() -> None:
    """Recolecta métricas de Celery y Redis y actualiza Prometheus.

    Se ejecuta periódicamente vía Celery Beat para mantener
    los gauges de Prometheus actualizados con datos en tiempo real.
    """
    import asyncio

    asyncio.get_event_loop().run_until_complete(
        _collect_metrics_async()
    )


async def _collect_metrics_async() -> None:
    """Implementación async de la recolección de métricas."""
    from app.core.metrics.celery_metrics import (
        celery_active_workers,
        celery_queue_length,
        celery_worker_utilization,
        redis_connected_clients,
        redis_hit_rate_percent,
        redis_keyspace_keys,
        redis_memory_usage_bytes,
        redis_ops_per_second,
    )
    from app.services.admin.celery_admin_service import CeleryAdminService
    from app.services.admin.redis_admin_service import RedisAdminService

    # Métricas de Celery
    celery_service = CeleryAdminService()
    workers = await celery_service.get_workers()
    celery_active_workers.set(len(workers))

    queues = await celery_service.get_queue_stats()
    for q in queues:
        celery_queue_length.labels(queue=q.name).set(q.pending_tasks)

    total_concurrency = sum(w.concurrency for w in workers) or 1
    total_active = sum(w.active_tasks for w in workers)
    celery_worker_utilization.set(
        min((total_active / total_concurrency) * 100, 100.0)
    )

    # Métricas de Redis
    redis_service = RedisAdminService()
    redis_info = await redis_service.get_server_info()
    redis_memory_usage_bytes.set(
        redis_info.used_memory_mb * 1024 * 1024
    )
    redis_connected_clients.set(redis_info.connected_clients)
    redis_ops_per_second.set(redis_info.ops_per_second)
    redis_hit_rate_percent.set(redis_info.hit_rate_percent)

    for db_name, db_info in redis_info.keyspace.items():
        redis_keyspace_keys.labels(db=db_name).set(db_info.get("keys", 0))
```

### 1.12 Configuración Celery Beat

```python
# Agregar a app/core/celery_config.py -> beat_schedule

beat_schedule = {
    # ... tareas existentes ...

    "collect-celery-redis-metrics": {
        "task": "metrics.collect_celery_redis",
        "schedule": 30.0,  # Cada 30 segundos
        "options": {"queue": "notifications"},
    },
}
```

### 1.13 Dashboard de Grafana: "Celery Operations"

```json
{
  "dashboard": {
    "title": "Celery Operations",
    "uid": "celery-ops-v1",
    "tags": ["celery", "redis", "operations"],
    "timezone": "utc",
    "refresh": "30s",
    "time": {
      "from": "now-6h",
      "to": "now"
    },
    "panels": [
      {
        "title": "Rendimiento de Tareas (tareas/min)",
        "type": "timeseries",
        "gridPos": { "h": 8, "w": 12, "x": 0, "y": 0 },
        "targets": [
          {
            "expr": "rate(celery_tasks_total[5m]) * 60",
            "legendFormat": "{{task_name}} - {{status}}"
          }
        ]
      },
      {
        "title": "Profundidad de Colas",
        "type": "timeseries",
        "gridPos": { "h": 8, "w": 12, "x": 12, "y": 0 },
        "targets": [
          {
            "expr": "celery_queue_length",
            "legendFormat": "{{queue}}"
          }
        ],
        "fieldConfig": {
          "defaults": {
            "thresholds": {
              "steps": [
                { "value": 0, "color": "green" },
                { "value": 500, "color": "yellow" },
                { "value": 1000, "color": "red" }
              ]
            }
          }
        }
      },
      {
        "title": "Duración de Tareas (p50, p95, p99)",
        "type": "timeseries",
        "gridPos": { "h": 8, "w": 12, "x": 0, "y": 8 },
        "targets": [
          {
            "expr": "histogram_quantile(0.50, rate(celery_task_duration_seconds_bucket[5m]))",
            "legendFormat": "p50"
          },
          {
            "expr": "histogram_quantile(0.95, rate(celery_task_duration_seconds_bucket[5m]))",
            "legendFormat": "p95"
          },
          {
            "expr": "histogram_quantile(0.99, rate(celery_task_duration_seconds_bucket[5m]))",
            "legendFormat": "p99"
          }
        ]
      },
      {
        "title": "Tasa de Fallos (%)",
        "type": "stat",
        "gridPos": { "h": 4, "w": 6, "x": 12, "y": 8 },
        "targets": [
          {
            "expr": "rate(celery_tasks_total{status='FAILURE'}[1h]) / rate(celery_tasks_total[1h]) * 100",
            "legendFormat": "Failure Rate"
          }
        ],
        "fieldConfig": {
          "defaults": {
            "thresholds": {
              "steps": [
                { "value": 0, "color": "green" },
                { "value": 2, "color": "yellow" },
                { "value": 5, "color": "red" }
              ]
            },
            "unit": "percent"
          }
        }
      },
      {
        "title": "Workers Activos",
        "type": "stat",
        "gridPos": { "h": 4, "w": 6, "x": 18, "y": 8 },
        "targets": [
          {
            "expr": "celery_active_workers",
            "legendFormat": "Workers"
          }
        ]
      },
      {
        "title": "Utilización de Workers (%)",
        "type": "gauge",
        "gridPos": { "h": 4, "w": 6, "x": 12, "y": 12 },
        "targets": [
          {
            "expr": "celery_worker_utilization_percent",
            "legendFormat": "Utilización"
          }
        ],
        "fieldConfig": {
          "defaults": {
            "thresholds": {
              "steps": [
                { "value": 0, "color": "green" },
                { "value": 70, "color": "yellow" },
                { "value": 90, "color": "red" }
              ]
            },
            "max": 100,
            "unit": "percent"
          }
        }
      },
      {
        "title": "Redis - Memoria Usada (MB)",
        "type": "timeseries",
        "gridPos": { "h": 8, "w": 12, "x": 0, "y": 16 },
        "targets": [
          {
            "expr": "redis_memory_usage_bytes / 1024 / 1024",
            "legendFormat": "Memoria Usada (MB)"
          }
        ]
      },
      {
        "title": "Redis - Ops/sec",
        "type": "timeseries",
        "gridPos": { "h": 8, "w": 12, "x": 12, "y": 16 },
        "targets": [
          {
            "expr": "redis_ops_per_second",
            "legendFormat": "Operaciones/seg"
          }
        ]
      }
    ]
  }
}
```

### 1.14 Tests

```python
# tests/unit/admin/test_celery_admin_service.py

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.schemas.admin.celery_schemas import (
    CeleryTaskFilter,
    RevokeTaskRequest,
    TaskStatus,
)
from app.services.admin.celery_admin_service import CeleryAdminService


@pytest.fixture
def celery_service() -> CeleryAdminService:
    """Fixture que crea un CeleryAdminService con app mockeada."""
    mock_app = MagicMock()
    mock_app.control.inspect.return_value = MagicMock()
    return CeleryAdminService(app=mock_app)


class TestCeleryAdminService:
    """Tests para CeleryAdminService."""

    @pytest.mark.asyncio
    async def test_get_workers_returns_empty_when_no_workers(
        self, celery_service: CeleryAdminService,
    ) -> None:
        """Verifica que retorna lista vacía sin workers."""
        with patch.object(
            celery_service.inspector, "ping", return_value=None
        ):
            workers = await celery_service.get_workers()
            assert workers == []

    @pytest.mark.asyncio
    async def test_get_workers_returns_worker_info(
        self, celery_service: CeleryAdminService,
    ) -> None:
        """Verifica que retorna info correcta de workers activos."""
        mock_ping = {"worker1@host": {"ok": "pong"}}
        mock_active = {"worker1@host": []}
        mock_stats = {
            "worker1@host": {
                "total": {"tasks.total": 100},
                "pid": 12345,
                "pool": {
                    "max-concurrency": 4,
                    "implementation": "prefork",
                },
                "uptime": 3600,
                "rusage": {"maxrss": 102400},
            }
        }
        mock_queues = {
            "worker1@host": [
                {"name": "webhooks"},
                {"name": "ai_inference"},
            ]
        }

        with (
            patch.object(celery_service.inspector, "ping", return_value=mock_ping),
            patch.object(celery_service.inspector, "active", return_value=mock_active),
            patch.object(celery_service.inspector, "stats", return_value=mock_stats),
            patch.object(celery_service.inspector, "active_queues", return_value=mock_queues),
        ):
            workers = await celery_service.get_workers()
            assert len(workers) == 1
            assert workers[0].hostname == "worker1@host"
            assert workers[0].status == "online"
            assert workers[0].queues == ["webhooks", "ai_inference"]

    @pytest.mark.asyncio
    async def test_revoke_task_raises_on_completed_task(
        self, celery_service: CeleryAdminService,
    ) -> None:
        """Verifica que lanza error al revocar tarea completada."""
        with patch(
            "app.services.admin.celery_admin_service.AsyncResult"
        ) as mock_result:
            mock_result.return_value.state = "SUCCESS"
            with pytest.raises(ValueError, match="ya finalizó"):
                await celery_service.revoke_task(
                    "task-123",
                    RevokeTaskRequest(terminate=True, signal="SIGTERM"),
                )

    @pytest.mark.asyncio
    async def test_get_queue_stats_returns_all_queues(
        self, celery_service: CeleryAdminService,
    ) -> None:
        """Verifica que retorna estadísticas de todas las colas."""
        mock_redis = AsyncMock()
        mock_redis.llen.return_value = 5

        with (
            patch(
                "app.services.admin.celery_admin_service.get_redis",
                return_value=mock_redis,
            ),
            patch.object(
                celery_service.inspector,
                "active_queues",
                return_value=None,
            ),
        ):
            stats = await celery_service.get_queue_stats()
            assert len(stats) == 5
            queue_names = [s.name for s in stats]
            assert "webhooks" in queue_names
            assert "ai_inference" in queue_names
            assert "documents" in queue_names
            assert "notifications" in queue_names
            assert "bulk" in queue_names


# tests/unit/admin/test_redis_admin_service.py

class TestRedisAdminService:
    """Tests para RedisAdminService."""

    @pytest.mark.asyncio
    async def test_get_server_info(self) -> None:
        """Verifica que parsea correctamente INFO de Redis."""
        from app.services.admin.redis_admin_service import RedisAdminService

        mock_redis = AsyncMock()
        mock_redis.info.return_value = {
            "redis_version": "7.2.0",
            "uptime_in_seconds": 86400,
            "connected_clients": 10,
            "used_memory": 52428800,
            "used_memory_peak": 104857600,
            "maxmemory": 268435456,
            "total_commands_processed": 1000000,
            "instantaneous_ops_per_sec": 150,
            "keyspace_hits": 9000,
            "keyspace_misses": 1000,
            "connected_slaves": 0,
            "role": "master",
            "db0": {"keys": 500, "expires": 100, "avg_ttl": 3600},
        }

        with patch(
            "app.services.admin.redis_admin_service.get_redis",
            return_value=mock_redis,
        ):
            service = RedisAdminService()
            info = await service.get_server_info()

            assert info.version == "7.2.0"
            assert info.connected_clients == 10
            assert info.hit_rate_percent == 90.0
            assert "db0" in info.keyspace

    @pytest.mark.asyncio
    async def test_get_queue_lengths(self) -> None:
        """Verifica que retorna longitudes de todas las colas."""
        from app.services.admin.redis_admin_service import RedisAdminService

        mock_redis = AsyncMock()
        mock_redis.llen.return_value = 42

        with patch(
            "app.services.admin.redis_admin_service.get_redis",
            return_value=mock_redis,
        ):
            service = RedisAdminService()
            queues = await service.get_queue_lengths()

            assert len(queues) == 5
            assert all(q.length == 42 for q in queues)
```

---

## Feature 2: Bot de Telegram para Monitoreo (Super Admin)

### 2.1 Descripción

Bot de Telegram exclusivo para `super_admin` que permite monitorear el estado del servidor, servicios Docker, base de datos, Redis y Celery desde el móvil. Incluye sistema proactivo de alertas con deduplicación.

### 2.2 Arquitectura

```
                    +------------------+
                    |  Telegram Bot API |
                    +--------+---------+
                             |
                    +--------v---------+
                    |  telegram-bot     |
                    |  (Docker service) |
                    +--------+---------+
                             |
              +--------------+--------------+
              |              |              |
    +---------v--+  +--------v---+  +-------v------+
    |  Docker API|  | Redis      |  | PostgreSQL   |
    +------------+  +------------+  +--------------+
              |              |              |
    +---------v--+  +--------v---+
    |  psutil    |  | Celery     |
    |  (sistema) |  | Inspector  |
    +------------+  +------------+
```

### 2.3 Comandos del Bot

| Comando | Descripción | Ejemplo de respuesta |
|---|---|---|
| `/status` | Estado general del sistema | CPU: 45%, RAM: 62%, Disco: 38%, Uptime: 15d 3h |
| `/docker` | Estado de contenedores Docker | Lista con nombre, estado, CPU%, Mem% |
| `/db` | Estadísticas de PostgreSQL | Conexiones: 24/100, Queries activas: 3, Tamaño: 12.4 GB |
| `/redis` | Estado de Redis | Memoria: 48MB, Clientes: 12, Ops/s: 150 |
| `/celery` | Resumen de workers Celery | Workers: 3, Pendientes: 12, Fallos/h: 0.5% |
| `/logs [servicio] [N]` | Últimas N líneas de un servicio | Log lines del contenedor especificado |
| `/backup` | Estado del último backup | Último: hace 6h, Tamaño: 2.3GB, Duración: 4m 12s |
| `/alerts` | Alertas activas y recientes | Lista de alertas activas con timestamp |
| `/restart [servicio]` | Reiniciar servicio Docker | Confirmación previa, luego reinicio |
| `/help` | Ayuda con todos los comandos | Lista formateada de comandos |

### 2.4 Implementación del Bot

```python
# app/services/monitoring/telegram_bot.py

"""Bot de Telegram para monitoreo de infraestructura.

Implementa un bot que responde a comandos del super_admin
para consultar el estado de los servicios, contenedores Docker,
base de datos, Redis, Celery y sistema operativo.

Seguridad: solo responde a chat_ids configurados en whitelist.
"""

import logging
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.config import settings
from app.services.monitoring.monitoring_service import MonitoringService

logger = logging.getLogger(__name__)


class TelegramMonitorBot:
    """Bot de Telegram para monitoreo de infraestructura.

    Registra handlers de comandos para consultar métricas
    del sistema, Docker, PostgreSQL, Redis y Celery.

    Attributes:
        token: Token del bot de Telegram.
        allowed_chat_ids: Lista de chat_ids autorizados.
        monitoring: Servicio de recolección de métricas.
        app: Instancia de la aplicación del bot.
    """

    def __init__(self) -> None:
        """Inicializa el bot con configuración de .env."""
        self.token: str = settings.TELEGRAM_BOT_TOKEN
        self.allowed_chat_ids: list[int] = [
            int(cid.strip())
            for cid in settings.TELEGRAM_ADMIN_CHAT_IDS.split(",")
        ]
        self.monitoring = MonitoringService()
        self.app: Application | None = None

    def _is_authorized(self, update: Update) -> bool:
        """Verifica si el chat_id está en la whitelist.

        Args:
            update: Update entrante de Telegram.

        Returns:
            True si el chat_id está autorizado.
        """
        chat_id = update.effective_chat.id if update.effective_chat else 0
        return chat_id in self.allowed_chat_ids

    async def start(self) -> None:
        """Inicia el bot y registra los handlers de comandos."""
        self.app = (
            Application.builder()
            .token(self.token)
            .build()
        )

        handlers = [
            CommandHandler("status", self._cmd_status),
            CommandHandler("docker", self._cmd_docker),
            CommandHandler("db", self._cmd_db),
            CommandHandler("redis", self._cmd_redis),
            CommandHandler("celery", self._cmd_celery),
            CommandHandler("logs", self._cmd_logs),
            CommandHandler("backup", self._cmd_backup),
            CommandHandler("alerts", self._cmd_alerts),
            CommandHandler("restart", self._cmd_restart),
            CommandHandler("help", self._cmd_help),
            CallbackQueryHandler(
                self._callback_restart_confirm,
                pattern=r"^restart_confirm:",
            ),
            CallbackQueryHandler(
                self._callback_restart_cancel,
                pattern=r"^restart_cancel$",
            ),
        ]
        for handler in handlers:
            self.app.add_handler(handler)

        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()

        logger.info(
            "Bot de Telegram iniciado. Chat IDs autorizados: %s",
            self.allowed_chat_ids,
        )

    async def stop(self) -> None:
        """Detiene el bot y libera recursos."""
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()

    async def send_alert(self, message: str) -> None:
        """Envía mensaje de alerta a todos los chats autorizados.

        Args:
            message: Texto de la alerta a enviar.
        """
        if not self.app:
            logger.warning("Bot no inicializado, no se puede enviar alerta")
            return

        for chat_id in self.allowed_chat_ids:
            try:
                await self.app.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception(
                    "Error enviando alerta a chat_id %s", chat_id,
                )

    # --- Handlers de comandos ---

    async def _cmd_status(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handler para /status - Estado general del sistema.

        Args:
            update: Update entrante.
            context: Contexto del bot.
        """
        if not self._is_authorized(update):
            return

        metrics = await self.monitoring.get_system_metrics()

        text = (
            "<b>Estado del Sistema</b>\n\n"
            f"CPU: {metrics['cpu_percent']:.1f}%\n"
            f"RAM: {metrics['memory_percent']:.1f}% "
            f"({metrics['memory_used_gb']:.1f}/{metrics['memory_total_gb']:.1f} GB)\n"
            f"Disco: {metrics['disk_percent']:.1f}% "
            f"({metrics['disk_used_gb']:.1f}/{metrics['disk_total_gb']:.1f} GB)\n"
            f"Uptime: {metrics['uptime']}\n"
            f"Load Average: {metrics['load_avg']}\n"
        )

        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_docker(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handler para /docker - Estado de contenedores Docker.

        Args:
            update: Update entrante.
            context: Contexto del bot.
        """
        if not self._is_authorized(update):
            return

        containers = await self.monitoring.get_docker_containers()

        lines = ["<b>Contenedores Docker</b>\n"]
        for c in containers:
            status_emoji = {
                "running": "OK",
                "exited": "DETENIDO",
                "restarting": "REINICIANDO",
            }.get(c["status"], c["status"].upper())

            lines.append(
                f"<code>{c['name']:<20}</code> [{status_emoji}] "
                f"CPU: {c['cpu_percent']:.1f}% | "
                f"Mem: {c['memory_mb']:.0f}MB"
            )

        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML",
        )

    async def _cmd_db(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handler para /db - Estadísticas de PostgreSQL.

        Args:
            update: Update entrante.
            context: Contexto del bot.
        """
        if not self._is_authorized(update):
            return

        db_stats = await self.monitoring.get_database_stats()

        text = (
            "<b>Base de Datos PostgreSQL</b>\n\n"
            f"Conexiones activas: {db_stats['active_connections']}"
            f"/{db_stats['max_connections']}\n"
            f"Queries en ejecución: {db_stats['active_queries']}\n"
            f"Tamaño total: {db_stats['total_size']}\n"
            f"Lag de replicación: {db_stats['replication_lag']}\n"
            "\n<b>Tablas más grandes:</b>\n"
        )

        for table in db_stats["largest_tables"][:5]:
            text += f"  - {table['name']}: {table['size']}\n"

        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_redis(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handler para /redis - Estado de Redis.

        Args:
            update: Update entrante.
            context: Contexto del bot.
        """
        if not self._is_authorized(update):
            return

        redis_info = await self.monitoring.get_redis_stats()

        text = (
            "<b>Redis</b>\n\n"
            f"Versión: {redis_info['version']}\n"
            f"Memoria: {redis_info['used_memory_mb']:.1f}MB\n"
            f"Clientes conectados: {redis_info['connected_clients']}\n"
            f"Ops/seg: {redis_info['ops_per_second']}\n"
            f"Hit rate: {redis_info['hit_rate']:.1f}%\n"
            "\n<b>Colas Celery:</b>\n"
        )

        for q in redis_info["queues"]:
            text += f"  - {q['name']}: {q['length']} pendientes\n"

        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_celery(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handler para /celery - Resumen de workers Celery.

        Args:
            update: Update entrante.
            context: Contexto del bot.
        """
        if not self._is_authorized(update):
            return

        celery_stats = await self.monitoring.get_celery_stats()

        text = (
            "<b>Celery Workers</b>\n\n"
            f"Workers activos: {celery_stats['active_workers']}\n"
            f"Tareas pendientes: {celery_stats['total_pending']}\n"
            f"Tareas/hora: {celery_stats['tasks_per_hour']:.0f}\n"
            f"Tasa de fallos: {celery_stats['failure_rate']:.2f}%\n"
            "\n<b>Por cola:</b>\n"
        )

        for q in celery_stats["queues"]:
            text += (
                f"  - {q['name']}: {q['pending']} pendientes, "
                f"{q['consumers']} consumidores\n"
            )

        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_logs(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handler para /logs [servicio] [N] - Últimas N líneas de log.

        Args:
            update: Update entrante.
            context: Contexto del bot.
        """
        if not self._is_authorized(update):
            return

        args = context.args or []
        if len(args) < 1:
            await update.message.reply_text(
                "Uso: /logs <servicio> [líneas]\n"
                "Ejemplo: /logs api 50"
            )
            return

        service_name = args[0]
        num_lines = int(args[1]) if len(args) > 1 else 20
        num_lines = min(num_lines, 100)  # Máximo 100 líneas

        try:
            logs = await self.monitoring.get_service_logs(
                service_name, num_lines,
            )
            # Telegram tiene límite de 4096 chars por mensaje
            if len(logs) > 4000:
                logs = logs[-4000:]
                logs = "...(truncado)\n" + logs

            await update.message.reply_text(
                f"<b>Logs: {service_name}</b> (últimas {num_lines} líneas)\n\n"
                f"<pre>{logs}</pre>",
                parse_mode="HTML",
            )
        except ValueError as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_backup(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handler para /backup - Estado del último backup.

        Args:
            update: Update entrante.
            context: Contexto del bot.
        """
        if not self._is_authorized(update):
            return

        backup_info = await self.monitoring.get_backup_status()

        status_text = "EXITOSO" if backup_info["success"] else "FALLIDO"

        text = (
            "<b>Estado de Backup</b>\n\n"
            f"Último backup: {backup_info['last_backup_time']}\n"
            f"Estado: {status_text}\n"
            f"Tamaño: {backup_info['size']}\n"
            f"Duración: {backup_info['duration']}\n"
            f"Destino: {backup_info['destination']}\n"
        )

        if backup_info.get("next_scheduled"):
            text += f"Próximo programado: {backup_info['next_scheduled']}\n"

        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_alerts(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handler para /alerts - Alertas activas y recientes.

        Args:
            update: Update entrante.
            context: Contexto del bot.
        """
        if not self._is_authorized(update):
            return

        alerts = await self.monitoring.get_alerts()

        if not alerts["active"] and not alerts["recent_resolved"]:
            await update.message.reply_text(
                "Sin alertas activas ni recientes."
            )
            return

        text = "<b>Alertas</b>\n\n"

        if alerts["active"]:
            text += "<b>ACTIVAS:</b>\n"
            for alert in alerts["active"]:
                text += (
                    f"  [!] {alert['message']}\n"
                    f"      Desde: {alert['since']}\n\n"
                )

        if alerts["recent_resolved"]:
            text += "<b>Resueltas (últimas 24h):</b>\n"
            for alert in alerts["recent_resolved"][:10]:
                text += (
                    f"  [OK] {alert['message']}\n"
                    f"       Resuelto: {alert['resolved_at']}\n\n"
                )

        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_restart(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handler para /restart [servicio] - Reiniciar servicio Docker.

        Requiere confirmación mediante botón inline antes de ejecutar.

        Args:
            update: Update entrante.
            context: Contexto del bot.
        """
        if not self._is_authorized(update):
            return

        args = context.args or []
        if len(args) < 1:
            await update.message.reply_text(
                "Uso: /restart <servicio>\n"
                "Ejemplo: /restart api"
            )
            return

        service_name = args[0]

        # Validar que el servicio existe
        containers = await self.monitoring.get_docker_containers()
        valid_services = [c["name"] for c in containers]

        if service_name not in valid_services:
            await update.message.reply_text(
                f"Servicio '{service_name}' no encontrado.\n"
                f"Servicios disponibles: {', '.join(valid_services)}"
            )
            return

        # Solicitar confirmación
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "Confirmar reinicio",
                    callback_data=f"restart_confirm:{service_name}",
                ),
                InlineKeyboardButton(
                    "Cancelar",
                    callback_data="restart_cancel",
                ),
            ]
        ])

        await update.message.reply_text(
            f"Confirma reinicio del servicio <b>{service_name}</b>?",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    async def _callback_restart_confirm(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Callback de confirmación de reinicio de servicio.

        Args:
            update: Update con callback query.
            context: Contexto del bot.
        """
        query = update.callback_query
        await query.answer()

        if not self._is_authorized(update):
            return

        service_name = query.data.split(":")[1]

        await query.edit_message_text(
            f"Reiniciando <b>{service_name}</b>...",
            parse_mode="HTML",
        )

        try:
            result = await self.monitoring.restart_docker_service(
                service_name,
            )
            await query.edit_message_text(
                f"Servicio <b>{service_name}</b> reiniciado exitosamente.\n"
                f"Tiempo de reinicio: {result['restart_time']}s",
                parse_mode="HTML",
            )
        except Exception as e:
            await query.edit_message_text(
                f"Error reiniciando {service_name}: {e}",
                parse_mode="HTML",
            )

    async def _callback_restart_cancel(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Callback de cancelación de reinicio.

        Args:
            update: Update con callback query.
            context: Contexto del bot.
        """
        query = update.callback_query
        await query.answer()
        await query.edit_message_text("Reinicio cancelado.")

    async def _cmd_help(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handler para /help - Lista de comandos disponibles.

        Args:
            update: Update entrante.
            context: Contexto del bot.
        """
        if not self._is_authorized(update):
            return

        text = (
            "<b>Comandos disponibles</b>\n\n"
            "/status - Estado del sistema (CPU, RAM, disco)\n"
            "/docker - Estado de contenedores Docker\n"
            "/db - Estadísticas de PostgreSQL\n"
            "/redis - Estado de Redis\n"
            "/celery - Resumen de workers Celery\n"
            "/logs [servicio] [N] - Últimas N líneas de log\n"
            "/backup - Estado del último backup\n"
            "/alerts - Alertas activas y recientes\n"
            "/restart [servicio] - Reiniciar servicio Docker\n"
            "/help - Este mensaje de ayuda\n"
        )

        await update.message.reply_text(text, parse_mode="HTML")
```

### 2.5 Servicio de Monitoreo

```python
# app/services/monitoring/monitoring_service.py

"""Servicio centralizado de recolección de métricas del sistema.

Recopila métricas de CPU, memoria, disco, Docker, PostgreSQL,
Redis y Celery. Usado tanto por el bot de Telegram como por
el sistema de alertas proactivas.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import docker
import psutil

from app.core.config import settings

logger = logging.getLogger(__name__)


class MonitoringService:
    """Servicio de recolección de métricas de infraestructura.

    Centraliza la obtención de métricas de todos los componentes
    del sistema para uso del bot de Telegram y alertas.

    Attributes:
        docker_client: Cliente Docker para gestión de contenedores.
    """

    def __init__(self) -> None:
        """Inicializa el servicio con conexión a Docker."""
        self.docker_client = docker.DockerClient(
            base_url="unix:///var/run/docker.sock",
        )

    async def get_system_metrics(self) -> dict[str, Any]:
        """Obtiene métricas del sistema operativo.

        Recopila uso de CPU, memoria, disco y uptime usando psutil.

        Returns:
            Diccionario con métricas del sistema.
        """
        loop = asyncio.get_event_loop()

        cpu_percent = await loop.run_in_executor(
            None, lambda: psutil.cpu_percent(interval=1),
        )
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        boot_time = datetime.fromtimestamp(
            psutil.boot_time(), tz=timezone.utc,
        )
        uptime = datetime.now(tz=timezone.utc) - boot_time
        load_avg = psutil.getloadavg()

        # Formatear uptime legible
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes = remainder // 60
        uptime_str = f"{days}d {hours}h {minutes}m"

        return {
            "cpu_percent": cpu_percent,
            "memory_percent": memory.percent,
            "memory_used_gb": memory.used / (1024 ** 3),
            "memory_total_gb": memory.total / (1024 ** 3),
            "disk_percent": disk.percent,
            "disk_used_gb": disk.used / (1024 ** 3),
            "disk_total_gb": disk.total / (1024 ** 3),
            "uptime": uptime_str,
            "load_avg": f"{load_avg[0]:.2f}, {load_avg[1]:.2f}, {load_avg[2]:.2f}",
        }

    async def get_docker_containers(self) -> list[dict[str, Any]]:
        """Obtiene estado de todos los contenedores Docker.

        Consulta la API de Docker para obtener nombre, estado,
        uso de CPU y memoria de cada contenedor.

        Returns:
            Lista de diccionarios con info de cada contenedor.
        """
        loop = asyncio.get_event_loop()
        containers = await loop.run_in_executor(
            None,
            lambda: self.docker_client.containers.list(all=True),
        )

        result: list[dict[str, Any]] = []
        for container in containers:
            stats: dict[str, Any] = {
                "name": container.name,
                "status": container.status,
                "cpu_percent": 0.0,
                "memory_mb": 0.0,
            }

            if container.status == "running":
                try:
                    raw_stats = await loop.run_in_executor(
                        None,
                        lambda c=container: c.stats(stream=False),
                    )
                    # Calcular CPU %
                    cpu_delta = (
                        raw_stats["cpu_stats"]["cpu_usage"]["total_usage"]
                        - raw_stats["precpu_stats"]["cpu_usage"]["total_usage"]
                    )
                    system_delta = (
                        raw_stats["cpu_stats"]["system_cpu_usage"]
                        - raw_stats["precpu_stats"]["system_cpu_usage"]
                    )
                    num_cpus = raw_stats["cpu_stats"]["online_cpus"]
                    if system_delta > 0:
                        stats["cpu_percent"] = (
                            (cpu_delta / system_delta) * num_cpus * 100
                        )

                    # Memoria
                    stats["memory_mb"] = (
                        raw_stats["memory_stats"].get("usage", 0)
                        / (1024 * 1024)
                    )
                except Exception:
                    logger.warning(
                        "No se pudieron obtener stats de %s",
                        container.name,
                    )

            result.append(stats)

        return result

    async def get_database_stats(self) -> dict[str, Any]:
        """Obtiene estadísticas de PostgreSQL.

        Consulta vistas del sistema de PostgreSQL para obtener
        conexiones activas, tamaño de tablas y lag de replicación.

        Returns:
            Diccionario con estadísticas de la base de datos.
        """
        from sqlalchemy import text as sa_text
        from app.core.database import get_async_session

        async with get_async_session() as session:
            # Conexiones activas
            result = await session.execute(
                sa_text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE state = 'active'"
                )
            )
            active_connections = result.scalar() or 0

            # Máx conexiones
            result = await session.execute(
                sa_text("SHOW max_connections")
            )
            max_connections = int(result.scalar() or 100)

            # Queries activas
            result = await session.execute(
                sa_text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE state = 'active' AND query NOT LIKE '%pg_stat%'"
                )
            )
            active_queries = result.scalar() or 0

            # Tamaño total
            result = await session.execute(
                sa_text(
                    "SELECT pg_size_pretty(pg_database_size(current_database()))"
                )
            )
            total_size = result.scalar() or "0 bytes"

            # Tablas más grandes
            result = await session.execute(
                sa_text(
                    "SELECT relname as name, "
                    "pg_size_pretty(pg_total_relation_size(relid)) as size "
                    "FROM pg_catalog.pg_statio_user_tables "
                    "ORDER BY pg_total_relation_size(relid) DESC "
                    "LIMIT 10"
                )
            )
            largest_tables = [
                {"name": row.name, "size": row.size}
                for row in result
            ]

            # Lag de replicación
            result = await session.execute(
                sa_text(
                    "SELECT COALESCE("
                    "  EXTRACT(EPOCH FROM replay_lag)::text || 's', "
                    "  'N/A'"
                    ") "
                    "FROM pg_stat_replication "
                    "LIMIT 1"
                )
            )
            replication_lag = result.scalar() or "Sin réplica"

        return {
            "active_connections": active_connections,
            "max_connections": max_connections,
            "active_queries": active_queries,
            "total_size": total_size,
            "largest_tables": largest_tables,
            "replication_lag": replication_lag,
        }

    async def get_redis_stats(self) -> dict[str, Any]:
        """Obtiene estadísticas de Redis.

        Returns:
            Diccionario con métricas del servidor Redis y colas.
        """
        from app.services.admin.redis_admin_service import RedisAdminService

        service = RedisAdminService()
        info = await service.get_server_info()
        queues = await service.get_queue_lengths()

        return {
            "version": info.version,
            "used_memory_mb": info.used_memory_mb,
            "connected_clients": info.connected_clients,
            "ops_per_second": info.ops_per_second,
            "hit_rate": info.hit_rate_percent,
            "queues": [
                {"name": q.queue_name, "length": q.length}
                for q in queues
            ],
        }

    async def get_celery_stats(self) -> dict[str, Any]:
        """Obtiene estadísticas de Celery.

        Returns:
            Diccionario con métricas de workers y colas de Celery.
        """
        from app.services.admin.celery_admin_service import CeleryAdminService

        service = CeleryAdminService()
        workers = await service.get_workers()
        queue_stats = await service.get_queue_stats()
        aggregate = await service.get_aggregate_stats()

        return {
            "active_workers": len(workers),
            "total_pending": sum(q.pending_tasks for q in queue_stats),
            "tasks_per_hour": aggregate.tasks_per_hour,
            "failure_rate": aggregate.failure_rate_percent,
            "queues": [
                {
                    "name": q.name,
                    "pending": q.pending_tasks,
                    "consumers": q.active_consumers,
                }
                for q in queue_stats
            ],
        }

    async def get_service_logs(
        self, service_name: str, num_lines: int = 20,
    ) -> str:
        """Obtiene las últimas N líneas de log de un servicio Docker.

        Args:
            service_name: Nombre del contenedor Docker.
            num_lines: Número de líneas a obtener (máx. 100).

        Returns:
            String con las líneas de log.

        Raises:
            ValueError: Si el servicio no existe.
        """
        loop = asyncio.get_event_loop()

        try:
            container = await loop.run_in_executor(
                None,
                lambda: self.docker_client.containers.get(service_name),
            )
        except docker.errors.NotFound:
            raise ValueError(f"Servicio '{service_name}' no encontrado")

        logs = await loop.run_in_executor(
            None,
            lambda: container.logs(tail=num_lines, timestamps=True).decode(
                "utf-8", errors="replace",
            ),
        )

        return logs

    async def get_backup_status(self) -> dict[str, Any]:
        """Obtiene estado del último backup.

        Consulta la tabla de registro de backups para obtener
        información del último backup realizado.

        Returns:
            Diccionario con información del último backup.
        """
        from sqlalchemy import text as sa_text
        from app.core.database import get_async_session

        async with get_async_session() as session:
            result = await session.execute(
                sa_text(
                    "SELECT * FROM backup_logs "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
            row = result.first()

            if not row:
                return {
                    "success": False,
                    "last_backup_time": "Nunca",
                    "size": "N/A",
                    "duration": "N/A",
                    "destination": "N/A",
                }

            return {
                "success": row.success,
                "last_backup_time": row.created_at.strftime(
                    "%Y-%m-%d %H:%M UTC"
                ),
                "size": row.size_human,
                "duration": row.duration_human,
                "destination": row.destination,
                "next_scheduled": "02:00 UTC (diario)",
            }

    async def get_alerts(self) -> dict[str, list[dict[str, Any]]]:
        """Obtiene alertas activas y resueltas recientemente.

        Returns:
            Diccionario con listas de alertas activas y resueltas.
        """
        from app.core.redis_client import get_redis

        redis = await get_redis()

        # Alertas activas se almacenan como hash en Redis
        active_keys = await redis.keys("alert:active:*")
        active: list[dict[str, Any]] = []
        for key in active_keys:
            alert_data = await redis.hgetall(key)
            if alert_data:
                active.append({
                    "message": alert_data.get(b"message", b"").decode(),
                    "since": alert_data.get(b"since", b"").decode(),
                    "severity": alert_data.get(b"severity", b"warning").decode(),
                })

        # Alertas resueltas en las últimas 24h
        resolved_keys = await redis.keys("alert:resolved:*")
        recent_resolved: list[dict[str, Any]] = []
        for key in resolved_keys:
            alert_data = await redis.hgetall(key)
            if alert_data:
                recent_resolved.append({
                    "message": alert_data.get(b"message", b"").decode(),
                    "resolved_at": alert_data.get(
                        b"resolved_at", b""
                    ).decode(),
                })

        return {
            "active": active,
            "recent_resolved": recent_resolved,
        }

    async def restart_docker_service(
        self, service_name: str,
    ) -> dict[str, Any]:
        """Reinicia un servicio Docker.

        Args:
            service_name: Nombre del contenedor a reiniciar.

        Returns:
            Diccionario con resultado del reinicio.

        Raises:
            ValueError: Si el servicio no existe.
            RuntimeError: Si el reinicio falla.
        """
        import time

        loop = asyncio.get_event_loop()
        start_time = time.monotonic()

        try:
            container = await loop.run_in_executor(
                None,
                lambda: self.docker_client.containers.get(service_name),
            )
        except docker.errors.NotFound:
            raise ValueError(f"Servicio '{service_name}' no encontrado")

        await loop.run_in_executor(
            None, lambda: container.restart(timeout=30),
        )

        elapsed = time.monotonic() - start_time

        return {
            "service": service_name,
            "restarted": True,
            "restart_time": round(elapsed, 1),
        }
```

### 2.6 Gestor de Alertas

```python
# app/services/monitoring/alert_manager.py

"""Gestor de alertas proactivas.

Evalúa umbrales de métricas del sistema y envía alertas
vía Telegram cuando se superan. Incluye deduplicación
para evitar alertas repetidas en períodos cortos.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)


class AlertThreshold:
    """Definición de un umbral de alerta.

    Attributes:
        name: Identificador único del umbral.
        description: Descripción legible de la alerta.
        severity: Nivel de severidad (critical, warning, info).
        cooldown_seconds: Tiempo mínimo entre alertas duplicadas.
    """

    def __init__(
        self,
        name: str,
        description: str,
        severity: str = "warning",
        cooldown_seconds: int = 900,
    ) -> None:
        self.name = name
        self.description = description
        self.severity = severity
        self.cooldown_seconds = cooldown_seconds


# Definición de umbrales
ALERT_THRESHOLDS: dict[str, dict[str, Any]] = {
    "cpu_high": {
        "metric": "cpu_percent",
        "threshold": 90.0,
        "operator": ">",
        "sustained_seconds": 300,  # 5 minutos
        "message": "CPU por encima del 90% por más de 5 minutos",
        "severity": "critical",
        "cooldown": 900,
    },
    "memory_high": {
        "metric": "memory_percent",
        "threshold": 85.0,
        "operator": ">",
        "message": "Uso de RAM por encima del 85%",
        "severity": "warning",
        "cooldown": 900,
    },
    "disk_high": {
        "metric": "disk_percent",
        "threshold": 80.0,
        "operator": ">",
        "message": "Uso de disco por encima del 80%",
        "severity": "warning",
        "cooldown": 900,
    },
    "docker_crash": {
        "metric": "container_restart",
        "threshold": 0,
        "operator": "event",
        "message": "Contenedor Docker reiniciado/caído: {container_name}",
        "severity": "critical",
        "cooldown": 300,
    },
    "celery_queue_depth": {
        "metric": "celery_queue_length",
        "threshold": 1000,
        "operator": ">",
        "message": "Cola Celery '{queue_name}' con más de 1000 tareas pendientes",
        "severity": "warning",
        "cooldown": 900,
    },
    "db_connections_high": {
        "metric": "db_connections_percent",
        "threshold": 80.0,
        "operator": ">",
        "message": "Conexiones de BD por encima del 80% del máximo",
        "severity": "warning",
        "cooldown": 900,
    },
    "backup_failure": {
        "metric": "backup_status",
        "threshold": 0,
        "operator": "event",
        "message": "Fallo en backup de base de datos",
        "severity": "critical",
        "cooldown": 300,
    },
    "api_error_rate": {
        "metric": "api_error_rate_percent",
        "threshold": 5.0,
        "operator": ">",
        "message": "Tasa de errores API por encima del 5% en los últimos 5 minutos",
        "severity": "critical",
        "cooldown": 300,
    },
}


class AlertManager:
    """Gestor de alertas con deduplicación via Redis.

    Evalúa métricas contra umbrales definidos, envía alertas
    vía Telegram y evita duplicados usando TTL en Redis.

    Attributes:
        monitoring: Servicio de monitoreo para obtener métricas.
        bot: Bot de Telegram para enviar alertas.
    """

    DEDUP_PREFIX = "alert:dedup:"
    ACTIVE_PREFIX = "alert:active:"
    RESOLVED_PREFIX = "alert:resolved:"

    def __init__(self) -> None:
        """Inicializa el AlertManager."""
        from app.services.monitoring.monitoring_service import MonitoringService
        self.monitoring = MonitoringService()
        self.bot = None  # Se inyecta al iniciar

    def set_bot(self, bot: Any) -> None:
        """Inyecta la instancia del bot de Telegram.

        Args:
            bot: Instancia de TelegramMonitorBot.
        """
        self.bot = bot

    async def check_all_thresholds(self) -> list[dict[str, Any]]:
        """Evalúa todas las métricas contra sus umbrales.

        Recopila métricas del sistema, Docker, Redis, Celery y BD.
        Compara cada métrica contra su umbral definido.
        Envía alertas vía Telegram si se supera el umbral y no
        existe un duplicado reciente.

        Returns:
            Lista de alertas disparadas en esta ejecución.
        """
        triggered: list[dict[str, Any]] = []

        try:
            # Métricas del sistema
            system = await self.monitoring.get_system_metrics()
            await self._check_threshold(
                "cpu_high", system["cpu_percent"], triggered,
            )
            await self._check_threshold(
                "memory_high", system["memory_percent"], triggered,
            )
            await self._check_threshold(
                "disk_high", system["disk_percent"], triggered,
            )

            # Docker: detectar contenedores caídos/reiniciando
            containers = await self.monitoring.get_docker_containers()
            for c in containers:
                if c["status"] in ("exited", "restarting"):
                    await self._check_event(
                        "docker_crash",
                        {"container_name": c["name"]},
                        triggered,
                    )

            # Celery: profundidad de colas
            celery_stats = await self.monitoring.get_celery_stats()
            for q in celery_stats["queues"]:
                if q["pending"] > 1000:
                    await self._check_event(
                        "celery_queue_depth",
                        {"queue_name": q["name"]},
                        triggered,
                    )

            # BD: conexiones
            db_stats = await self.monitoring.get_database_stats()
            db_conn_percent = (
                db_stats["active_connections"]
                / db_stats["max_connections"]
                * 100
            )
            await self._check_threshold(
                "db_connections_high", db_conn_percent, triggered,
            )

        except Exception:
            logger.exception("Error evaluando umbrales de alerta")

        return triggered

    async def _check_threshold(
        self,
        alert_name: str,
        current_value: float,
        triggered: list[dict[str, Any]],
    ) -> None:
        """Evalúa un valor numérico contra su umbral.

        Args:
            alert_name: Nombre del umbral a evaluar.
            current_value: Valor actual de la métrica.
            triggered: Lista donde agregar alertas disparadas.
        """
        config = ALERT_THRESHOLDS[alert_name]

        if current_value > config["threshold"]:
            sent = await self._send_alert_if_not_duplicate(
                alert_name,
                config["message"],
                config["severity"],
                config["cooldown"],
                current_value=current_value,
            )
            if sent:
                triggered.append({
                    "name": alert_name,
                    "value": current_value,
                    "threshold": config["threshold"],
                })
        else:
            # Resolver alerta si existía
            await self._resolve_alert(alert_name)

    async def _check_event(
        self,
        alert_name: str,
        context: dict[str, str],
        triggered: list[dict[str, Any]],
    ) -> None:
        """Evalúa un evento (sin valor numérico).

        Args:
            alert_name: Nombre del umbral de evento.
            context: Diccionario con variables para el mensaje.
            triggered: Lista donde agregar alertas disparadas.
        """
        config = ALERT_THRESHOLDS[alert_name]
        message = config["message"].format(**context)
        dedup_key = f"{alert_name}:{':'.join(context.values())}"

        sent = await self._send_alert_if_not_duplicate(
            dedup_key,
            message,
            config["severity"],
            config["cooldown"],
        )
        if sent:
            triggered.append({
                "name": alert_name,
                "context": context,
            })

    async def _send_alert_if_not_duplicate(
        self,
        alert_key: str,
        message: str,
        severity: str,
        cooldown: int,
        current_value: float | None = None,
    ) -> bool:
        """Envía alerta si no existe duplicado reciente en Redis.

        Usa una key con TTL en Redis para deduplicación.
        Si la key existe, la alerta no se reenvía.

        Args:
            alert_key: Clave única para deduplicación.
            message: Texto de la alerta.
            severity: Nivel de severidad.
            cooldown: Segundos de cooldown antes de reenviar.
            current_value: Valor actual de la métrica (opcional).

        Returns:
            True si la alerta fue enviada, False si fue deduplicada.
        """
        redis = await get_redis()
        dedup_key = f"{self.DEDUP_PREFIX}{alert_key}"

        # Verificar deduplicación
        exists = await redis.exists(dedup_key)
        if exists:
            return False

        # Marcar como enviada con TTL
        await redis.setex(dedup_key, cooldown, "1")

        # Registrar como alerta activa
        now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        active_key = f"{self.ACTIVE_PREFIX}{alert_key}"
        await redis.hset(active_key, mapping={
            "message": message,
            "since": now,
            "severity": severity,
            "value": str(current_value) if current_value else "",
        })

        # Formatear y enviar
        severity_label = {
            "critical": "[CRITICO]",
            "warning": "[ADVERTENCIA]",
            "info": "[INFO]",
        }.get(severity, "[ALERTA]")

        formatted_message = (
            f"<b>{severity_label}</b>\n\n"
            f"{message}\n"
        )
        if current_value is not None:
            formatted_message += f"Valor actual: {current_value:.1f}\n"
        formatted_message += f"Hora: {now}"

        if self.bot:
            await self.bot.send_alert(formatted_message)
        else:
            logger.warning(
                "Bot no configurado, alerta no enviada: %s", message,
            )

        return True

    async def _resolve_alert(self, alert_key: str) -> None:
        """Marca una alerta como resuelta.

        Mueve la alerta de activa a resuelta con TTL de 24h.

        Args:
            alert_key: Clave de la alerta a resolver.
        """
        redis = await get_redis()
        active_key = f"{self.ACTIVE_PREFIX}{alert_key}"

        alert_data = await redis.hgetall(active_key)
        if alert_data:
            now = datetime.now(tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
            resolved_key = f"{self.RESOLVED_PREFIX}{alert_key}:{now}"
            await redis.hset(resolved_key, mapping={
                **{k.decode(): v.decode() for k, v in alert_data.items()},
                "resolved_at": now,
            })
            await redis.expire(resolved_key, 86400)  # 24h TTL
            await redis.delete(active_key)
```

### 2.7 Tarea Celery Beat de monitoreo

```python
# app/tasks/monitoring_check.py

"""Tarea periódica de verificación de umbrales de alerta.

Ejecuta cada 60 segundos vía Celery Beat. Evalúa todas las
métricas del sistema contra los umbrales definidos y envía
alertas proactivas vía Telegram cuando se superan.
"""

from app.core.celery_app import celery_app


@celery_app.task(
    name="monitoring.check_thresholds",
    queue="notifications",
    ignore_result=True,
    soft_time_limit=50,   # 50s soft limit
    time_limit=55,        # 55s hard limit (dejar margen para el ciclo de 60s)
)
def monitoring_check() -> None:
    """Evalúa todos los umbrales de alerta y envía notificaciones.

    Se ejecuta cada 60 segundos. Verifica CPU, RAM, disco,
    Docker, Celery, BD y API. Envía alertas vía Telegram
    con deduplicación para evitar spam.
    """
    import asyncio

    asyncio.get_event_loop().run_until_complete(
        _run_monitoring_check()
    )


async def _run_monitoring_check() -> None:
    """Implementación async de la verificación de umbrales."""
    import logging
    from app.services.monitoring.alert_manager import AlertManager

    logger = logging.getLogger(__name__)

    try:
        manager = AlertManager()
        # El bot se inyecta desde el servicio singleton
        from app.services.monitoring.bot_singleton import get_bot_instance
        bot = get_bot_instance()
        if bot:
            manager.set_bot(bot)

        triggered = await manager.check_all_thresholds()

        if triggered:
            logger.info(
                "Alertas disparadas: %s",
                [t["name"] for t in triggered],
            )
    except Exception:
        logger.exception("Error en monitoring_check")
```

### 2.8 Configuración Celery Beat (agregar)

```python
# Agregar a app/core/celery_config.py -> beat_schedule

beat_schedule = {
    # ... tareas existentes ...

    "monitoring-check-thresholds": {
        "task": "monitoring.check_thresholds",
        "schedule": 60.0,  # Cada 60 segundos
        "options": {"queue": "notifications"},
    },
}
```

### 2.9 Servicio Docker Compose

```yaml
# Agregar a docker-compose.yml

  telegram-bot:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: telegram-bot
    command: python -m app.services.monitoring.bot_entrypoint
    restart: unless-stopped
    env_file:
      - .env
    environment:
      - SERVICE_NAME=telegram-bot
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - backend
    depends_on:
      - redis
      - db
    deploy:
      resources:
        limits:
          cpus: "0.25"
          memory: 256M
        reservations:
          cpus: "0.1"
          memory: 128M
    healthcheck:
      test: ["CMD", "python", "-c", "import requests; requests.get('http://localhost:8081/health')"]
      interval: 30s
      timeout: 10s
      retries: 3
```

### 2.10 Entrypoint del bot

```python
# app/services/monitoring/bot_entrypoint.py

"""Punto de entrada para el servicio telegram-bot en Docker.

Inicializa y ejecuta el bot de Telegram como proceso principal
del contenedor. Incluye health check HTTP simple.
"""

import asyncio
import logging
import signal
import sys
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from aiohttp import web

from app.services.monitoring.telegram_bot import TelegramMonitorBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def health_handler(request: web.Request) -> web.Response:
    """Handler HTTP para health check.

    Args:
        request: Request HTTP entrante.

    Returns:
        Response con status 200 y body "ok".
    """
    return web.Response(text="ok")


async def main() -> None:
    """Función principal que inicia el bot y el health check HTTP."""
    bot = TelegramMonitorBot()

    # Registrar singleton para acceso desde tareas Celery
    from app.services.monitoring import bot_singleton
    bot_singleton.set_bot_instance(bot)

    # Health check HTTP
    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8081)
    await site.start()

    # Iniciar bot
    await bot.start()
    logger.info("Telegram bot iniciado correctamente")

    # Mantener proceso vivo
    stop_event = asyncio.Event()

    def signal_handler() -> None:
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    await stop_event.wait()

    # Cleanup
    logger.info("Deteniendo bot de Telegram...")
    await bot.stop()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
```

### 2.11 Singleton del bot

```python
# app/services/monitoring/bot_singleton.py

"""Singleton para acceso global a la instancia del bot de Telegram.

Permite que las tareas Celery accedan al bot para enviar alertas
sin necesidad de crear una nueva instancia.
"""

from typing import Any

_bot_instance: Any = None


def set_bot_instance(bot: Any) -> None:
    """Establece la instancia global del bot.

    Args:
        bot: Instancia de TelegramMonitorBot.
    """
    global _bot_instance
    _bot_instance = bot


def get_bot_instance() -> Any:
    """Obtiene la instancia global del bot.

    Returns:
        Instancia de TelegramMonitorBot o None si no está inicializada.
    """
    return _bot_instance
```

### 2.12 Variables de entorno

```bash
# Agregar a .env

# --- Telegram Bot ---
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
TELEGRAM_ADMIN_CHAT_IDS=123456789,987654321
```

### 2.13 Tests

```python
# tests/unit/monitoring/test_telegram_bot.py

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.monitoring.telegram_bot import TelegramMonitorBot


@pytest.fixture
def bot() -> TelegramMonitorBot:
    """Fixture que crea un bot con configuración de prueba."""
    with patch.object(
        TelegramMonitorBot,
        "__init__",
        lambda self: None,
    ):
        b = TelegramMonitorBot()
        b.token = "test-token"
        b.allowed_chat_ids = [123456789]
        b.monitoring = MagicMock()
        b.app = MagicMock()
        return b


class TestTelegramMonitorBot:
    """Tests para TelegramMonitorBot."""

    def test_is_authorized_with_valid_chat_id(
        self, bot: TelegramMonitorBot,
    ) -> None:
        """Verifica autorización con chat_id válido."""
        update = MagicMock()
        update.effective_chat.id = 123456789
        assert bot._is_authorized(update) is True

    def test_is_authorized_rejects_unknown_chat_id(
        self, bot: TelegramMonitorBot,
    ) -> None:
        """Verifica rechazo de chat_id no autorizado."""
        update = MagicMock()
        update.effective_chat.id = 999999999
        assert bot._is_authorized(update) is False

    @pytest.mark.asyncio
    async def test_send_alert_sends_to_all_chat_ids(
        self, bot: TelegramMonitorBot,
    ) -> None:
        """Verifica que send_alert envía a todos los chats."""
        bot.app.bot.send_message = AsyncMock()
        bot.allowed_chat_ids = [111, 222]

        await bot.send_alert("Test alert")

        assert bot.app.bot.send_message.call_count == 2


# tests/unit/monitoring/test_alert_manager.py

class TestAlertManager:
    """Tests para AlertManager."""

    @pytest.mark.asyncio
    async def test_deduplication_prevents_repeated_alerts(self) -> None:
        """Verifica que la deduplicación previene alertas repetidas."""
        from app.services.monitoring.alert_manager import AlertManager

        manager = AlertManager()
        mock_redis = AsyncMock()
        mock_bot = AsyncMock()

        with patch(
            "app.services.monitoring.alert_manager.get_redis",
            return_value=mock_redis,
        ):
            manager.set_bot(mock_bot)

            # Primera alerta: debe enviarse
            mock_redis.exists.return_value = False
            sent = await manager._send_alert_if_not_duplicate(
                "test_alert", "Test", "warning", 900,
            )
            assert sent is True

            # Segunda alerta: deduplicada
            mock_redis.exists.return_value = True
            sent = await manager._send_alert_if_not_duplicate(
                "test_alert", "Test", "warning", 900,
            )
            assert sent is False

    @pytest.mark.asyncio
    async def test_resolve_alert_moves_to_resolved(self) -> None:
        """Verifica que resolver alerta la mueve correctamente."""
        from app.services.monitoring.alert_manager import AlertManager

        manager = AlertManager()
        mock_redis = AsyncMock()
        mock_redis.hgetall.return_value = {
            b"message": b"Test alert",
            b"since": b"2026-09-05 10:00:00 UTC",
            b"severity": b"warning",
        }

        with patch(
            "app.services.monitoring.alert_manager.get_redis",
            return_value=mock_redis,
        ):
            await manager._resolve_alert("test_alert")

            mock_redis.delete.assert_called_once()
            mock_redis.expire.assert_called_once()
```

---

## Feature 3: Backup Avanzado y Replicación

### 3.1 Descripción

Ampliación de la estrategia de backup existente (Sprint 8) con verificación post-backup, replicación por streaming a VPS secundario, política de retención granular (diario/semanal/mensual) y pruebas automatizadas de restauración.

### 3.2 Arquitectura de Backup y Replicación

```
    +------------------+          Streaming Replication         +------------------+
    |  Primary VPS     | ======================================>|  Replica VPS     |
    |  PostgreSQL      |                                       |  PostgreSQL      |
    |  (lectura/escr.) |                                       |  (solo lectura)  |
    +--------+---------+                                       +------------------+
             |
             | pg_dump (02:00 UTC diario)
             v
    +--------+---------+
    |  Backup local    |
    |  /backups/       |
    +--------+---------+
             |
       +-----+-----+
       |           |
       v           v
    +--+---+   +---+---+
    | S3 / |   | VPS   |
    |MinIO |   | Sec.  |
    +------+   +-------+
```

### 3.3 Script de backup mejorado

```bash
#!/usr/bin/env bash
# scripts/backup.sh
# Backup completo de PostgreSQL con verificación y upload a S3 + VPS secundario.
#
# Uso: ./scripts/backup.sh [--verify] [--upload-s3] [--upload-vps]
# Opciones:
#   --verify     Verificar el backup restaurándolo en una BD temporal
#   --upload-s3  Subir a S3/MinIO
#   --upload-vps Copiar a VPS secundario
#
# Variables de entorno requeridas:
#   DB_HOST, DB_PORT, DB_NAME, DB_USER, PGPASSWORD
#   S3_BUCKET, S3_ENDPOINT (para --upload-s3)
#   REPLICA_HOST, REPLICA_SSH_USER (para --upload-vps)

set -euo pipefail

# --- Configuración ---
BACKUP_DIR="/backups"
DATE=$(date +%Y%m%d_%H%M%S)
DAY_OF_WEEK=$(date +%u)   # 1=lunes, 7=domingo
DAY_OF_MONTH=$(date +%d)
BACKUP_FILE="${BACKUP_DIR}/backup_${DATE}.sql.gz"
VERIFY_DB="backup_verify_${DATE}"
LOG_FILE="${BACKUP_DIR}/logs/backup_${DATE}.log"

VERIFY=false
UPLOAD_S3=false
UPLOAD_VPS=false

# Parsear argumentos
for arg in "$@"; do
    case $arg in
        --verify) VERIFY=true ;;
        --upload-s3) UPLOAD_S3=true ;;
        --upload-vps) UPLOAD_VPS=true ;;
    esac
done

# --- Funciones ---

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

send_telegram_alert() {
    local message="$1"
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_ADMIN_CHAT_ID:-}" ]; then
        curl -s -X POST \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_ADMIN_CHAT_ID}" \
            -d "text=${message}" \
            -d "parse_mode=HTML" > /dev/null 2>&1 || true
    fi
}

log_to_db() {
    local success="$1"
    local size_bytes="$2"
    local duration_seconds="$3"
    local destination="$4"
    local size_human
    size_human=$(numfmt --to=iec-i --suffix=B "$size_bytes" 2>/dev/null || echo "${size_bytes} bytes")
    local duration_human
    duration_human=$(printf '%dm %ds' $((duration_seconds / 60)) $((duration_seconds % 60)))

    PGPASSWORD="${DB_PASSWORD}" psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -c "
        INSERT INTO backup_logs (success, file_path, size_bytes, size_human, duration_seconds, duration_human, destination, backup_type, created_at)
        VALUES ($success, '${BACKUP_FILE}', ${size_bytes}, '${size_human}', ${duration_seconds}, '${duration_human}', '${destination}', '$(get_backup_type)', NOW());
    " >> "$LOG_FILE" 2>&1
}

get_backup_type() {
    if [ "$DAY_OF_MONTH" == "01" ]; then
        echo "monthly"
    elif [ "$DAY_OF_WEEK" == "7" ]; then
        echo "weekly"
    else
        echo "daily"
    fi
}

# --- Pre-checks ---

mkdir -p "${BACKUP_DIR}/logs"

log "=== Inicio de backup ==="
log "Tipo de backup: $(get_backup_type)"
START_TIME=$(date +%s)

# Verificar conectividad a BD
log "Verificando conexión a base de datos..."
if ! PGPASSWORD="${DB_PASSWORD}" pg_isready -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" > /dev/null 2>&1; then
    log "ERROR: No se puede conectar a la base de datos"
    send_telegram_alert "<b>[CRITICO] Backup Fallido</b>%0A%0ANo se puede conectar a la base de datos."
    log_to_db "false" "0" "0" "ninguno"
    exit 1
fi

# Verificar espacio en disco (mínimo 10GB libres)
AVAILABLE_SPACE=$(df -BG "${BACKUP_DIR}" | awk 'NR==2 {print $4}' | tr -d 'G')
if [ "${AVAILABLE_SPACE}" -lt 10 ]; then
    log "ERROR: Espacio insuficiente en disco. Disponible: ${AVAILABLE_SPACE}GB"
    send_telegram_alert "<b>[CRITICO] Backup Fallido</b>%0A%0AEspacio insuficiente: ${AVAILABLE_SPACE}GB disponibles."
    log_to_db "false" "0" "0" "ninguno"
    exit 1
fi
log "Espacio disponible: ${AVAILABLE_SPACE}GB"

# --- Ejecutar pg_dump ---

log "Ejecutando pg_dump con compresión gzip nivel 9..."
PGPASSWORD="${DB_PASSWORD}" pg_dump \
    -h "${DB_HOST}" \
    -p "${DB_PORT}" \
    -U "${DB_USER}" \
    -d "${DB_NAME}" \
    --format=custom \
    --compress=9 \
    --verbose \
    --no-owner \
    --no-privileges \
    --exclude-table-data='celery_task_logs' \
    2>> "$LOG_FILE" | gzip -9 > "${BACKUP_FILE}"

BACKUP_STATUS=$?
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

if [ ${BACKUP_STATUS} -ne 0 ]; then
    log "ERROR: pg_dump falló con código ${BACKUP_STATUS}"
    send_telegram_alert "<b>[CRITICO] Backup Fallido</b>%0A%0Apg_dump falló con código ${BACKUP_STATUS}."
    log_to_db "false" "0" "${DURATION}" "ninguno"
    exit 1
fi

BACKUP_SIZE=$(stat -c%s "${BACKUP_FILE}")
BACKUP_SIZE_HUMAN=$(numfmt --to=iec-i --suffix=B "${BACKUP_SIZE}")
log "Backup completado: ${BACKUP_SIZE_HUMAN} en ${DURATION}s"

# --- Checksum ---

log "Generando checksum SHA256..."
sha256sum "${BACKUP_FILE}" > "${BACKUP_FILE}.sha256"
log "Checksum: $(cat "${BACKUP_FILE}.sha256")"

# --- Verificación (si --verify) ---

if [ "${VERIFY}" = true ]; then
    log "Iniciando verificación de backup..."

    # Crear BD temporal
    PGPASSWORD="${DB_PASSWORD}" createdb \
        -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" \
        "${VERIFY_DB}" >> "$LOG_FILE" 2>&1

    # Restaurar
    gunzip -c "${BACKUP_FILE}" | \
        PGPASSWORD="${DB_PASSWORD}" pg_restore \
            -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" \
            -d "${VERIFY_DB}" \
            --no-owner --no-privileges \
            --verbose 2>> "$LOG_FILE"

    RESTORE_STATUS=$?

    if [ ${RESTORE_STATUS} -eq 0 ]; then
        log "Verificación exitosa: backup restaurado correctamente"

        # Verificar integridad básica
        TABLE_COUNT=$(PGPASSWORD="${DB_PASSWORD}" psql \
            -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" \
            -d "${VERIFY_DB}" -t -c \
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
        log "Tablas en backup verificado: ${TABLE_COUNT}"
    else
        log "ADVERTENCIA: La verificación del backup falló"
        send_telegram_alert "<b>[ADVERTENCIA] Verificación de Backup Fallida</b>%0A%0AEl backup se creó pero no pasó la verificación."
    fi

    # Eliminar BD temporal
    PGPASSWORD="${DB_PASSWORD}" dropdb \
        -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" \
        "${VERIFY_DB}" >> "$LOG_FILE" 2>&1 || true

    log "BD temporal de verificación eliminada"
fi

# --- Upload a S3/MinIO (si --upload-s3) ---

DESTINATION="local"

if [ "${UPLOAD_S3}" = true ]; then
    log "Subiendo backup a S3..."
    S3_PATH="s3://${S3_BUCKET}/backups/$(get_backup_type)/${DATE}/"

    aws s3 cp "${BACKUP_FILE}" "${S3_PATH}" \
        --endpoint-url "${S3_ENDPOINT}" \
        --storage-class STANDARD_IA \
        2>> "$LOG_FILE"

    aws s3 cp "${BACKUP_FILE}.sha256" "${S3_PATH}" \
        --endpoint-url "${S3_ENDPOINT}" \
        2>> "$LOG_FILE"

    if [ $? -eq 0 ]; then
        log "Upload a S3 completado: ${S3_PATH}"
        DESTINATION="s3+local"
    else
        log "ERROR: Upload a S3 falló"
        send_telegram_alert "<b>[ADVERTENCIA]</b> Upload de backup a S3 falló."
    fi
fi

# --- Copiar a VPS secundario (si --upload-vps) ---

if [ "${UPLOAD_VPS}" = true ] && [ -n "${REPLICA_HOST:-}" ]; then
    log "Copiando backup a VPS secundario (${REPLICA_HOST})..."
    REMOTE_DIR="/backups/$(get_backup_type)"

    ssh "${REPLICA_SSH_USER}@${REPLICA_HOST}" "mkdir -p ${REMOTE_DIR}" 2>> "$LOG_FILE"
    rsync -avz --progress \
        "${BACKUP_FILE}" "${BACKUP_FILE}.sha256" \
        "${REPLICA_SSH_USER}@${REPLICA_HOST}:${REMOTE_DIR}/" \
        2>> "$LOG_FILE"

    if [ $? -eq 0 ]; then
        log "Copia a VPS secundario completada"
        DESTINATION="${DESTINATION}+vps"
    else
        log "ERROR: Copia a VPS secundario falló"
        send_telegram_alert "<b>[ADVERTENCIA]</b> Copia de backup a VPS secundario falló."
    fi
fi

# --- Registrar en BD ---

log_to_db "true" "${BACKUP_SIZE}" "${DURATION}" "${DESTINATION}"

# --- Resumen ---

log "=== Backup completado ==="
log "Archivo: ${BACKUP_FILE}"
log "Tamaño: ${BACKUP_SIZE_HUMAN}"
log "Duración: ${DURATION}s"
log "Destino: ${DESTINATION}"
log "Tipo: $(get_backup_type)"

send_telegram_alert "<b>[OK] Backup Completado</b>%0A%0ATipo: $(get_backup_type)%0ATamaño: ${BACKUP_SIZE_HUMAN}%0ADuración: ${DURATION}s%0ADestino: ${DESTINATION}"
```

### 3.4 Script de configuración de replicación

```bash
#!/usr/bin/env bash
# scripts/setup_replication.sh
# Configura replicación por streaming de PostgreSQL entre primary y replica.
#
# Ejecutar en el servidor PRIMARY.
#
# Variables de entorno requeridas:
#   REPLICA_HOST        - IP/hostname del servidor réplica
#   REPLICA_PORT        - Puerto PostgreSQL en la réplica (default: 5432)
#   REPLICA_USER        - Usuario de replicación (default: replicator)
#   REPLICA_PASSWORD    - Contraseña del usuario de replicación
#   REPLICA_SSH_USER    - Usuario SSH para acceder a la réplica
#   PG_DATA_DIR         - Directorio de datos de PostgreSQL (default: /var/lib/postgresql/16/main)

set -euo pipefail

REPLICA_HOST="${REPLICA_HOST}"
REPLICA_PORT="${REPLICA_PORT:-5432}"
REPLICA_USER="${REPLICA_USER:-replicator}"
REPLICA_PASSWORD="${REPLICA_PASSWORD}"
REPLICA_SSH_USER="${REPLICA_SSH_USER:-root}"
PG_DATA_DIR="${PG_DATA_DIR:-/var/lib/postgresql/16/main}"
PG_CONF="${PG_DATA_DIR}/postgresql.conf"
PG_HBA="${PG_DATA_DIR}/pg_hba.conf"
SLOT_NAME="replica_slot_1"

echo "=== Configuración de Replicación PostgreSQL ==="
echo "Primary: localhost"
echo "Replica: ${REPLICA_HOST}:${REPLICA_PORT}"
echo "Usuario de replicación: ${REPLICA_USER}"
echo ""

# --- Paso 1: Crear usuario de replicación en primary ---

echo "[1/6] Creando usuario de replicación..."
sudo -u postgres psql -c "
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${REPLICA_USER}') THEN
            CREATE ROLE ${REPLICA_USER} WITH REPLICATION LOGIN PASSWORD '${REPLICA_PASSWORD}';
            RAISE NOTICE 'Usuario de replicación creado';
        ELSE
            ALTER ROLE ${REPLICA_USER} WITH PASSWORD '${REPLICA_PASSWORD}';
            RAISE NOTICE 'Contraseña del usuario de replicación actualizada';
        END IF;
    END
    \$\$;
"

# --- Paso 2: Configurar postgresql.conf en primary ---

echo "[2/6] Configurando postgresql.conf en primary..."

# Backup de configuración actual
cp "${PG_CONF}" "${PG_CONF}.bak.$(date +%Y%m%d)"

# Parámetros de replicación
cat >> "${PG_CONF}" << PGCONF

# === Replicación (configurado por setup_replication.sh) ===
wal_level = replica
max_wal_senders = 5
max_replication_slots = 5
wal_keep_size = 1024      # 1GB de WAL retenido
synchronous_commit = on
archive_mode = on
archive_command = 'cp %p /backups/wal_archive/%f'
hot_standby = on
PGCONF

# --- Paso 3: Configurar pg_hba.conf ---

echo "[3/6] Configurando pg_hba.conf..."
cp "${PG_HBA}" "${PG_HBA}.bak.$(date +%Y%m%d)"

# Agregar regla de replicación
echo "host    replication     ${REPLICA_USER}    ${REPLICA_HOST}/32    scram-sha-256" >> "${PG_HBA}"

# --- Paso 4: Crear slot de replicación ---

echo "[4/6] Creando slot de replicación..."
sudo -u postgres psql -c "
    SELECT CASE
        WHEN EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name = '${SLOT_NAME}')
        THEN 'Slot ya existe'
        ELSE (SELECT pg_create_physical_replication_slot('${SLOT_NAME}'))::text
    END;
"

# Crear directorio de WAL archive
mkdir -p /backups/wal_archive
chown postgres:postgres /backups/wal_archive

# --- Paso 5: Reiniciar primary ---

echo "[5/6] Reiniciando PostgreSQL primary..."
systemctl restart postgresql

echo "Esperando que PostgreSQL esté listo..."
sleep 5
until pg_isready; do
    echo "  Esperando..."
    sleep 2
done
echo "PostgreSQL primary listo."

# --- Paso 6: Configurar réplica (vía SSH) ---

echo "[6/6] Configurando servidor réplica vía SSH..."

ssh "${REPLICA_SSH_USER}@${REPLICA_HOST}" << REMOTE_SCRIPT
set -euo pipefail

echo "  Deteniendo PostgreSQL en réplica..."
systemctl stop postgresql || true

echo "  Limpiando directorio de datos..."
rm -rf ${PG_DATA_DIR}/*

echo "  Ejecutando pg_basebackup..."
PGPASSWORD="${REPLICA_PASSWORD}" pg_basebackup \
    -h $(hostname -I | awk '{print $1}') \
    -p 5432 \
    -U ${REPLICA_USER} \
    -D ${PG_DATA_DIR} \
    -Fp -Xs -P -R \
    --slot=${SLOT_NAME}

echo "  Configurando standby..."
cat >> ${PG_DATA_DIR}/postgresql.conf << STANDBY_CONF

# === Standby (configurado por setup_replication.sh) ===
primary_conninfo = 'host=$(hostname -I | awk '{print $1}') port=5432 user=${REPLICA_USER} password=${REPLICA_PASSWORD}'
primary_slot_name = '${SLOT_NAME}'
hot_standby = on
hot_standby_feedback = on
STANDBY_CONF

echo "  Creando standby.signal..."
touch ${PG_DATA_DIR}/standby.signal

echo "  Ajustando permisos..."
chown -R postgres:postgres ${PG_DATA_DIR}

echo "  Iniciando PostgreSQL réplica..."
systemctl start postgresql

echo "  Esperando que PostgreSQL réplica esté listo..."
sleep 5
until pg_isready; do
    echo "    Esperando..."
    sleep 2
done
echo "  PostgreSQL réplica listo."
REMOTE_SCRIPT

# --- Verificación ---

echo ""
echo "=== Verificando replicación ==="
echo ""

echo "Estado de replicación en primary:"
sudo -u postgres psql -c "SELECT * FROM pg_stat_replication;"

echo ""
echo "Slots de replicación:"
sudo -u postgres psql -c "SELECT * FROM pg_replication_slots;"

echo ""
echo "=== Configuración de replicación completada ==="
echo ""
echo "Comandos útiles:"
echo "  - Ver lag de replicación: SELECT replay_lag FROM pg_stat_replication;"
echo "  - Promover réplica: ssh ${REPLICA_SSH_USER}@${REPLICA_HOST} 'sudo -u postgres pg_ctl promote -D ${PG_DATA_DIR}'"
echo "  - Ver estado del slot: SELECT * FROM pg_replication_slots WHERE slot_name = '${SLOT_NAME}';"
```

### 3.5 Script de retención de backups

```bash
#!/usr/bin/env bash
# scripts/backup_retention.sh
# Elimina backups antiguos según la política de retención:
#   - Diarios: 7 días
#   - Semanales (domingo): 4 semanas
#   - Mensuales (día 1): 12 meses
#
# Ejecutar diariamente vía cron DESPUÉS del backup.
# Aplica tanto a backups locales como a S3/MinIO.

set -euo pipefail

BACKUP_DIR="/backups"
DAILY_RETENTION=7       # días
WEEKLY_RETENTION=28     # días (4 semanas)
MONTHLY_RETENTION=365   # días (12 meses)

LOG_FILE="${BACKUP_DIR}/logs/retention_$(date +%Y%m%d).log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "=== Inicio de limpieza de retención ==="

DELETED_COUNT=0
FREED_BYTES=0

# --- Retención local ---

# Diarios: eliminar mayores a 7 días
log "Eliminando backups diarios mayores a ${DAILY_RETENTION} días..."
while IFS= read -r file; do
    if [ -f "$file" ]; then
        size=$(stat -c%s "$file")
        log "  Eliminando: $(basename "$file") ($(numfmt --to=iec-i --suffix=B "$size"))"
        rm -f "$file" "${file}.sha256"
        DELETED_COUNT=$((DELETED_COUNT + 1))
        FREED_BYTES=$((FREED_BYTES + size))
    fi
done < <(find "${BACKUP_DIR}" -name "backup_*.sql.gz" -mtime +${DAILY_RETENTION} -not -name "*_weekly_*" -not -name "*_monthly_*" 2>/dev/null)

# Semanales: eliminar mayores a 4 semanas
log "Eliminando backups semanales mayores a ${WEEKLY_RETENTION} días..."
while IFS= read -r file; do
    if [ -f "$file" ]; then
        size=$(stat -c%s "$file")
        log "  Eliminando: $(basename "$file") ($(numfmt --to=iec-i --suffix=B "$size"))"
        rm -f "$file" "${file}.sha256"
        DELETED_COUNT=$((DELETED_COUNT + 1))
        FREED_BYTES=$((FREED_BYTES + size))
    fi
done < <(find "${BACKUP_DIR}" -name "*_weekly_*.sql.gz" -mtime +${WEEKLY_RETENTION} 2>/dev/null)

# Mensuales: eliminar mayores a 12 meses
log "Eliminando backups mensuales mayores a ${MONTHLY_RETENTION} días..."
while IFS= read -r file; do
    if [ -f "$file" ]; then
        size=$(stat -c%s "$file")
        log "  Eliminando: $(basename "$file") ($(numfmt --to=iec-i --suffix=B "$size"))"
        rm -f "$file" "${file}.sha256"
        DELETED_COUNT=$((DELETED_COUNT + 1))
        FREED_BYTES=$((FREED_BYTES + size))
    fi
done < <(find "${BACKUP_DIR}" -name "*_monthly_*.sql.gz" -mtime +${MONTHLY_RETENTION} 2>/dev/null)

# --- Retención en S3 ---

if [ -n "${S3_BUCKET:-}" ] && [ -n "${S3_ENDPOINT:-}" ]; then
    log "Limpiando backups en S3..."

    # Diarios en S3
    S3_DAILY_CUTOFF=$(date -d "-${DAILY_RETENTION} days" +%Y%m%d)
    aws s3 ls "s3://${S3_BUCKET}/backups/daily/" --endpoint-url "${S3_ENDPOINT}" 2>/dev/null | while read -r line; do
        file_date=$(echo "$line" | grep -oP '\d{8}' | head -1)
        if [ -n "$file_date" ] && [ "$file_date" -lt "$S3_DAILY_CUTOFF" ]; then
            file_name=$(echo "$line" | awk '{print $NF}')
            log "  S3: Eliminando daily/${file_name}"
            aws s3 rm "s3://${S3_BUCKET}/backups/daily/${file_name}" \
                --endpoint-url "${S3_ENDPOINT}" 2>> "$LOG_FILE"
            DELETED_COUNT=$((DELETED_COUNT + 1))
        fi
    done

    # Semanales en S3
    S3_WEEKLY_CUTOFF=$(date -d "-${WEEKLY_RETENTION} days" +%Y%m%d)
    aws s3 ls "s3://${S3_BUCKET}/backups/weekly/" --endpoint-url "${S3_ENDPOINT}" 2>/dev/null | while read -r line; do
        file_date=$(echo "$line" | grep -oP '\d{8}' | head -1)
        if [ -n "$file_date" ] && [ "$file_date" -lt "$S3_WEEKLY_CUTOFF" ]; then
            file_name=$(echo "$line" | awk '{print $NF}')
            log "  S3: Eliminando weekly/${file_name}"
            aws s3 rm "s3://${S3_BUCKET}/backups/weekly/${file_name}" \
                --endpoint-url "${S3_ENDPOINT}" 2>> "$LOG_FILE"
            DELETED_COUNT=$((DELETED_COUNT + 1))
        fi
    done

    # Mensuales en S3
    S3_MONTHLY_CUTOFF=$(date -d "-${MONTHLY_RETENTION} days" +%Y%m%d)
    aws s3 ls "s3://${S3_BUCKET}/backups/monthly/" --endpoint-url "${S3_ENDPOINT}" 2>/dev/null | while read -r line; do
        file_date=$(echo "$line" | grep -oP '\d{8}' | head -1)
        if [ -n "$file_date" ] && [ "$file_date" -lt "$S3_MONTHLY_CUTOFF" ]; then
            file_name=$(echo "$line" | awk '{print $NF}')
            log "  S3: Eliminando monthly/${file_name}"
            aws s3 rm "s3://${S3_BUCKET}/backups/monthly/${file_name}" \
                --endpoint-url "${S3_ENDPOINT}" 2>> "$LOG_FILE"
            DELETED_COUNT=$((DELETED_COUNT + 1))
        fi
    done
fi

# --- Limpiar logs de retención antiguos (> 30 días) ---

find "${BACKUP_DIR}/logs" -name "retention_*.log" -mtime +30 -delete 2>/dev/null || true
find "${BACKUP_DIR}/logs" -name "backup_*.log" -mtime +30 -delete 2>/dev/null || true

# --- Resumen ---

FREED_HUMAN=$(numfmt --to=iec-i --suffix=B "$FREED_BYTES" 2>/dev/null || echo "${FREED_BYTES} bytes")
log "=== Retención completada ==="
log "Archivos eliminados: ${DELETED_COUNT}"
log "Espacio liberado: ${FREED_HUMAN}"
```

### 3.6 Script de prueba de restauración

```bash
#!/usr/bin/env bash
# scripts/restore_test.sh
# Prueba automatizada de restauración de backup.
#
# - Crea un contenedor Docker temporal con PostgreSQL
# - Restaura el último backup disponible
# - Ejecuta verificaciones de integridad
# - Reporta resultado vía Telegram y log
#
# Diseñado para ejecutarse semanalmente (domingos 04:00 UTC) vía Celery Beat.

set -euo pipefail

BACKUP_DIR="/backups"
TEST_CONTAINER="pg_restore_test_$(date +%s)"
TEST_PORT=5433
PG_VERSION="16"
LOG_FILE="${BACKUP_DIR}/logs/restore_test_$(date +%Y%m%d).log"

# Tablas críticas y conteos mínimos esperados
declare -A CRITICAL_TABLES=(
    ["clients"]=1
    ["users"]=1
    ["conversations"]=0
    ["messages"]=0
    ["ai_agents"]=0
)

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

send_result() {
    local status="$1"
    local details="$2"
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_ADMIN_CHAT_ID:-}" ]; then
        curl -s -X POST \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_ADMIN_CHAT_ID}" \
            -d "text=${status}%0A%0A${details}" \
            -d "parse_mode=HTML" > /dev/null 2>&1 || true
    fi
}

cleanup() {
    log "Limpiando contenedor de prueba..."
    docker rm -f "${TEST_CONTAINER}" 2>/dev/null || true
}
trap cleanup EXIT

# --- Encontrar último backup ---

log "=== Inicio de prueba de restauración ==="

LATEST_BACKUP=$(ls -t "${BACKUP_DIR}"/backup_*.sql.gz 2>/dev/null | head -1)
if [ -z "${LATEST_BACKUP}" ]; then
    log "ERROR: No se encontraron backups"
    send_result "<b>[FALLO] Prueba de Restauración</b>" "No se encontraron archivos de backup."
    exit 1
fi

BACKUP_SIZE=$(stat -c%s "${LATEST_BACKUP}")
BACKUP_SIZE_HUMAN=$(numfmt --to=iec-i --suffix=B "${BACKUP_SIZE}")
log "Backup a restaurar: $(basename "${LATEST_BACKUP}") (${BACKUP_SIZE_HUMAN})"

# --- Verificar checksum ---

if [ -f "${LATEST_BACKUP}.sha256" ]; then
    log "Verificando checksum SHA256..."
    if sha256sum -c "${LATEST_BACKUP}.sha256" >> "$LOG_FILE" 2>&1; then
        log "Checksum verificado correctamente"
    else
        log "ERROR: Checksum no coincide"
        send_result "<b>[FALLO] Prueba de Restauración</b>" "El checksum SHA256 del backup no coincide."
        exit 1
    fi
fi

# --- Crear contenedor PostgreSQL temporal ---

log "Creando contenedor PostgreSQL temporal..."
docker run -d \
    --name "${TEST_CONTAINER}" \
    -e POSTGRES_USER=restore_test \
    -e POSTGRES_PASSWORD=restore_test_pwd \
    -e POSTGRES_DB=restore_test \
    -p "${TEST_PORT}:5432" \
    --tmpfs /var/lib/postgresql/data:rw,size=4g \
    "postgres:${PG_VERSION}-alpine"

# Esperar a que PostgreSQL esté listo
log "Esperando a que PostgreSQL esté listo..."
RETRIES=0
MAX_RETRIES=30
until docker exec "${TEST_CONTAINER}" pg_isready -U restore_test 2>/dev/null; do
    RETRIES=$((RETRIES + 1))
    if [ ${RETRIES} -ge ${MAX_RETRIES} ]; then
        log "ERROR: PostgreSQL no inició en tiempo esperado"
        send_result "<b>[FALLO] Prueba de Restauración</b>" "PostgreSQL temporal no inició."
        exit 1
    fi
    sleep 2
done
log "PostgreSQL temporal listo."

# --- Habilitar extensiones necesarias ---

log "Habilitando extensiones..."
PGPASSWORD=restore_test_pwd psql -h localhost -p "${TEST_PORT}" -U restore_test -d restore_test -c "
    CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
" >> "$LOG_FILE" 2>&1

# --- Restaurar backup ---

log "Restaurando backup..."
START_TIME=$(date +%s)

gunzip -c "${LATEST_BACKUP}" | \
    PGPASSWORD=restore_test_pwd pg_restore \
        -h localhost -p "${TEST_PORT}" -U restore_test \
        -d restore_test \
        --no-owner --no-privileges \
        --verbose 2>> "$LOG_FILE"

RESTORE_STATUS=$?
END_TIME=$(date +%s)
RESTORE_DURATION=$((END_TIME - START_TIME))

if [ ${RESTORE_STATUS} -ne 0 ] && [ ${RESTORE_STATUS} -ne 1 ]; then
    # pg_restore retorna 1 para warnings no fatales (ej: roles no existentes)
    log "ERROR: Restauración falló con código ${RESTORE_STATUS}"
    send_result "<b>[FALLO] Prueba de Restauración</b>" "pg_restore falló con código ${RESTORE_STATUS}."
    exit 1
fi

log "Restauración completada en ${RESTORE_DURATION}s"

# --- Verificaciones de integridad ---

log "Ejecutando verificaciones de integridad..."
CHECKS_PASSED=0
CHECKS_TOTAL=0
DETAILS=""

# 1. Contar tablas
TABLE_COUNT=$(PGPASSWORD=restore_test_pwd psql -h localhost -p "${TEST_PORT}" -U restore_test -d restore_test -t -c "
    SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';
")
TABLE_COUNT=$(echo "$TABLE_COUNT" | tr -d ' ')
CHECKS_TOTAL=$((CHECKS_TOTAL + 1))
if [ "${TABLE_COUNT}" -gt 0 ]; then
    log "  OK: ${TABLE_COUNT} tablas encontradas"
    CHECKS_PASSED=$((CHECKS_PASSED + 1))
    DETAILS="${DETAILS}Tablas: ${TABLE_COUNT}%0A"
else
    log "  FALLO: No se encontraron tablas"
    DETAILS="${DETAILS}[!] No se encontraron tablas%0A"
fi

# 2. Verificar tablas críticas y conteos
for table in "${!CRITICAL_TABLES[@]}"; do
    CHECKS_TOTAL=$((CHECKS_TOTAL + 1))
    min_count=${CRITICAL_TABLES[$table]}

    EXISTS=$(PGPASSWORD=restore_test_pwd psql -h localhost -p "${TEST_PORT}" -U restore_test -d restore_test -t -c "
        SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_schema = 'public' AND table_name = '${table}');
    ")
    EXISTS=$(echo "$EXISTS" | tr -d ' ')

    if [ "$EXISTS" = "t" ]; then
        ROW_COUNT=$(PGPASSWORD=restore_test_pwd psql -h localhost -p "${TEST_PORT}" -U restore_test -d restore_test -t -c "
            SELECT count(*) FROM ${table};
        ")
        ROW_COUNT=$(echo "$ROW_COUNT" | tr -d ' ')

        if [ "${ROW_COUNT}" -ge "${min_count}" ]; then
            log "  OK: ${table} tiene ${ROW_COUNT} filas (mín: ${min_count})"
            CHECKS_PASSED=$((CHECKS_PASSED + 1))
            DETAILS="${DETAILS}${table}: ${ROW_COUNT} filas%0A"
        else
            log "  FALLO: ${table} tiene ${ROW_COUNT} filas (mín: ${min_count})"
            DETAILS="${DETAILS}[!] ${table}: solo ${ROW_COUNT} filas%0A"
        fi
    else
        log "  FALLO: Tabla ${table} no existe en el backup"
        DETAILS="${DETAILS}[!] ${table}: no encontrada%0A"
    fi
done

# 3. Verificar RLS
CHECKS_TOTAL=$((CHECKS_TOTAL + 1))
RLS_ENABLED=$(PGPASSWORD=restore_test_pwd psql -h localhost -p "${TEST_PORT}" -U restore_test -d restore_test -t -c "
    SELECT count(*) FROM pg_tables
    WHERE schemaname = 'public' AND rowsecurity = true;
")
RLS_ENABLED=$(echo "$RLS_ENABLED" | tr -d ' ')
if [ "${RLS_ENABLED}" -gt 0 ]; then
    log "  OK: RLS habilitado en ${RLS_ENABLED} tablas"
    CHECKS_PASSED=$((CHECKS_PASSED + 1))
    DETAILS="${DETAILS}RLS: ${RLS_ENABLED} tablas protegidas%0A"
else
    log "  ADVERTENCIA: RLS no detectado en ninguna tabla"
    DETAILS="${DETAILS}[!] RLS: no detectado%0A"
fi

# 4. Verificar extensiones
CHECKS_TOTAL=$((CHECKS_TOTAL + 1))
EXTENSIONS=$(PGPASSWORD=restore_test_pwd psql -h localhost -p "${TEST_PORT}" -U restore_test -d restore_test -t -c "
    SELECT string_agg(extname, ', ') FROM pg_extension WHERE extname IN ('uuid-ossp', 'vector', 'pg_trgm');
")
EXTENSIONS=$(echo "$EXTENSIONS" | tr -d ' ')
if [ -n "$EXTENSIONS" ]; then
    log "  OK: Extensiones: ${EXTENSIONS}"
    CHECKS_PASSED=$((CHECKS_PASSED + 1))
    DETAILS="${DETAILS}Extensiones: ${EXTENSIONS}%0A"
else
    log "  ADVERTENCIA: Extensiones no encontradas"
    DETAILS="${DETAILS}[!] Extensiones: no verificadas%0A"
fi

# --- Resultado final ---

log ""
log "=== Resultado de la prueba ==="
log "Checks pasados: ${CHECKS_PASSED}/${CHECKS_TOTAL}"
log "Restauración: ${RESTORE_DURATION}s"

if [ "${CHECKS_PASSED}" -eq "${CHECKS_TOTAL}" ]; then
    log "RESULTADO: EXITOSO"
    send_result \
        "<b>[OK] Prueba de Restauración Exitosa</b>" \
        "Backup: $(basename "${LATEST_BACKUP}")%0ATamaño: ${BACKUP_SIZE_HUMAN}%0ARestauración: ${RESTORE_DURATION}s%0AChecks: ${CHECKS_PASSED}/${CHECKS_TOTAL}%0A%0A${DETAILS}"
else
    log "RESULTADO: PARCIAL (${CHECKS_PASSED}/${CHECKS_TOTAL})"
    send_result \
        "<b>[ADVERTENCIA] Prueba de Restauración Parcial</b>" \
        "Backup: $(basename "${LATEST_BACKUP}")%0ATamaño: ${BACKUP_SIZE_HUMAN}%0AChecks: ${CHECKS_PASSED}/${CHECKS_TOTAL}%0A%0A${DETAILS}"
fi
```

### 3.7 Tarea Celery Beat para prueba de restauración

```python
# app/tasks/backup_tasks.py

"""Tareas Celery para backup y restauración.

Incluye la tarea semanal de prueba de restauración y la
tarea diaria de limpieza de retención.
"""

import subprocess

from app.core.celery_app import celery_app


@celery_app.task(
    name="backup.test_restore",
    queue="bulk",
    ignore_result=False,
    soft_time_limit=1800,   # 30 min
    time_limit=2400,        # 40 min
)
def test_backup_restore() -> dict[str, str]:
    """Ejecuta prueba semanal de restauración de backup.

    Llama al script restore_test.sh que:
    1. Crea contenedor Docker temporal con PostgreSQL
    2. Restaura el último backup
    3. Verifica integridad (tablas, RLS, extensiones)
    4. Reporta resultado vía Telegram

    Returns:
        Diccionario con resultado de la prueba.
    """
    result = subprocess.run(
        ["/bin/bash", "/app/scripts/restore_test.sh"],
        capture_output=True,
        text=True,
        timeout=1800,
    )

    return {
        "exit_code": str(result.returncode),
        "stdout": result.stdout[-2000:],  # Últimas 2000 chars
        "stderr": result.stderr[-1000:],
    }


@celery_app.task(
    name="backup.cleanup_retention",
    queue="bulk",
    ignore_result=True,
)
def cleanup_backup_retention() -> None:
    """Ejecuta la limpieza de backups según política de retención.

    Llama al script backup_retention.sh que elimina backups
    antiguos según la política: 7d diarios, 4w semanales, 12m mensuales.
    """
    subprocess.run(
        ["/bin/bash", "/app/scripts/backup_retention.sh"],
        capture_output=True,
        text=True,
        timeout=600,
    )
```

### 3.8 Configuración Celery Beat (agregar)

```python
# Agregar a app/core/celery_config.py -> beat_schedule

from celery.schedules import crontab

beat_schedule = {
    # ... tareas existentes ...

    "test-backup-restore-weekly": {
        "task": "backup.test_restore",
        "schedule": crontab(
            hour=4, minute=0, day_of_week=0,  # Domingos 04:00 UTC
        ),
        "options": {"queue": "bulk"},
    },

    "backup-retention-cleanup-daily": {
        "task": "backup.cleanup_retention",
        "schedule": crontab(
            hour=3, minute=0,  # Diario a las 03:00 UTC (después del backup)
        ),
        "options": {"queue": "bulk"},
    },
}
```

### 3.9 Métricas Prometheus para replicación

```python
# Agregar a app/core/metrics/celery_metrics.py

from prometheus_client import Gauge

# --- Replicación PostgreSQL ---

postgresql_replication_lag_seconds = Gauge(
    "postgresql_replication_lag_seconds",
    "Lag de replicación en segundos entre primary y replica",
)

postgresql_replication_state = Gauge(
    "postgresql_replication_state",
    "Estado de la replicación (1=streaming, 0=desconectado)",
)

# --- Backups ---

backup_last_success_timestamp = Gauge(
    "backup_last_success_timestamp",
    "Timestamp del último backup exitoso (epoch seconds)",
)

backup_last_duration_seconds = Gauge(
    "backup_last_duration_seconds",
    "Duración del último backup en segundos",
)

backup_last_size_bytes = Gauge(
    "backup_last_size_bytes",
    "Tamaño del último backup en bytes",
)
```

### 3.10 Plan de Recuperación ante Desastres

**Archivo:** `docs/disaster-recovery.md`

**Contenido (esquema):**

#### Escenario 1: Corrupción de tabla individual

```
RTO: < 30 minutos | RPO: < 1 hora

Pasos:
1. Identificar la tabla corrupta
2. Extraer tabla del último backup:
   pg_restore --data-only --table=<tabla> backup.dump -d temp_db
3. Exportar datos de tabla temporal:
   pg_dump --data-only --table=<tabla> temp_db > tabla_data.sql
4. Restaurar en producción (dentro de transacción):
   BEGIN;
   TRUNCATE <tabla>;
   \i tabla_data.sql
   COMMIT;
5. Verificar integridad referencial
6. Verificar RLS activo en la tabla
```

#### Escenario 2: Pérdida completa de base de datos

```
RTO: < 1 hora | RPO: < 1 hora (con replicación) / < 24h (sin replicación)

Opción A - Con réplica activa:
1. Promover réplica a primary:
   pg_ctl promote -D /var/lib/postgresql/16/main
2. Actualizar DNS/configuración para apuntar a la réplica
3. Verificar que la aplicación se conecta correctamente
4. Configurar nueva réplica cuando el primary original se recupere

Opción B - Sin réplica (restaurar desde backup):
1. Instalar PostgreSQL en el servidor
2. Habilitar extensiones (uuid-ossp, vector, pg_trgm)
3. Restaurar desde el último backup:
   pg_restore -d production backup_latest.sql.gz
4. Verificar integridad (tablas, RLS, datos)
5. Reconectar la aplicación
```

#### Escenario 3: Fallo del servidor primary

```
RTO: < 15 minutos | RPO: < 1 minuto (streaming replication)

Pasos:
1. Verificar que la réplica está actualizada:
   SELECT replay_lag FROM pg_stat_replication; -- desde réplica
2. Promover réplica:
   sudo -u postgres pg_ctl promote -D $PGDATA
3. Actualizar configuración DNS/Traefik
4. Verificar servicios de aplicación
5. Notificar al equipo
6. Planificar recuperación del primary original
```

#### Escenario 4: Pérdida completa del datacenter

```
RTO: < 4 horas | RPO: < 24 horas

Pasos:
1. Provisionar nuevo servidor (manual o Terraform)
2. Instalar stack base (Docker, PostgreSQL, etc.)
3. Descargar último backup de S3:
   aws s3 cp s3://bucket/backups/latest/ ./backups/ --recursive
4. Verificar checksum SHA256
5. Restaurar base de datos desde backup
6. Deploy de la aplicación (docker-compose up -d)
7. Restaurar configuración de Cloudflare/DNS
8. Verificar todos los servicios
9. Configurar nueva réplica
10. Ejecutar prueba de integridad completa
```

### 3.11 Variables de entorno para replicación

```bash
# Agregar a .env

# --- Replicación PostgreSQL ---
REPLICA_HOST=replica.example.com
REPLICA_PORT=5432
REPLICA_USER=replicator
REPLICA_PASSWORD=strong_replication_password_here
REPLICA_SSH_USER=deploy
```

---

## Feature 4: Políticas de Seguridad

### 4.1 Descripción

Hardening integral del servidor, aplicación, red y contenedores Docker. Incluye integración con Cloudflare, segmentación de redes Docker, headers de seguridad avanzados y configuración de firewall.

### 4.2 Seguridad del servidor

#### 4.2.1 Script de configuración UFW

```bash
#!/usr/bin/env bash
# scripts/setup_ufw.sh
# Configura el firewall UFW con las reglas de seguridad.
#
# Ejecutar como root en el servidor de producción.

set -euo pipefail

SSH_PORT="${SSH_PORT:-2222}"

echo "=== Configuración de UFW ==="

# Resetear reglas
ufw --force reset

# Política por defecto: denegar entrante, permitir saliente
ufw default deny incoming
ufw default allow outgoing

# SSH (puerto personalizado)
ufw allow "${SSH_PORT}/tcp" comment "SSH"

# HTTP/HTTPS (Traefik)
ufw allow 80/tcp comment "HTTP"
ufw allow 443/tcp comment "HTTPS"

# Prometheus (solo desde red interna si aplica)
# ufw allow from 10.0.0.0/8 to any port 9090 proto tcp comment "Prometheus"

# Habilitar
ufw --force enable
ufw status verbose

echo "=== UFW configurado ==="
```

#### 4.2.2 Configuración de fail2ban para la API

```ini
# /etc/fail2ban/jail.d/api.conf

[api-auth]
enabled = true
port = 80,443
filter = api-auth
logpath = /var/log/traefik/access.log
maxretry = 10
findtime = 300
bantime = 3600
action = ufw[name=API-Auth, port="80,443", protocol=tcp]

[api-ratelimit]
enabled = true
port = 80,443
filter = api-ratelimit
logpath = /var/log/traefik/access.log
maxretry = 100
findtime = 60
bantime = 600
action = ufw[name=API-RateLimit, port="80,443", protocol=tcp]
```

```ini
# /etc/fail2ban/filter.d/api-auth.conf

[Definition]
failregex = ^.*"(POST|PUT) /api/v1/auth/login.*" (401|403) .*$
            ^.*"(POST|PUT) /api/v1/auth/.*" (401|403) .*$
ignoreregex =
```

```ini
# /etc/fail2ban/filter.d/api-ratelimit.conf

[Definition]
failregex = ^.*"(GET|POST|PUT|DELETE) /api/v1/.*" 429 .*$
ignoreregex =
```

#### 4.2.3 Configuración SSH hardening

```bash
# /etc/ssh/sshd_config.d/hardening.conf

Port 2222
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
AuthorizedKeysFile .ssh/authorized_keys
ChallengeResponseAuthentication no
UsePAM yes
X11Forwarding no
PrintMotd no
AcceptEnv LANG LC_*
MaxAuthTries 3
MaxSessions 3
ClientAliveInterval 300
ClientAliveCountMax 2
AllowGroups sshusers
LoginGraceTime 30
```

#### 4.2.4 Ansible Playbook (esquema)

```yaml
# infra/server-hardening.yml

---
- name: Hardening del servidor de producción
  hosts: production
  become: true
  vars:
    ssh_port: 2222
    allowed_ssh_users:
      - deploy
      - admin

  tasks:
    # --- SSH ---
    - name: Configurar SSH hardening
      copy:
        src: files/sshd_hardening.conf
        dest: /etc/ssh/sshd_config.d/hardening.conf
        mode: "0644"
      notify: restart sshd

    - name: Crear grupo sshusers
      group:
        name: sshusers
        state: present

    - name: Agregar usuarios al grupo sshusers
      user:
        name: "{{ item }}"
        groups: sshusers
        append: true
      loop: "{{ allowed_ssh_users }}"

    # --- Firewall ---
    - name: Instalar UFW
      apt:
        name: ufw
        state: present

    - name: Configurar UFW - política por defecto
      ufw:
        state: enabled
        policy: deny
        direction: incoming

    - name: Permitir SSH
      ufw:
        rule: allow
        port: "{{ ssh_port }}"
        proto: tcp

    - name: Permitir HTTP/HTTPS
      ufw:
        rule: allow
        port: "{{ item }}"
        proto: tcp
      loop:
        - "80"
        - "443"

    # --- fail2ban ---
    - name: Instalar fail2ban
      apt:
        name: fail2ban
        state: present

    - name: Copiar configuración fail2ban
      copy:
        src: "files/fail2ban/{{ item }}"
        dest: "/etc/fail2ban/{{ item }}"
        mode: "0644"
      loop:
        - jail.d/api.conf
        - filter.d/api-auth.conf
        - filter.d/api-ratelimit.conf
      notify: restart fail2ban

    # --- Actualizaciones automáticas ---
    - name: Instalar unattended-upgrades
      apt:
        name:
          - unattended-upgrades
          - apt-listchanges
        state: present

    - name: Configurar unattended-upgrades
      copy:
        content: |
          Unattended-Upgrade::Allowed-Origins {
              "${distro_id}:${distro_codename}-security";
          };
          Unattended-Upgrade::AutoFixInterruptedDpkg "true";
          Unattended-Upgrade::Remove-Unused-Dependencies "true";
          Unattended-Upgrade::Automatic-Reboot "false";
          Unattended-Upgrade::Mail "admin@example.com";
        dest: /etc/apt/apt.conf.d/50unattended-upgrades
        mode: "0644"

    # --- Sistema de archivos ---
    - name: Montar /tmp con noexec
      mount:
        path: /tmp
        src: tmpfs
        fstype: tmpfs
        opts: defaults,noexec,nosuid,nodev,size=2G
        state: mounted

    # --- Límites del sistema ---
    - name: Configurar límites de archivos abiertos
      pam_limits:
        domain: "*"
        limit_type: "{{ item.type }}"
        limit_item: nofile
        value: "{{ item.value }}"
      loop:
        - { type: soft, value: "65536" }
        - { type: hard, value: "65536" }

    # --- Sysctl hardening ---
    - name: Configurar parámetros de kernel
      sysctl:
        name: "{{ item.key }}"
        value: "{{ item.value }}"
        sysctl_set: true
        state: present
        reload: true
      loop:
        - { key: "net.ipv4.tcp_syncookies", value: "1" }
        - { key: "net.ipv4.conf.all.rp_filter", value: "1" }
        - { key: "net.ipv4.conf.default.rp_filter", value: "1" }
        - { key: "net.ipv4.conf.all.accept_redirects", value: "0" }
        - { key: "net.ipv4.conf.all.send_redirects", value: "0" }
        - { key: "net.ipv4.conf.all.accept_source_route", value: "0" }
        - { key: "net.ipv4.icmp_echo_ignore_broadcasts", value: "1" }
        - { key: "kernel.randomize_va_space", value: "2" }

  handlers:
    - name: restart sshd
      service:
        name: sshd
        state: restarted

    - name: restart fail2ban
      service:
        name: fail2ban
        state: restarted
```

### 4.3 Seguridad de la Aplicación

#### 4.3.1 Configuración de Traefik - Headers de seguridad

```yaml
# traefik/dynamic/security-headers.yml

http:
  middlewares:
    security-headers:
      headers:
        # --- Protección contra XSS ---
        browserXssFilter: true
        contentTypeNosniff: true

        # --- Control de frames ---
        frameDeny: true
        customFrameOptionsValue: "DENY"

        # --- HSTS ---
        stsIncludeSubdomains: true
        stsPreload: true
        stsSeconds: 31536000  # 1 año

        # --- Content Security Policy ---
        contentSecurityPolicy: >-
          default-src 'self';
          script-src 'self';
          style-src 'self' 'unsafe-inline';
          img-src 'self' data: https:;
          font-src 'self';
          connect-src 'self' wss:;
          frame-ancestors 'none';
          base-uri 'self';
          form-action 'self';
          upgrade-insecure-requests

        # --- Referrer Policy ---
        referrerPolicy: "strict-origin-when-cross-origin"

        # --- Permissions Policy ---
        permissionsPolicy: >-
          camera=(),
          microphone=(),
          geolocation=(),
          payment=(),
          usb=(),
          magnetometer=(),
          gyroscope=(),
          accelerometer=()

        # --- Custom Headers ---
        customResponseHeaders:
          X-Robots-Tag: "noindex, nofollow"
          X-Download-Options: "noopen"
          X-Permitted-Cross-Domain-Policies: "none"
          Cross-Origin-Embedder-Policy: "require-corp"
          Cross-Origin-Opener-Policy: "same-origin"
          Cross-Origin-Resource-Policy: "same-origin"

    # --- Rate limiting por tier ---
    rate-limit-free:
      rateLimit:
        average: 100
        period: 1m
        burst: 20
        sourceCriterion:
          requestHeaderName: "X-Client-ID"

    rate-limit-pro:
      rateLimit:
        average: 1000
        period: 1m
        burst: 200
        sourceCriterion:
          requestHeaderName: "X-Client-ID"

    rate-limit-enterprise:
      rateLimit:
        average: 10000
        period: 1m
        burst: 2000
        sourceCriterion:
          requestHeaderName: "X-Client-ID"

    # --- Cloudflare IP whitelist ---
    cloudflare-ips:
      ipAllowList:
        sourceRange:
          # IPv4
          - "173.245.48.0/20"
          - "103.21.244.0/22"
          - "103.22.200.0/22"
          - "103.31.4.0/22"
          - "141.101.64.0/18"
          - "108.162.192.0/18"
          - "190.93.240.0/20"
          - "188.114.96.0/20"
          - "197.234.240.0/22"
          - "198.41.128.0/17"
          - "162.158.0.0/15"
          - "104.16.0.0/13"
          - "104.24.0.0/14"
          - "172.64.0.0/13"
          - "131.0.72.0/22"
          # IPv6
          - "2400:cb00::/32"
          - "2606:4700::/32"
          - "2803:f800::/32"
          - "2405:b500::/32"
          - "2405:8100::/32"
          - "2a06:98c0::/29"
          - "2c0f:f248::/32"
        ipStrategy:
          depth: 0  # Usar IP directa (Cloudflare)
```

#### 4.3.2 Configuración CORS dinámica por tenant

```python
# app/core/cors.py

"""Configuración CORS dinámica por tenant.

Permite configurar orígenes permitidos por cada tenant
almacenados en clients.settings.allowed_origins.
"""

from fastapi import Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.core.config import settings


class DynamicCORSMiddleware(BaseHTTPMiddleware):
    """Middleware CORS dinámico por tenant.

    Consulta la configuración del tenant (client_id) para
    determinar los orígenes permitidos en cada request.

    Note:
        Este middleware se ejecuta ANTES de la autenticación.
        Usa el header X-Client-ID o el subdominio para
        identificar al tenant.

    Attributes:
        default_origins: Orígenes permitidos por defecto.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Inicializa el middleware.

        Args:
            app: Aplicación ASGI.
        """
        super().__init__(app)
        self.default_origins: list[str] = [
            settings.FRONTEND_URL,
        ]

    async def dispatch(
        self, request: Request, call_next: object,
    ) -> Response:
        """Procesa el request aplicando CORS dinámico.

        Args:
            request: Request HTTP entrante.
            call_next: Siguiente middleware/handler.

        Returns:
            Response con headers CORS apropiados.
        """
        origin = request.headers.get("origin", "")

        # Para preflight requests
        if request.method == "OPTIONS":
            allowed_origins = await self._get_allowed_origins(request)
            if origin in allowed_origins:
                response = Response(status_code=200)
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Methods"] = (
                    "GET, POST, PUT, PATCH, DELETE, OPTIONS"
                )
                response.headers["Access-Control-Allow-Headers"] = (
                    "Authorization, Content-Type, X-Client-ID, "
                    "X-Request-ID"
                )
                response.headers["Access-Control-Allow-Credentials"] = "true"
                response.headers["Access-Control-Max-Age"] = "86400"
                return response

        response = await call_next(request)

        if origin:
            allowed_origins = await self._get_allowed_origins(request)
            if origin in allowed_origins:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Credentials"] = "true"
                response.headers["Vary"] = "Origin"

        return response

    async def _get_allowed_origins(
        self, request: Request,
    ) -> list[str]:
        """Obtiene orígenes permitidos para el tenant del request.

        Busca el client_id en headers o subdominio, luego
        consulta la configuración del tenant en BD/cache.

        Args:
            request: Request HTTP.

        Returns:
            Lista de orígenes permitidos.
        """
        from app.core.redis_client import get_redis

        client_id = request.headers.get("x-client-id")
        if not client_id:
            return self.default_origins

        # Intentar cache Redis
        redis = await get_redis()
        cache_key = f"cors:origins:{client_id}"
        cached = await redis.get(cache_key)

        if cached:
            import json
            return json.loads(cached)

        # Consultar BD
        from sqlalchemy import text as sa_text
        from app.core.database import get_async_session

        async with get_async_session() as session:
            await session.execute(
                sa_text("SET LOCAL app.current_client_id = :cid"),
                {"cid": client_id},
            )
            result = await session.execute(
                sa_text(
                    "SELECT settings->'allowed_origins' "
                    "FROM clients WHERE id = :cid"
                ),
                {"cid": client_id},
            )
            row = result.scalar()

        if row:
            import json
            origins = json.loads(row) if isinstance(row, str) else row
            combined = list(set(self.default_origins + origins))
            # Cache por 5 minutos
            await redis.setex(cache_key, 300, json.dumps(combined))
            return combined

        return self.default_origins
```

#### 4.3.3 Middleware de rate limiting por tier de tenant

```python
# app/middleware/rate_limit.py

"""Middleware de rate limiting por tier de tenant.

Aplica límites de requests por minuto según el plan del tenant:
- Free: 100 req/min
- Pro: 1000 req/min
- Enterprise: 10000 req/min

Usa Redis como backend para contadores distribuidos.
"""

import time
from typing import Any

from fastapi import Request, Response, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp


TIER_LIMITS: dict[str, int] = {
    "free": 100,
    "pro": 1000,
    "enterprise": 10000,
}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware de rate limiting por tenant usando Redis.

    Implementa sliding window counter con Redis para limitar
    la tasa de requests por tenant según su tier de suscripción.

    Attributes:
        default_limit: Límite por defecto si no se identifica el tenant.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Inicializa el middleware.

        Args:
            app: Aplicación ASGI.
        """
        super().__init__(app)
        self.default_limit = TIER_LIMITS["free"]

    async def dispatch(
        self, request: Request, call_next: object,
    ) -> Response:
        """Procesa el request verificando el rate limit.

        Args:
            request: Request HTTP entrante.
            call_next: Siguiente middleware/handler.

        Returns:
            Response normal o 429 si se excede el límite.
        """
        # No limitar health checks
        if request.url.path in ("/health", "/metrics"):
            return await call_next(request)

        client_id = request.headers.get("x-client-id")
        if not client_id:
            return await call_next(request)

        limit = await self._get_tenant_limit(client_id)
        allowed = await self._check_rate_limit(client_id, limit)

        if not allowed:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "detail": "Rate limit excedido. Intente nuevamente en un momento.",
                    "retry_after": 60,
                },
                headers={
                    "Retry-After": "60",
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)

        # Agregar headers de rate limit informativos
        remaining = await self._get_remaining(client_id, limit)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)

        return response

    async def _get_tenant_limit(self, client_id: str) -> int:
        """Obtiene el límite de rate para el tenant.

        Args:
            client_id: UUID del tenant.

        Returns:
            Límite de requests por minuto según el tier.
        """
        from app.core.redis_client import get_redis

        redis = await get_redis()
        cache_key = f"tier:{client_id}"
        tier = await redis.get(cache_key)

        if tier:
            return TIER_LIMITS.get(tier.decode(), self.default_limit)

        # Consultar BD
        from sqlalchemy import text as sa_text
        from app.core.database import get_async_session

        async with get_async_session() as session:
            result = await session.execute(
                sa_text(
                    "SELECT subscription_tier FROM clients WHERE id = :cid"
                ),
                {"cid": client_id},
            )
            row = result.scalar()

        tier_name = row or "free"
        await redis.setex(cache_key, 300, tier_name)

        return TIER_LIMITS.get(tier_name, self.default_limit)

    async def _check_rate_limit(
        self, client_id: str, limit: int,
    ) -> bool:
        """Verifica si el tenant puede hacer otro request.

        Usa sliding window counter en Redis con ventana de 60 segundos.

        Args:
            client_id: UUID del tenant.
            limit: Límite de requests por minuto.

        Returns:
            True si está permitido, False si excede el límite.
        """
        from app.core.redis_client import get_redis

        redis = await get_redis()
        now = time.time()
        window_start = now - 60
        key = f"rl:{client_id}"

        pipe = redis.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        pipe.zadd(key, {str(now): now})
        pipe.expire(key, 120)
        results = await pipe.execute()

        current_count = results[1]
        return current_count < limit

    async def _get_remaining(
        self, client_id: str, limit: int,
    ) -> int:
        """Obtiene requests restantes en la ventana actual.

        Args:
            client_id: UUID del tenant.
            limit: Límite configurado.

        Returns:
            Número de requests restantes.
        """
        from app.core.redis_client import get_redis

        redis = await get_redis()
        now = time.time()
        key = f"rl:{client_id}"

        count = await redis.zcount(key, now - 60, now)
        return max(0, limit - count)
```

### 4.4 Integración con Cloudflare

#### 4.4.1 Servicio Docker para Cloudflare Tunnel

```yaml
# Agregar a docker-compose.yml

  cloudflare-tunnel:
    image: cloudflare/cloudflared:latest
    container_name: cloudflare-tunnel
    restart: unless-stopped
    command: tunnel --no-autoupdate run
    environment:
      - TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}
    networks:
      - frontend
    depends_on:
      - traefik
    deploy:
      resources:
        limits:
          cpus: "0.25"
          memory: 128M
```

#### 4.4.2 Configuración de Traefik para Cloudflare

```yaml
# traefik/traefik.yml (agregar a configuración existente)

entryPoints:
  web:
    address: ":80"
    http:
      redirections:
        entryPoint:
          to: websecure
          scheme: https
    forwardedHeaders:
      trustedIPs:
        # Cloudflare IPv4
        - "173.245.48.0/20"
        - "103.21.244.0/22"
        - "103.22.200.0/22"
        - "103.31.4.0/22"
        - "141.101.64.0/18"
        - "108.162.192.0/18"
        - "190.93.240.0/20"
        - "188.114.96.0/20"
        - "197.234.240.0/22"
        - "198.41.128.0/17"
        - "162.158.0.0/15"
        - "104.16.0.0/13"
        - "104.24.0.0/14"
        - "172.64.0.0/13"
        - "131.0.72.0/22"
        # Cloudflare IPv6
        - "2400:cb00::/32"
        - "2606:4700::/32"
        - "2803:f800::/32"
        - "2405:b500::/32"
        - "2405:8100::/32"
        - "2a06:98c0::/29"
        - "2c0f:f248::/32"

  websecure:
    address: ":443"
    http:
      tls:
        certResolver: cloudflare
      middlewares:
        - security-headers@file
    forwardedHeaders:
      trustedIPs:
        # Mismas IPs de Cloudflare que arriba
        - "173.245.48.0/20"
        - "103.21.244.0/22"
        # ... (lista completa)

certificatesResolvers:
  cloudflare:
    acme:
      email: admin@example.com
      storage: /etc/traefik/acme.json
      dnsChallenge:
        provider: cloudflare
        delayBeforeCheck: 30
        resolvers:
          - "1.1.1.1:53"
          - "8.8.8.8:53"
```

#### 4.4.3 Authenticated Origin Pulls (mTLS Cloudflare-Traefik)

```yaml
# traefik/dynamic/cloudflare-mtls.yml

tls:
  options:
    cloudflare-mtls:
      clientAuth:
        caFiles:
          - /etc/traefik/certs/cloudflare-origin-pull-ca.pem
        clientAuthType: RequireAndVerifyClientCert
      minVersion: VersionTLS12
      cipherSuites:
        - TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384
        - TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256
        - TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256
```

#### 4.4.4 Variables de entorno para Cloudflare

```bash
# Agregar a .env

# --- Cloudflare ---
CLOUDFLARE_API_TOKEN=your_cloudflare_api_token_here
CLOUDFLARE_ZONE_ID=your_zone_id_here
CLOUDFLARE_TUNNEL_TOKEN=your_tunnel_token_here
CLOUDFLARE_EMAIL=admin@example.com
```

### 4.5 Seguridad Docker

#### 4.5.1 Docker Compose con hardening completo

```yaml
# docker-compose.yml (secciones relevantes con hardening)

version: "3.9"

networks:
  frontend:
    name: frontend
    driver: bridge
    internal: false  # Acceso externo (Traefik)
  backend:
    name: backend
    driver: bridge
    internal: true   # Solo comunicación entre servicios
  db:
    name: db_network
    driver: bridge
    internal: true   # Solo PostgreSQL y servicios que lo necesitan

services:
  traefik:
    image: traefik:v3.1
    container_name: traefik
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    read_only: true
    tmpfs:
      - /tmp:size=10M
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./traefik/traefik.yml:/etc/traefik/traefik.yml:ro
      - ./traefik/dynamic:/etc/traefik/dynamic:ro
      - ./traefik/certs:/etc/traefik/certs:ro
      - traefik-acme:/etc/traefik/acme
      - /var/log/traefik:/var/log/traefik
    networks:
      - frontend
    deploy:
      resources:
        limits:
          cpus: "0.5"
          memory: 256M
        reservations:
          cpus: "0.1"
          memory: 64M

  api:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: api
    restart: unless-stopped
    user: "1000:1000"
    security_opt:
      - no-new-privileges:true
    read_only: true
    tmpfs:
      - /tmp:size=100M
      - /app/.cache:size=50M
    env_file:
      - .env
    networks:
      - frontend
      - backend
      - db
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
    deploy:
      resources:
        limits:
          cpus: "2.0"
          memory: 1024M
        reservations:
          cpus: "0.5"
          memory: 256M
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s

  celery-worker:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: celery-worker
    command: >
      celery -A app.core.celery_app worker
      --loglevel=info
      --concurrency=4
      -Q webhooks,ai_inference,documents,notifications,bulk
      --max-tasks-per-child=1000
    restart: unless-stopped
    user: "1000:1000"
    security_opt:
      - no-new-privileges:true
    env_file:
      - .env
    networks:
      - backend
      - db
    depends_on:
      - redis
      - db
    deploy:
      resources:
        limits:
          cpus: "2.0"
          memory: 2048M
        reservations:
          cpus: "0.5"
          memory: 512M

  celery-beat:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: celery-beat
    command: >
      celery -A app.core.celery_app beat
      --loglevel=info
      --schedule=/tmp/celerybeat-schedule
    restart: unless-stopped
    user: "1000:1000"
    security_opt:
      - no-new-privileges:true
    read_only: true
    tmpfs:
      - /tmp:size=10M
    env_file:
      - .env
    networks:
      - backend
    depends_on:
      - redis
    deploy:
      resources:
        limits:
          cpus: "0.25"
          memory: 128M
        reservations:
          cpus: "0.05"
          memory: 64M

  db:
    image: supabase/postgres:15.6.1
    container_name: db
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    shm_size: "256mb"
    env_file:
      - .env
    volumes:
      - postgres-data:/var/lib/postgresql/data
      - ./scripts/init-db.sql:/docker-entrypoint-initdb.d/init.sql:ro
    networks:
      - db
    deploy:
      resources:
        limits:
          cpus: "2.0"
          memory: 2048M
        reservations:
          cpus: "0.5"
          memory: 512M
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USER} -d ${DB_NAME}"]
      interval: 10s
      timeout: 5s
      retries: 5

  pgbouncer:
    image: edoburu/pgbouncer:latest
    container_name: pgbouncer
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    read_only: true
    tmpfs:
      - /tmp:size=10M
      - /var/run/pgbouncer:size=1M
    env_file:
      - .env
    networks:
      - db
      - backend
    depends_on:
      db:
        condition: service_healthy
    deploy:
      resources:
        limits:
          cpus: "0.5"
          memory: 128M
        reservations:
          cpus: "0.1"
          memory: 32M

  redis:
    image: redis:7.2-alpine
    container_name: redis
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    read_only: true
    tmpfs:
      - /tmp:size=10M
    command: >
      redis-server
      --maxmemory 256mb
      --maxmemory-policy allkeys-lru
      --requirepass ${REDIS_PASSWORD}
      --rename-command FLUSHALL ""
      --rename-command FLUSHDB ""
      --rename-command DEBUG ""
      --rename-command CONFIG ""
    volumes:
      - redis-data:/data
    networks:
      - backend
    deploy:
      resources:
        limits:
          cpus: "0.5"
          memory: 384M
        reservations:
          cpus: "0.1"
          memory: 64M
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "${REDIS_PASSWORD}", "ping"]
      interval: 10s
      timeout: 5s
      retries: 3

  prometheus:
    image: prom/prometheus:v2.53.0
    container_name: prometheus
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    user: "nobody"
    read_only: true
    tmpfs:
      - /tmp:size=10M
    volumes:
      - ./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./monitoring/alert-rules.yml:/etc/prometheus/alert-rules.yml:ro
      - prometheus-data:/prometheus
    networks:
      - backend
    deploy:
      resources:
        limits:
          cpus: "0.5"
          memory: 512M
        reservations:
          cpus: "0.1"
          memory: 128M

  grafana:
    image: grafana/grafana:11.1.0
    container_name: grafana
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    user: "472"
    env_file:
      - .env
    volumes:
      - grafana-data:/var/lib/grafana
      - ./monitoring/dashboards:/etc/grafana/provisioning/dashboards:ro
      - ./monitoring/datasources:/etc/grafana/provisioning/datasources:ro
    networks:
      - backend
      - frontend
    deploy:
      resources:
        limits:
          cpus: "0.5"
          memory: 256M
        reservations:
          cpus: "0.1"
          memory: 64M

  telegram-bot:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: telegram-bot
    command: python -m app.services.monitoring.bot_entrypoint
    restart: unless-stopped
    user: "1000:1000"
    security_opt:
      - no-new-privileges:true
    env_file:
      - .env
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - backend
    depends_on:
      - redis
      - db
    deploy:
      resources:
        limits:
          cpus: "0.25"
          memory: 256M
        reservations:
          cpus: "0.1"
          memory: 128M

  cloudflare-tunnel:
    image: cloudflare/cloudflared:latest
    container_name: cloudflare-tunnel
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    read_only: true
    command: tunnel --no-autoupdate run
    environment:
      - TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}
    networks:
      - frontend
    depends_on:
      - traefik
    deploy:
      resources:
        limits:
          cpus: "0.25"
          memory: 128M

volumes:
  postgres-data:
    driver: local
  redis-data:
    driver: local
  traefik-acme:
    driver: local
  prometheus-data:
    driver: local
  grafana-data:
    driver: local
```

#### 4.5.2 Segmentación de redes Docker

```
Diagrama de segmentación de redes:

    Internet
       |
       v
+==============+    +==================+
|   frontend   |    |  cloudflare-     |
|   network    |----|  tunnel          |
+==============+    +==================+
       |
  +---------+     +---------+
  | traefik |     | grafana |
  +---------+     +---------+
       |               |
+==============+       |
|   backend    |-------+
|   network    |
+==============+
  |    |    |    |    |    |
  v    v    v    v    v    v
 api  celery celery redis prom telegram
       work  beat               bot
  |    |            |
+==============+    |
|  db_network  |----+
+==============+
  |         |
  v         v
  db    pgbouncer

Reglas:
- frontend: Tráfico externo. Solo Traefik, Grafana y Cloudflare.
- backend:  Comunicación entre servicios. Sin acceso externo.
- db:       Solo PostgreSQL y servicios que lo necesitan (API, workers, pgBouncer).
```

### 4.6 Esquema del documento `docs/security-policies.md`

```
1. Introducción
   - Alcance
   - Responsabilidades
   - Clasificación de datos

2. Seguridad del Servidor
   2.1 SSH Hardening
   2.2 Firewall (UFW)
   2.3 fail2ban
   2.4 Actualizaciones automáticas
   2.5 Sistema de archivos
   2.6 Kernel hardening (sysctl)

3. Seguridad de la Aplicación
   3.1 Headers HTTP de seguridad
   3.2 CORS por tenant
   3.3 Rate limiting por tier
   3.4 Validación de entrada (Pydantic strict)
   3.5 Prevención de inyección SQL
   3.6 Autenticación y autorización (JWT + RBAC)
   3.7 RLS en PostgreSQL

4. Seguridad de Red
   4.1 Cloudflare (DDoS, WAF)
   4.2 Cloudflare Tunnel
   4.3 mTLS (Authenticated Origin Pulls)
   4.4 Segmentación de redes Docker

5. Seguridad de Contenedores
   5.1 Usuarios no-root
   5.2 Filesystem read-only
   5.3 Límites de recursos
   5.4 Docker Secrets (roadmap)
   5.5 No privileged containers
   5.6 security_opt: no-new-privileges

6. Gestión de Secretos
   6.1 Variables de entorno (.env)
   6.2 Rotación de secretos
   6.3 Migración a Docker Secrets (plan)

7. Monitoreo de Seguridad
   7.1 Alertas de seguridad (Telegram)
   7.2 Logs de auditoría
   7.3 Detección de intrusiones (fail2ban)

8. Respuesta a Incidentes
   8.1 Procedimiento de escalación
   8.2 Contactos de emergencia
   8.3 Checklist de respuesta

9. Cumplimiento
   9.1 GDPR/Protección de datos
   9.2 Retención de datos por tenant
   9.3 Derecho al olvido (data deletion)

Apéndices:
A. Checklist de despliegue seguro
B. Comandos útiles de emergencia
C. Configuración de referencia
```

### 4.7 Reglas de alerta Prometheus para seguridad

```yaml
# monitoring/alert-rules.yml

groups:
  - name: security_alerts
    rules:
      - alert: HighAuthFailureRate
        expr: rate(http_requests_total{status="401"}[5m]) > 1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Alta tasa de fallos de autenticación"
          description: "Más de 1 req/s con 401 en los últimos 5 min. Posible intento de fuerza bruta."

      - alert: HighRateLimitHits
        expr: rate(http_requests_total{status="429"}[5m]) > 10
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "Alta tasa de rate limit hits"
          description: "Más de 10 req/s devolviendo 429. Posible abuso o DDoS."

      - alert: UnauthorizedAccessAttempt
        expr: rate(http_requests_total{status="403", path=~"/api/v1/admin/.*"}[5m]) > 0.1
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "Intentos de acceso no autorizado a endpoints admin"
          description: "Se detectaron intentos repetidos de acceso a endpoints de administración."

      - alert: SSLCertExpiringSoon
        expr: probe_ssl_earliest_cert_expiry - time() < 604800
        for: 1h
        labels:
          severity: warning
        annotations:
          summary: "Certificado SSL expira en menos de 7 días"
          description: "El certificado SSL expira en {{ $value | humanizeDuration }}."

      - alert: ContainerRunningAsRoot
        expr: container_processes{user="root"} > 0
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Contenedor ejecutando procesos como root"
          description: "El contenedor {{ $labels.name }} tiene procesos ejecutándose como root."

  - name: backup_alerts
    rules:
      - alert: BackupNotRunIn24h
        expr: time() - backup_last_success_timestamp > 86400
        for: 30m
        labels:
          severity: critical
        annotations:
          summary: "No se ha ejecutado un backup exitoso en 24 horas"

      - alert: ReplicationLagHigh
        expr: postgresql_replication_lag_seconds > 30
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Lag de replicación PostgreSQL alto"
          description: "El lag de replicación es de {{ $value }}s (umbral: 30s)."

      - alert: ReplicationDown
        expr: postgresql_replication_state == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "Replicación PostgreSQL desconectada"
          description: "La replicación por streaming no está activa."
```

### 4.8 Tests

```python
# tests/unit/middleware/test_rate_limit.py

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.middleware.rate_limit import RateLimitMiddleware, TIER_LIMITS


class TestRateLimitMiddleware:
    """Tests para el middleware de rate limiting."""

    @pytest.mark.asyncio
    async def test_health_check_bypasses_rate_limit(self) -> None:
        """Verifica que /health no pasa por rate limiting."""
        app = AsyncMock()
        middleware = RateLimitMiddleware(app)

        request = MagicMock()
        request.url.path = "/health"
        call_next = AsyncMock()

        await middleware.dispatch(request, call_next)
        call_next.assert_called_once()

    @pytest.mark.asyncio
    async def test_request_without_client_id_passes(self) -> None:
        """Verifica que requests sin client_id pasan sin límite."""
        app = AsyncMock()
        middleware = RateLimitMiddleware(app)

        request = MagicMock()
        request.url.path = "/api/v1/test"
        request.headers.get.return_value = None
        call_next = AsyncMock()

        await middleware.dispatch(request, call_next)
        call_next.assert_called_once()

    def test_tier_limits_are_correct(self) -> None:
        """Verifica los límites por tier."""
        assert TIER_LIMITS["free"] == 100
        assert TIER_LIMITS["pro"] == 1000
        assert TIER_LIMITS["enterprise"] == 10000


# tests/unit/middleware/test_cors.py

class TestDynamicCORSMiddleware:
    """Tests para el middleware CORS dinámico."""

    @pytest.mark.asyncio
    async def test_preflight_with_valid_origin(self) -> None:
        """Verifica que preflight con origen válido retorna 200."""
        from app.core.cors import DynamicCORSMiddleware

        app = AsyncMock()
        middleware = DynamicCORSMiddleware(app)

        request = MagicMock()
        request.method = "OPTIONS"
        request.headers.get.side_effect = lambda key, default="": {
            "origin": "https://app.example.com",
            "x-client-id": "test-uuid",
        }.get(key, default)

        with patch.object(
            middleware,
            "_get_allowed_origins",
            return_value=["https://app.example.com"],
        ):
            response = await middleware.dispatch(request, AsyncMock())
            assert response.status_code == 200
            assert "Access-Control-Allow-Origin" in response.headers

    @pytest.mark.asyncio
    async def test_request_with_unauthorized_origin(self) -> None:
        """Verifica que origen no autorizado no recibe headers CORS."""
        from app.core.cors import DynamicCORSMiddleware

        app = AsyncMock()
        middleware = DynamicCORSMiddleware(app)

        request = MagicMock()
        request.method = "GET"
        request.headers.get.side_effect = lambda key, default="": {
            "origin": "https://malicious.example.com",
            "x-client-id": "test-uuid",
        }.get(key, default)

        call_next = AsyncMock()
        mock_response = MagicMock()
        mock_response.headers = {}
        call_next.return_value = mock_response

        with patch.object(
            middleware,
            "_get_allowed_origins",
            return_value=["https://app.example.com"],
        ):
            response = await middleware.dispatch(request, call_next)
            assert "Access-Control-Allow-Origin" not in response.headers
```

---

## Resumen de archivos nuevos/modificados

### Archivos nuevos

| Archivo | Feature |
|---|---|
| `app/schemas/admin/celery_schemas.py` | F1 |
| `app/services/admin/celery_admin_service.py` | F1 |
| `app/services/admin/redis_admin_service.py` | F1 |
| `app/services/admin/task_logger.py` | F1 |
| `app/api/v1/endpoints/admin/celery_admin.py` | F1 |
| `app/models/celery_task_log.py` | F1 |
| `app/core/celery_signals.py` | F1 |
| `app/core/metrics/celery_metrics.py` | F1 |
| `app/tasks/metrics_collector.py` | F1 |
| `monitoring/dashboards/celery-operations.json` | F1 |
| `app/services/monitoring/telegram_bot.py` | F2 |
| `app/services/monitoring/monitoring_service.py` | F2 |
| `app/services/monitoring/alert_manager.py` | F2 |
| `app/services/monitoring/bot_entrypoint.py` | F2 |
| `app/services/monitoring/bot_singleton.py` | F2 |
| `app/tasks/monitoring_check.py` | F2 |
| `scripts/backup.sh` (mejorado) | F3 |
| `scripts/setup_replication.sh` | F3 |
| `scripts/backup_retention.sh` | F3 |
| `scripts/restore_test.sh` (mejorado) | F3 |
| `app/tasks/backup_tasks.py` | F3 |
| `docs/disaster-recovery.md` | F3 |
| `scripts/setup_ufw.sh` | F4 |
| `app/core/cors.py` | F4 |
| `app/middleware/rate_limit.py` | F4 |
| `traefik/dynamic/security-headers.yml` | F4 |
| `traefik/dynamic/cloudflare-mtls.yml` | F4 |
| `infra/server-hardening.yml` | F4 |
| `docs/security-policies.md` | F4 |
| `monitoring/alert-rules.yml` | F4 |

### Archivos modificados

| Archivo | Cambio |
|---|---|
| `app/core/celery_config.py` | Agregar tareas Beat (métricas, monitoreo, backup, retención) |
| `app/api/v1/router.py` | Incluir router de `celery_admin` |
| `docker-compose.yml` | Agregar servicios: `telegram-bot`, `cloudflare-tunnel`; hardening de todos los servicios |
| `traefik/traefik.yml` | Configuración Cloudflare (forwardedHeaders, certResolver) |
| `.env` | Variables: Telegram, Cloudflare, replicación |
| `alembic/versions/` | Nueva migración para `celery_task_logs` |

### Dependencias nuevas (agregar a `requirements.txt`)

```
python-telegram-bot>=21.0
docker>=7.0.0
psutil>=5.9.0
aiohttp>=3.9.0
```

---

## Criterios de aceptación

### Feature 1: Panel Celery/Redis

- [ ] Todos los endpoints responden correctamente con datos reales
- [ ] Solo `super_admin` puede acceder a los endpoints
- [ ] Filtrado y paginación de tareas funciona correctamente
- [ ] Revocación de tareas envía señal correcta al worker
- [ ] Reinicio de pool no pierde tareas en ejecución
- [ ] Dashboard de Grafana muestra métricas en tiempo real
- [ ] Métricas de Prometheus se actualizan cada 30 segundos
- [ ] Tests unitarios con cobertura > 80%

### Feature 2: Bot de Telegram

- [ ] Bot responde a todos los comandos documentados
- [ ] Solo responde a chat_ids en whitelist
- [ ] Alertas proactivas se envían cuando se superan umbrales
- [ ] Deduplicación previene alertas repetidas dentro del cooldown
- [ ] Reinicio de servicio requiere confirmación explícita
- [ ] Bot se ejecuta como servicio Docker independiente
- [ ] Health check HTTP responde correctamente
- [ ] Tests unitarios con mocks de Telegram API

### Feature 3: Backup y Replicación

- [ ] Backup diario ejecuta pre-checks (conectividad, espacio)
- [ ] Verificación post-backup valida integridad del dump
- [ ] Upload a S3 y VPS secundario funciona correctamente
- [ ] Replicación streaming funciona con lag < 1 segundo
- [ ] Alerta si lag de replicación > 30 segundos
- [ ] Política de retención elimina backups correctamente
- [ ] Prueba de restauración semanal se ejecuta y reporta
- [ ] Plan de recuperación documentado para 4 escenarios
- [ ] Scripts ejecutables y probados en staging

### Feature 4: Políticas de Seguridad

- [ ] Headers de seguridad presentes en todas las respuestas HTTP
- [ ] CORS dinámico por tenant funciona correctamente
- [ ] Rate limiting por tier aplicado correctamente
- [ ] Cloudflare Tunnel operativo como alternativa
- [ ] mTLS configurado entre Cloudflare y Traefik
- [ ] Todos los contenedores ejecutan como non-root
- [ ] Redes Docker segmentadas (frontend/backend/db)
- [ ] fail2ban bloquea IPs con intentos excesivos de auth
- [ ] UFW configurado con reglas mínimas
- [ ] Reglas de alerta Prometheus para eventos de seguridad
- [ ] Documento de políticas de seguridad completo
- [ ] Tests de middleware (CORS, rate limit)
