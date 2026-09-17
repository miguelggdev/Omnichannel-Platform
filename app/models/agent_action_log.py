"""Modelo AgentActionLog — traza de lo que hizo cada nodo del grafo, por turno.

Contrato exacto (`app/api/v1/agent_logs.py`, `app/schemas/agent_log.py` y
`tests/unit/test_agent_logging.py::AgentActionLogDoble`, ya entregados por
Dev B en el Sprint 7): tabla `agent_action_logs` con estas columnas. `status` y
`tokens_used` son nullable con default a nivel de columna, no `NOT NULL`
-- coincide con `specs/sprint-07-addendum-agent-logging.md` §1.
"""

from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel


class AgentActionLog(TenantBaseModel):
    """Una acción de un nodo del grafo en un turno de conversación.

    Attributes:
        conversation_id: Conversación en la que corrió el nodo.
        message_id: Mensaje que disparó el turno, si se conoce.
        node_name: Nombre del nodo (`intent_routing`, `rag_query`, ...).
        action_type: Tipo de acción (`decision`, `query`, `response`,
            `tool_call`, `error`, `handoff`).
        input_summary: Resumen del input al nodo, truncado.
        output_summary: Resumen del output del nodo, truncado.
        details: Detalles estructurados (modelo, tokens, confianza, ...).
        duration_ms: Duración de ejecución del nodo en milisegundos.
        tokens_used: Tokens consumidos por el nodo, si invocó un LLM.
        model_used: Modelo de LLM usado, si aplica.
        status: `success`, `error`, `skipped` o `timeout`.
        error_message: Mensaje de error si `status == "error"`.
    """

    __tablename__ = "agent_action_logs"
    __table_args__ = (
        Index("idx_agent_action_logs_created_at", "created_at"),
        Index(
            "idx_agent_action_logs_error_status",
            "status",
            postgresql_where=text("status = 'error'"),
        ),
    )

    client_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False, index=True
    )
    conversation_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False, index=True
    )
    message_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id"), nullable=True
    )
    node_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    input_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_used: Mapped[int | None] = mapped_column(Integer, server_default="0")
    model_used: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str | None] = mapped_column(String(20), server_default="success")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
