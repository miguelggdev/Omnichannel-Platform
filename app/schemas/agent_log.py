"""Schemas de AgentActionLog — consulta de la actividad de los nodos del grafo."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AgentActionLogResponse(BaseModel):
    """Una accion registrada por un nodo del grafo."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    message_id: UUID | None = None
    node_name: str
    action_type: str
    input_summary: str | None = None
    output_summary: str | None = None
    details: dict[str, Any] = {}
    duration_ms: int | None = None
    tokens_used: int = 0
    model_used: str | None = None
    status: str
    error_message: str | None = None
    created_at: datetime


class NodeStatsResponse(BaseModel):
    """Rendimiento agregado de un nodo en una ventana de tiempo."""

    node_name: str
    total_actions: int
    avg_duration_ms: float
    total_tokens: int
    error_count: int
    error_rate: float
