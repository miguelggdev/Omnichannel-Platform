"""Schemas del panel de plataforma (super_admin) — Sprint 15, fase 2."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ClientSummary(BaseModel):
    """Un cliente (tenant) en el listado de la plataforma.

    Attributes:
        id: Id del tenant.
        name: Nombre.
        slug: Slug unico.
        plan: Plan de suscripcion.
        is_active: `False` si esta suspendido (sus usuarios no inician ni renuevan sesion).
        created_at: Alta.
        suspended_at: Cuando se suspendio, si lo esta.
        users_count: Usuarios del tenant.
        conversations_30d: Conversaciones creadas en 30 dias.
        messages_30d: Mensajes de 30 dias.
        last_message_at: Ultimo mensaje, o `None` si nunca hubo.
    """

    id: UUID
    name: str
    slug: str
    plan: str
    is_active: bool
    created_at: datetime
    suspended_at: datetime | None
    users_count: int
    conversations_30d: int
    messages_30d: int
    last_message_at: datetime | None


class ClientListResponse(BaseModel):
    """Pagina de clientes."""

    items: list[ClientSummary]
    total: int
    page: int
    page_size: int


class ClientDetail(BaseModel):
    """Un cliente con su uso. Solo conteos y fechas: nunca contenido del tenant."""

    id: UUID
    name: str
    slug: str
    plan: str
    is_active: bool
    created_at: datetime
    suspended_at: datetime | None
    alert_message: str | None
    users_total: int
    users_active: int
    contacts_total: int
    conversations_open: int
    conversations_30d: int
    messages_30d: int
    last_message_at: datetime | None
    documents_ready: int
    has_agent: bool
    token_used: int | None
    token_budget: int | None


class ClientStatusUpdate(BaseModel):
    """Cuerpo de `PUT /api/v1/platform/clients/{id}/status`.

    Attributes:
        is_active: `False` suspende al cliente; `True` lo reactiva.
        reason: Mensaje que se guarda al suspender (`clients.alert_message`); se borra al
            reactivar.
    """

    is_active: bool
    reason: str | None = Field(default=None, max_length=1000)


class WorkerInfo(BaseModel):
    """Un worker de Celery que respondio al `inspect`."""

    hostname: str
    pid: int | None
    concurrency: int | None
    active_tasks: int
    reserved_tasks: int
    processed_total: int | None
    queues: list[str]
    uptime_seconds: int | None


class WorkersResponse(BaseModel):
    """Estado de los workers.

    Attributes:
        broker_ok: Si se pudo hablar con el broker. Con `False` no se sabe cuantos workers
            hay: no es lo mismo que "ninguno".
        workers: Los que respondieron; vacia si el broker esta bien y no hay ninguno.
    """

    broker_ok: bool
    workers: list[WorkerInfo]


class QueueDepth(BaseModel):
    """Mensajes esperando en una cola de Celery."""

    name: str
    pending: int


class TaskInfo(BaseModel):
    """Una tarea en curso. Sin argumentos: pueden llevar datos de contactos."""

    id: str
    name: str
    worker: str
    state: Literal["active", "reserved", "scheduled"]
    queue: str | None
    started_at: float | None


class RedisInfo(BaseModel):
    """Salud de Redis (cache y broker)."""

    version: str
    uptime_seconds: int
    connected_clients: int
    used_memory: int
    max_memory: int
    total_keys: int
    hit_ratio: float | None


class ComponentStatus(BaseModel):
    """Estado de un componente del sistema."""

    name: str
    ok: bool
    latency_ms: float | None
    detail: str | None = None


class SystemStatus(BaseModel):
    """Vista de salud de la plataforma."""

    version: str
    environment: str
    components: list[ComponentStatus]


class SecurityCheck(BaseModel):
    """Una comprobacion de la postura de seguridad.

    Attributes:
        id: Identificador estable (el frontend lo traduce).
        status: `ok`, `warn` (revisar) o `fail` (corregir).
        detail: Dato que justifica el estado. Nunca un secreto.
    """

    id: str
    status: str
    detail: str | None = None


class RlsTableStatus(BaseModel):
    """Estado de RLS de una tabla con aislamiento por tenant."""

    name: str
    rls_enabled: bool
    rls_forced: bool


class SecurityOverview(BaseModel):
    """Vision general de seguridad de la plataforma (solo `super_admin`).

    Attributes:
        environment: `APP_ENV`.
        checks: Comprobaciones de configuracion y del rol de base de datos.
        rls_tables: Cada tabla con `client_id` (y `clients`) y su RLS.
        rls_unprotected: Nombres de las que no tienen RLS habilitado **y forzado**.
    """

    environment: str
    checks: list[SecurityCheck]
    rls_tables: list[RlsTableStatus]
    rls_unprotected: list[str]
