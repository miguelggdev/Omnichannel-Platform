# Sprint 6 — Addendum: Logging de Actividad de Agentes (LangGraph)

## Objetivo

Agregar un sistema completo de logging de actividad para cada nodo del grafo LangGraph, registrando todas las acciones, decisiones y resultados de cada agente en cada turno de conversación.

## Prerequisitos

- Sprint 6 base completado (grafo LangGraph funcional)
- Sprint 3 completado (modelos SQLAlchemy, async sessions)

## Archivos a Crear/Modificar

- `app/models/agent_action_log.py` — Modelo SQLAlchemy para logs de agentes
- `app/services/agent_logger.py` — Servicio de logging de agentes
- `app/agents/middleware/logging_middleware.py` — Middleware que wrappea cada nodo
- Modificar `app/agents/graph.py` — Integrar middleware de logging
- Modificar `app/agents/state.py` — Agregar campo `action_log` al ConversationState
- `app/api/v1/agent_logs.py` — Endpoints para consultar logs
- `tests/unit/test_agent_logging.py`

## Tareas Detalladas

### 1. Modelo AgentActionLog

```sql
-- Migración SQL
CREATE TABLE IF NOT EXISTS agent_action_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    client_id UUID NOT NULL REFERENCES clients(id),
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    message_id UUID REFERENCES messages(id),
    node_name VARCHAR(100) NOT NULL,       -- 'intent_routing', 'rag_query', 'respond', etc.
    action_type VARCHAR(50) NOT NULL,      -- 'decision', 'query', 'response', 'tool_call', 'error', 'handoff'
    input_summary TEXT,                     -- Resumen del input al nodo (truncado a 500 chars)
    output_summary TEXT,                    -- Resumen del output del nodo (truncado a 500 chars)
    details JSONB DEFAULT '{}',            -- Detalles completos (modelo usado, tokens, confidence, etc.)
    duration_ms INTEGER,                   -- Duración de ejecución del nodo en ms
    tokens_used INTEGER DEFAULT 0,         -- Tokens consumidos en este nodo
    model_used VARCHAR(100),               -- Modelo LLM usado (si aplica)
    status VARCHAR(20) DEFAULT 'success',  -- 'success', 'error', 'skipped', 'timeout'
    error_message TEXT,                    -- Mensaje de error si status='error'
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    CONSTRAINT fk_client FOREIGN KEY (client_id) REFERENCES clients(id),
    CONSTRAINT fk_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);

-- Índices
CREATE INDEX idx_agent_action_logs_client_id ON agent_action_logs(client_id);
CREATE INDEX idx_agent_action_logs_conversation_id ON agent_action_logs(conversation_id);
CREATE INDEX idx_agent_action_logs_node_name ON agent_action_logs(node_name);
CREATE INDEX idx_agent_action_logs_created_at ON agent_action_logs(created_at DESC);
CREATE INDEX idx_agent_action_logs_status ON agent_action_logs(status) WHERE status = 'error';

-- RLS
ALTER TABLE agent_action_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_action_logs FORCE ROW LEVEL SECURITY;

CREATE POLICY agent_action_logs_tenant_isolation ON agent_action_logs
    FOR ALL
    USING (client_id = current_setting('app.current_client_id')::uuid);
```

### 2. Modelo SQLAlchemy

```python
# app/models/agent_action_log.py
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, Integer
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel


class AgentActionLog(TenantBaseModel):
    """Log de acciones ejecutadas por nodos del grafo LangGraph.
    
    Registra cada acción de cada nodo para auditoría, debugging y
    análisis de rendimiento de los agentes de IA.
    """
    
    __tablename__ = "agent_action_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False, index=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id"), nullable=True
    )
    node_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    input_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    model_used: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="success")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    __table_args__ = (
        Index("idx_agent_action_logs_created_at", "created_at", postgresql_using="btree"),
        Index(
            "idx_agent_action_logs_error_status",
            "status",
            postgresql_where="status = 'error'",
        ),
    )
```

### 3. Servicio de Logging de Agentes

```python
# app/services/agent_logger.py
from __future__ import annotations

import time
import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_action_log import AgentActionLog


class AgentLogger:
    """Servicio para registrar acciones de agentes LangGraph.
    
    Cada nodo del grafo usa este servicio para registrar sus acciones,
    decisiones, consultas y errores. Los logs son por tenant (client_id)
    y por conversación.
    """

    MAX_SUMMARY_LENGTH = 500

    def __init__(self, db: AsyncSession, client_id: uuid.UUID):
        """Inicializar logger para un tenant específico.
        
        Args:
            db: Sesión async de SQLAlchemy.
            client_id: ID del tenant.
        """
        self.db = db
        self.client_id = client_id

    def _truncate(self, text: str | None) -> str | None:
        """Truncar texto a MAX_SUMMARY_LENGTH caracteres."""
        if text and len(text) > self.MAX_SUMMARY_LENGTH:
            return text[: self.MAX_SUMMARY_LENGTH] + "..."
        return text

    async def log_action(
        self,
        conversation_id: uuid.UUID,
        node_name: str,
        action_type: str,
        *,
        message_id: uuid.UUID | None = None,
        input_summary: str | None = None,
        output_summary: str | None = None,
        details: dict[str, Any] | None = None,
        duration_ms: int | None = None,
        tokens_used: int = 0,
        model_used: str | None = None,
        status: str = "success",
        error_message: str | None = None,
    ) -> AgentActionLog:
        """Registrar una acción de un nodo del grafo.
        
        Args:
            conversation_id: ID de la conversación.
            node_name: Nombre del nodo ('intent_routing', 'rag_query', etc.).
            action_type: Tipo de acción ('decision', 'query', 'response',
                'tool_call', 'error', 'handoff').
            message_id: ID del mensaje que disparó la acción.
            input_summary: Resumen del input (truncado automáticamente).
            output_summary: Resumen del output (truncado automáticamente).
            details: Detalles adicionales en JSONB.
            duration_ms: Duración en milisegundos.
            tokens_used: Tokens consumidos.
            model_used: Modelo LLM usado.
            status: Estado ('success', 'error', 'skipped', 'timeout').
            error_message: Mensaje de error si aplica.
            
        Returns:
            La entrada de log creada.
        """
        log_entry = AgentActionLog(
            client_id=self.client_id,
            conversation_id=conversation_id,
            message_id=message_id,
            node_name=node_name,
            action_type=action_type,
            input_summary=self._truncate(input_summary),
            output_summary=self._truncate(output_summary),
            details=details or {},
            duration_ms=duration_ms,
            tokens_used=tokens_used,
            model_used=model_used,
            status=status,
            error_message=error_message,
        )
        self.db.add(log_entry)

        # Log también a Loguru para correlación con traces
        log_method = logger.error if status == "error" else logger.info
        log_method(
            "Agent action: {node}.{action} [{status}] ({duration}ms)",
            node=node_name,
            action=action_type,
            status=status,
            duration=duration_ms or 0,
            extra={
                "client_id": str(self.client_id),
                "conversation_id": str(conversation_id),
                "node_name": node_name,
                "action_type": action_type,
                "tokens_used": tokens_used,
            },
        )

        return log_entry

    async def get_conversation_log(
        self,
        conversation_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
        node_name: str | None = None,
        status: str | None = None,
    ) -> list[AgentActionLog]:
        """Obtener logs de una conversación con filtros opcionales.
        
        Args:
            conversation_id: ID de la conversación.
            limit: Máximo de registros a retornar.
            offset: Offset para paginación.
            node_name: Filtrar por nombre de nodo.
            status: Filtrar por estado.
            
        Returns:
            Lista de entradas de log ordenadas por created_at desc.
        """
        query = (
            select(AgentActionLog)
            .where(AgentActionLog.conversation_id == conversation_id)
            .order_by(AgentActionLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if node_name:
            query = query.where(AgentActionLog.node_name == node_name)
        if status:
            query = query.where(AgentActionLog.status == status)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_node_stats(
        self,
        *,
        hours: int = 24,
    ) -> list[dict[str, Any]]:
        """Estadísticas de rendimiento por nodo.
        
        Args:
            hours: Ventana de tiempo en horas.
            
        Returns:
            Lista de dicts con stats por nodo (count, avg_duration, 
            error_rate, total_tokens).
        """
        from datetime import datetime, timedelta

        cutoff = datetime.utcnow() - timedelta(hours=hours)
        query = (
            select(
                AgentActionLog.node_name,
                func.count().label("total_actions"),
                func.avg(AgentActionLog.duration_ms).label("avg_duration_ms"),
                func.sum(AgentActionLog.tokens_used).label("total_tokens"),
                func.count()
                .filter(AgentActionLog.status == "error")
                .label("error_count"),
            )
            .where(AgentActionLog.created_at >= cutoff)
            .group_by(AgentActionLog.node_name)
        )

        result = await self.db.execute(query)
        rows = result.all()
        return [
            {
                "node_name": row.node_name,
                "total_actions": row.total_actions,
                "avg_duration_ms": round(float(row.avg_duration_ms or 0), 2),
                "total_tokens": row.total_tokens or 0,
                "error_count": row.error_count,
                "error_rate": round(
                    (row.error_count / row.total_actions * 100)
                    if row.total_actions > 0
                    else 0,
                    2,
                ),
            }
            for row in rows
        ]
```

### 4. Middleware de Logging para Nodos LangGraph

```python
# app/agents/middleware/logging_middleware.py
from __future__ import annotations

import functools
import time
import traceback
from typing import Any, Callable, Awaitable

from app.agents.state import ConversationState
from app.services.agent_logger import AgentLogger


def logged_node(
    node_name: str,
    action_type: str = "decision",
) -> Callable:
    """Decorator que wrappea un nodo LangGraph con logging automático.
    
    Registra automáticamente:
    - Input: últimos campos relevantes del state
    - Output: campos retornados por el nodo
    - Duración de ejecución
    - Errores si ocurren
    
    Args:
        node_name: Nombre del nodo para el log.
        action_type: Tipo de acción default ('decision', 'query', etc.).
        
    Returns:
        Decorator para funciones de nodo LangGraph.
        
    Example:
        @logged_node("intent_routing", action_type="decision")
        async def intent_routing_node(state: ConversationState) -> dict:
            ...
    """

    def decorator(
        func: Callable[[ConversationState], Awaitable[dict[str, Any]]],
    ) -> Callable[[ConversationState], Awaitable[dict[str, Any]]]:

        @functools.wraps(func)
        async def wrapper(state: ConversationState) -> dict[str, Any]:
            db = state.get("_db_session")
            client_id = state.get("client_id")
            conversation_id = state.get("conversation_id")
            message_id = state.get("message_id")

            # Si no hay sesión DB, ejecutar sin logging
            if not db or not client_id:
                return await func(state)

            agent_logger = AgentLogger(db, client_id)
            start_time = time.monotonic()

            # Capturar input summary
            input_summary = _build_input_summary(state, node_name)

            try:
                result = await func(state)
                duration_ms = int((time.monotonic() - start_time) * 1000)

                # Extraer metadata del resultado
                tokens = result.get("_tokens_used", 0)
                model = result.get("_model_used")
                output_summary = _build_output_summary(result, node_name)
                details = _extract_details(result, node_name)

                await agent_logger.log_action(
                    conversation_id=conversation_id,
                    node_name=node_name,
                    action_type=action_type,
                    message_id=message_id,
                    input_summary=input_summary,
                    output_summary=output_summary,
                    details=details,
                    duration_ms=duration_ms,
                    tokens_used=tokens,
                    model_used=model,
                    status="success",
                )

                return result

            except Exception as exc:
                duration_ms = int((time.monotonic() - start_time) * 1000)

                await agent_logger.log_action(
                    conversation_id=conversation_id,
                    node_name=node_name,
                    action_type="error",
                    message_id=message_id,
                    input_summary=input_summary,
                    details={"traceback": traceback.format_exc()[:2000]},
                    duration_ms=duration_ms,
                    status="error",
                    error_message=str(exc)[:500],
                )

                raise  # Re-raise para que LangGraph maneje el error

        return wrapper

    return decorator


def _build_input_summary(state: ConversationState, node_name: str) -> str:
    """Construir resumen del input relevante para el nodo."""
    parts = []
    msg = state.get("last_user_message") or state.get("message")
    if msg:
        text = msg if isinstance(msg, str) else getattr(msg, "text", str(msg))
        parts.append(f"msg: {text[:200]}")
    if state.get("intent"):
        parts.append(f"intent: {state['intent']}")
    if state.get("detected_language"):
        parts.append(f"lang: {state['detected_language']}")
    return " | ".join(parts) if parts else f"[{node_name} input]"


def _build_output_summary(result: dict[str, Any], node_name: str) -> str:
    """Construir resumen del output del nodo."""
    parts = []
    for key in ("intent", "response", "intent_confidence", "sentiment",
                "rag_results_count", "next_node", "action"):
        if key in result:
            val = result[key]
            if isinstance(val, str) and len(val) > 200:
                val = val[:200] + "..."
            parts.append(f"{key}: {val}")
    return " | ".join(parts) if parts else f"[{node_name} output]"


def _extract_details(result: dict[str, Any], node_name: str) -> dict:
    """Extraer detalles estructurados del resultado para JSONB."""
    details = {}
    detail_keys = {
        "intent_confidence", "rag_sources", "rag_results_count",
        "sentiment_score", "sentiment_level", "budget_status",
        "budget_percentage", "tool_calls", "handoff_reason",
        "training_mode", "approval_required",
    }
    for key in detail_keys:
        if key in result:
            details[key] = result[key]
    return details
```

### 5. Integración en el Grafo

```python
# Modificaciones en app/agents/graph.py

from app.agents.middleware.logging_middleware import logged_node

# Wrappear cada nodo existente con el decorator:

@logged_node("token_budget_check", action_type="decision")
async def token_budget_check_node(state: ConversationState) -> dict:
    """Verificar presupuesto de tokens del tenant."""
    # ... código existente del nodo ...

@logged_node("intent_routing", action_type="decision")
async def intent_routing_node(state: ConversationState) -> dict:
    """Detectar intent del mensaje del usuario."""
    # ... código existente ...

@logged_node("rag_query", action_type="query")
async def rag_query_node(state: ConversationState) -> dict:
    """Buscar en la base de conocimiento."""
    # ... código existente ...
    # Agregar al resultado: "_tokens_used": tokens, "_model_used": model
    result["_tokens_used"] = completion.usage.total_tokens
    result["_model_used"] = model_name
    return result

@logged_node("human_handoff", action_type="handoff")
async def human_handoff_node(state: ConversationState) -> dict:
    """Transferir conversación a humano."""
    # ... código existente ...

@logged_node("respond", action_type="response")
async def respond_node(state: ConversationState) -> dict:
    """Enviar respuesta al usuario."""
    # ... código existente ...

@logged_node("training_approval", action_type="decision")
async def training_approval_node(state: ConversationState) -> dict:
    """Evaluar si la respuesta necesita aprobación (training mode)."""
    # ... código existente ...

# Para nodos de Fase 2/3 (cuando se implementen):
@logged_node("sentiment_analysis", action_type="decision")
@logged_node("scheduling", action_type="tool_call")
@logged_node("financial_agent", action_type="tool_call")
@logged_node("marketing_agent", action_type="tool_call")
@logged_node("clinical_agent", action_type="tool_call")
```

### 6. Campo en ConversationState

```python
# Modificación en app/agents/state.py
from typing import TypedDict

class ConversationState(TypedDict, total=False):
    # ... campos existentes (19 campos) ...
    
    # Nuevo: referencia a sesión DB para logging
    _db_session: Any          # AsyncSession (no serializable, no se persiste)
    _action_log_ids: list[str]  # IDs de logs creados en este turno
```

### 7. Endpoints para Consultar Logs

```python
# app/api/v1/agent_logs.py
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.core.dependencies import TenantSession, CurrentUser, require_role
from app.services.agent_logger import AgentLogger

router = APIRouter(prefix="/agent-logs", tags=["Agent Logs"])


class AgentActionLogResponse(BaseModel):
    """Respuesta con un registro de log de agente."""
    id: UUID
    conversation_id: UUID
    message_id: Optional[UUID] = None
    node_name: str
    action_type: str
    input_summary: Optional[str] = None
    output_summary: Optional[str] = None
    details: dict = Field(default_factory=dict)
    duration_ms: Optional[int] = None
    tokens_used: int = 0
    model_used: Optional[str] = None
    status: str
    error_message: Optional[str] = None
    created_at: datetime


class NodeStatsResponse(BaseModel):
    """Estadísticas de rendimiento por nodo."""
    node_name: str
    total_actions: int
    avg_duration_ms: float
    total_tokens: int
    error_count: int
    error_rate: float


@router.get(
    "/conversations/{conversation_id}",
    response_model=list[AgentActionLogResponse],
    dependencies=[Depends(require_role("supervisor"))],
)
async def get_conversation_agent_logs(
    conversation_id: UUID,
    db: TenantSession,
    user: CurrentUser,
    node_name: Optional[str] = Query(None, description="Filtrar por nombre de nodo"),
    status: Optional[str] = Query(None, description="Filtrar por estado"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[AgentActionLogResponse]:
    """Obtener logs de actividad de agentes para una conversación.
    
    Requiere rol: supervisor, admin, super_admin.
    Los logs muestran cada acción tomada por cada nodo del grafo
    durante el procesamiento de mensajes de la conversación.
    """
    agent_logger = AgentLogger(db, user.client_id)
    logs = await agent_logger.get_conversation_log(
        conversation_id,
        limit=limit,
        offset=offset,
        node_name=node_name,
        status=status,
    )
    return [AgentActionLogResponse.model_validate(log, from_attributes=True) for log in logs]


@router.get(
    "/stats",
    response_model=list[NodeStatsResponse],
    dependencies=[Depends(require_role("admin"))],
)
async def get_agent_node_stats(
    db: TenantSession,
    user: CurrentUser,
    hours: int = Query(24, ge=1, le=720, description="Ventana de tiempo en horas"),
) -> list[NodeStatsResponse]:
    """Estadísticas de rendimiento de nodos del grafo.
    
    Requiere rol: admin, super_admin.
    Retorna métricas agregadas por nodo: conteo, duración promedio,
    tokens totales y tasa de error.
    """
    agent_logger = AgentLogger(db, user.client_id)
    stats = await agent_logger.get_node_stats(hours=hours)
    return [NodeStatsResponse(**s) for s in stats]


@router.get(
    "/errors",
    response_model=list[AgentActionLogResponse],
    dependencies=[Depends(require_role("admin"))],
)
async def get_agent_errors(
    db: TenantSession,
    user: CurrentUser,
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(50, ge=1, le=200),
) -> list[AgentActionLogResponse]:
    """Obtener errores recientes de agentes.
    
    Requiere rol: admin, super_admin.
    Lista los errores más recientes de los nodos del grafo para
    diagnóstico y debugging.
    """
    from sqlalchemy import select
    from datetime import timedelta
    from app.models.agent_action_log import AgentActionLog

    cutoff = datetime.utcnow() - timedelta(hours=hours)
    query = (
        select(AgentActionLog)
        .where(
            AgentActionLog.status == "error",
            AgentActionLog.created_at >= cutoff,
        )
        .order_by(AgentActionLog.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(query)
    logs = result.scalars().all()
    return [AgentActionLogResponse.model_validate(log, from_attributes=True) for log in logs]
```

### 8. Métricas Prometheus para Agentes

```python
# Agregar en app/core/metrics.py (Sprint 8)

from prometheus_client import Counter, Histogram, Gauge

# Métricas de nodos LangGraph
AGENT_NODE_DURATION = Histogram(
    "agent_node_duration_seconds",
    "Duración de ejecución de nodos del grafo",
    ["node_name", "status"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

AGENT_NODE_TOTAL = Counter(
    "agent_node_executions_total",
    "Total de ejecuciones de nodos",
    ["node_name", "action_type", "status"],
)

AGENT_NODE_ERRORS = Counter(
    "agent_node_errors_total",
    "Total de errores en nodos",
    ["node_name", "error_type"],
)

AGENT_TOKENS_USED = Counter(
    "agent_tokens_used_total",
    "Tokens consumidos por nodos",
    ["node_name", "model"],
)
```

### 9. Tests

```python
# tests/unit/test_agent_logging.py
import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from app.services.agent_logger import AgentLogger
from app.agents.middleware.logging_middleware import (
    logged_node,
    _build_input_summary,
    _build_output_summary,
    _extract_details,
)


class TestAgentLogger:
    """Tests para el servicio de logging de agentes."""

    @pytest.fixture
    def mock_db(self):
        """Mock de sesión async de SQLAlchemy."""
        db = AsyncMock()
        db.add = MagicMock()
        return db

    @pytest.fixture
    def agent_logger(self, mock_db):
        """Instancia de AgentLogger con DB mock."""
        return AgentLogger(mock_db, uuid.uuid4())

    @pytest.mark.asyncio
    async def test_log_action_success(self, agent_logger, mock_db):
        """Registrar acción exitosa crea entrada en DB."""
        conv_id = uuid.uuid4()
        result = await agent_logger.log_action(
            conversation_id=conv_id,
            node_name="intent_routing",
            action_type="decision",
            input_summary="msg: Hola, necesito una cita",
            output_summary="intent: scheduling",
            duration_ms=45,
            tokens_used=150,
            model_used="gpt-4o-mini",
        )
        mock_db.add.assert_called_once()
        assert result.node_name == "intent_routing"
        assert result.status == "success"
        assert result.tokens_used == 150

    @pytest.mark.asyncio
    async def test_log_action_error(self, agent_logger, mock_db):
        """Registrar acción con error incluye error_message."""
        conv_id = uuid.uuid4()
        result = await agent_logger.log_action(
            conversation_id=conv_id,
            node_name="rag_query",
            action_type="error",
            status="error",
            error_message="Connection timeout to pgvector",
            duration_ms=5000,
        )
        assert result.status == "error"
        assert "timeout" in result.error_message

    @pytest.mark.asyncio
    async def test_truncate_long_summary(self, agent_logger):
        """Resúmenes largos se truncan a MAX_SUMMARY_LENGTH."""
        long_text = "x" * 1000
        truncated = agent_logger._truncate(long_text)
        assert len(truncated) == 503  # 500 + "..."
        assert truncated.endswith("...")

    @pytest.mark.asyncio
    async def test_truncate_none(self, agent_logger):
        """Truncar None retorna None."""
        assert agent_logger._truncate(None) is None

    @pytest.mark.asyncio
    async def test_truncate_short(self, agent_logger):
        """Textos cortos no se truncan."""
        short = "Hola mundo"
        assert agent_logger._truncate(short) == short


class TestLoggingMiddleware:
    """Tests para el middleware de logging de nodos."""

    @pytest.mark.asyncio
    async def test_logged_node_success(self):
        """Decorator registra acción exitosa."""
        mock_db = AsyncMock()
        mock_db.add = MagicMock()

        @logged_node("test_node", action_type="decision")
        async def my_node(state):
            return {"intent": "greeting", "intent_confidence": 0.95}

        state = {
            "_db_session": mock_db,
            "client_id": uuid.uuid4(),
            "conversation_id": uuid.uuid4(),
            "message_id": uuid.uuid4(),
            "last_user_message": "Hola",
        }

        result = await my_node(state)
        assert result["intent"] == "greeting"
        mock_db.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_logged_node_error(self):
        """Decorator registra error y re-raises la excepción."""
        mock_db = AsyncMock()
        mock_db.add = MagicMock()

        @logged_node("failing_node", action_type="query")
        async def bad_node(state):
            raise ValueError("Test error")

        state = {
            "_db_session": mock_db,
            "client_id": uuid.uuid4(),
            "conversation_id": uuid.uuid4(),
            "message_id": None,
        }

        with pytest.raises(ValueError, match="Test error"):
            await bad_node(state)

        mock_db.add.assert_called_once()
        logged_entry = mock_db.add.call_args[0][0]
        assert logged_entry.status == "error"

    @pytest.mark.asyncio
    async def test_logged_node_without_db(self):
        """Sin sesión DB, ejecuta nodo sin logging."""

        @logged_node("test_node")
        async def my_node(state):
            return {"result": "ok"}

        state = {}  # Sin _db_session
        result = await my_node(state)
        assert result["result"] == "ok"

    def test_build_input_summary(self):
        """Input summary incluye mensaje e intent."""
        state = {
            "last_user_message": "Necesito agendar una cita",
            "intent": "scheduling",
            "detected_language": "es",
        }
        summary = _build_input_summary(state, "test")
        assert "agendar" in summary
        assert "scheduling" in summary
        assert "es" in summary

    def test_build_output_summary(self):
        """Output summary incluye campos clave del resultado."""
        result = {
            "intent": "greeting",
            "intent_confidence": 0.95,
        }
        summary = _build_output_summary(result, "test")
        assert "greeting" in summary
        assert "0.95" in summary

    def test_extract_details(self):
        """Extract details filtra solo campos relevantes."""
        result = {
            "intent_confidence": 0.95,
            "budget_status": "ok",
            "response": "Hola",  # No es un detail key
            "_tokens_used": 100,  # Prefijo _ no es detail
        }
        details = _extract_details(result, "test")
        assert "intent_confidence" in details
        assert "budget_status" in details
        assert "response" not in details
        assert "_tokens_used" not in details
```

## Criterios de Aceptación

| # | Criterio | Verificación |
|---|---|---|
| 1 | Toda ejecución de nodo crea un AgentActionLog | Enviar mensaje → verificar log entries en DB |
| 2 | Errores se registran con traceback | Provocar error en nodo → verificar log con status='error' |
| 3 | Duración se mide correctamente | Verificar duration_ms > 0 para nodos que hacen I/O |
| 4 | Tokens se registran por nodo | Verificar tokens_used > 0 para nodos que usan LLM |
| 5 | Input/output se truncan a 500 chars | Enviar mensaje largo → verificar truncamiento |
| 6 | RLS aísla logs por tenant | Consultar logs con client_id A → no ver logs de B |
| 7 | Endpoint GET /agent-logs/conversations/{id} funcional | Retorna logs ordenados por created_at desc |
| 8 | Endpoint GET /agent-logs/stats funcional | Retorna estadísticas agregadas por nodo |
| 9 | Endpoint GET /agent-logs/errors funcional | Retorna solo logs con status='error' |
| 10 | RBAC: supervisor puede ver logs de conversación | supervisor → 200, agent → 403 |
| 11 | RBAC: admin puede ver stats y errores | admin → 200, supervisor → 403 |
| 12 | Métricas Prometheus se actualizan | agent_node_duration_seconds registra histograma |
| 13 | Sin sesión DB, nodo ejecuta sin logging | Nodo sin _db_session no falla |
| 14 | Tests unitarios pasan | pytest tests/unit/test_agent_logging.py — all pass |

## Notas Técnicas

- **No serializar `_db_session`**: El campo `_db_session` en ConversationState NO se persiste en el checkpointer de LangGraph. Se inyecta al inicio de cada invocación del grafo y se limpia después. El prefijo `_` indica que es efímero.
- **Performance**: El logging es async y no bloquea la ejecución del nodo. Los INSERT son batched al final de la transacción. Para alto throughput, considerar buffer en Redis + flush periódico con Celery.
- **Retención**: Los logs de agentes crecen rápido. Implementar limpieza automática: retener 30 días de logs detallados, 1 año de estadísticas agregadas. Celery Beat task `cleanup_agent_logs` semanal.
- **Dashboard Grafana**: Crear dashboard "Agent Performance" con paneles: latencia por nodo (p50, p95, p99), throughput (acciones/min), error rate por nodo, distribución de intents, tokens consumidos por modelo.
- **Correlación con traces**: El `trace_id` de OpenTelemetry se incluye en el Loguru log para correlación cruzada entre logs de agentes, traces de API y métricas de Prometheus.

## Dependencias

- Reutiliza todas las dependencias de Sprint 6 (LangGraph, LangChain)
- Sin dependencias adicionales para el logging
