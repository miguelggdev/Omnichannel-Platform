"""Consulta de la actividad de los nodos del grafo LangGraph.

GET /api/v1/agent-logs/conversations/{id}  traza completa de una conversacion
GET /api/v1/agent-logs/stats               rendimiento agregado por nodo
GET /api/v1/agent-logs/errors              ultimos errores de los nodos

Quien *escribe* estos registros es `AgentLogger` (`app/services/agent_logger.py`)
a traves del middleware que envuelve cada nodo — ambos son entrega de Dev A
(`specs/sprint-07-addendum-agent-logging.md` §3 y §4). Aqui solo se leen.

La lectura se hace con selects propios en vez de delegar en `AgentLogger`: el
servicio todavia no existe y acoplar los endpoints a una firma que aun no esta
escrita significaria reescribirlos cuando llegue. Si al entregarse sus metodos
de lectura quedan como en la spec, delegar es un cambio interno que no toca el
contrato HTTP.

El modelo `AgentActionLog` se importa de forma perezosa por la misma razon que
`ContactUnifier` en `contacts.py`: mientras Dev A no lo entregue, estos tres
endpoints responden 503 con un motivo legible en vez de reventar el arranque de
toda la API con un ImportError al registrar el router.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import VALIDATION_ERROR, AppException
from app.schemas.agent_log import AgentActionLogResponse, NodeStatsResponse

logger = logging.getLogger(__name__)

router = APIRouter()

LOGGING_UNAVAILABLE = "AGENT_LOGGING_UNAVAILABLE"

# Estados que `AgentLogger.log_action()` escribe en la columna `status`.
VALID_STATUSES = ("success", "error", "skipped", "timeout")

_READ_ROLES = ("super_admin", "admin", "supervisor")
_STATS_ROLES = ("super_admin", "admin")


def _agent_action_log_model() -> Any:
    """Devuelve el modelo `AgentActionLog` o levanta 503 si no esta entregado.

    Returns:
        La clase del modelo SQLAlchemy.

    Raises:
        AppException: 503 si `app/models/agent_action_log.py` todavia no existe.
    """
    try:
        from app.models.agent_action_log import AgentActionLog
    except ImportError as exc:
        logger.error("AgentActionLog no disponible: %s", exc)
        raise AppException(
            status_code=503,
            error_code=LOGGING_UNAVAILABLE,
            message="El registro de actividad de agentes no esta disponible todavia",
        ) from exc
    return AgentActionLog


@router.get("/conversations/{conversation_id}", response_model=list[AgentActionLogResponse])
async def get_conversation_agent_logs(
    conversation_id: UUID,
    node_name: str | None = Query(default=None, description="Filtra por nodo del grafo"),
    status: str | None = Query(default=None, description="Filtra por estado de la accion"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: dict[str, Any] = Depends(require_role(*_READ_ROLES)),
) -> list[AgentActionLogResponse]:
    """Traza de lo que hizo cada nodo del grafo en una conversacion.

    Args:
        conversation_id: Conversacion a inspeccionar.
        node_name: Limita a un nodo (intent_routing, rag_query, respond, ...).
        status: Limita a un estado (success, error, skipped, timeout).
        limit: Maximo de registros, hasta 500.
        offset: Desplazamiento para paginar.
        user: Usuario autenticado; supervisor, admin o super_admin.

    Returns:
        Las acciones registradas, de la mas reciente a la mas vieja.

    Raises:
        AppException: 400 si el status no es valido, 503 si el modelo de logs no
            esta entregado.
    """
    _validar_status(status)
    log_model = _agent_action_log_model()
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        filtros = [
            log_model.client_id == client_id,
            log_model.conversation_id == conversation_id,
        ]
        if node_name is not None:
            filtros.append(log_model.node_name == node_name)
        if status is not None:
            filtros.append(log_model.status == status)

        stmt = (
            select(log_model)
            .where(*filtros)
            .order_by(log_model.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        registros = (await session.execute(stmt)).scalars().all()
        return [AgentActionLogResponse.model_validate(r) for r in registros]


@router.get("/stats", response_model=list[NodeStatsResponse])
async def get_agent_node_stats(
    hours: int = Query(default=24, ge=1, le=720, description="Ventana de tiempo en horas"),
    user: dict[str, Any] = Depends(require_role(*_STATS_ROLES)),
) -> list[NodeStatsResponse]:
    """Rendimiento de cada nodo del grafo en las ultimas `hours` horas.

    Args:
        hours: Ventana hacia atras, de 1 hora a 30 dias.
        user: Usuario autenticado; admin o super_admin.

    Returns:
        Una fila por nodo con conteo, duracion media, tokens y tasa de error.

    Raises:
        AppException: 503 si el modelo de logs no esta entregado.
    """
    log_model = _agent_action_log_model()
    client_id: UUID = user["client_id"]
    corte = datetime.now(timezone.utc) - timedelta(hours=hours)

    async with tenant_session(client_id) as session:
        stmt = (
            select(
                log_model.node_name,
                func.count().label("total_actions"),
                func.avg(log_model.duration_ms).label("avg_duration_ms"),
                func.sum(log_model.tokens_used).label("total_tokens"),
                # count(*) FILTER (WHERE status = 'error'): una sola pasada por
                # los datos en vez de una segunda consulta solo para los errores.
                func.count().filter(log_model.status == "error").label("error_count"),
            )
            .where(
                log_model.client_id == client_id,
                log_model.created_at >= corte,
            )
            .group_by(log_model.node_name)
            .order_by(log_model.node_name)
        )
        filas = (await session.execute(stmt)).all()

    return [
        NodeStatsResponse(
            node_name=fila.node_name,
            total_actions=int(fila.total_actions),
            avg_duration_ms=round(float(fila.avg_duration_ms or 0), 2),
            total_tokens=int(fila.total_tokens or 0),
            error_count=int(fila.error_count),
            error_rate=(
                round(int(fila.error_count) / int(fila.total_actions) * 100, 2)
                if fila.total_actions
                else 0.0
            ),
        )
        for fila in filas
    ]


@router.get("/errors", response_model=list[AgentActionLogResponse])
async def get_agent_errors(
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=50, ge=1, le=200),
    user: dict[str, Any] = Depends(require_role(*_STATS_ROLES)),
) -> list[AgentActionLogResponse]:
    """Ultimos errores registrados por los nodos del grafo.

    Args:
        hours: Ventana hacia atras, de 1 hora a 30 dias.
        limit: Maximo de errores a devolver.
        user: Usuario autenticado; admin o super_admin.

    Returns:
        Los errores mas recientes primero.

    Raises:
        AppException: 503 si el modelo de logs no esta entregado.
    """
    log_model = _agent_action_log_model()
    client_id: UUID = user["client_id"]
    corte = datetime.now(timezone.utc) - timedelta(hours=hours)

    async with tenant_session(client_id) as session:
        stmt = (
            select(log_model)
            .where(
                log_model.client_id == client_id,
                log_model.status == "error",
                log_model.created_at >= corte,
            )
            .order_by(log_model.created_at.desc())
            .limit(limit)
        )
        registros = (await session.execute(stmt)).scalars().all()
        return [AgentActionLogResponse.model_validate(r) for r in registros]


def _validar_status(status: str | None) -> None:
    """Rechaza un filtro de status que no exista.

    Args:
        status: Valor recibido por query string, o None.

    Raises:
        AppException: 400 si no es uno de `VALID_STATUSES`.
    """
    if status is not None and status not in VALID_STATUSES:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message=f"Status invalido: {status}. Validos: {', '.join(VALID_STATUSES)}",
        )
