"""AgentLogger — registra y consulta la actividad de los nodos del grafo.

Desviaciones sobre `specs/sprint-07-addendum-agent-logging.md` §3:

- `logging` estandar en vez de `loguru`: pese a que CLAUDE.md lista Loguru en
  el stack, ningun modulo de este proyecto lo usa en la practica (todos hacen
  `logging.getLogger(__name__)` — `webhook_processor.py`, `contacts.py`,
  `graph.py`, etc.); seguir esa convencion real es mas consistente que la
  tabla de stack.
- `datetime.now(timezone.utc)` en vez de `datetime.utcnow()` (deprecado desde
  Python 3.12, y el resto del proyecto ya usa datetimes con tz).
- El parametro del constructor se llama `session`, no `db`: mismo nombre que
  `ConversationLifecycle`/`ContactUnifier`.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_action_log import AgentActionLog

logger = logging.getLogger(__name__)


class AgentLogger:
    """Registra y consulta la actividad (`AgentActionLog`) de un tenant.

    Attributes:
        session: Sesión con el contexto de tenant ya aplicado.
        client_id: Tenant dueño de los logs que este logger escribe/lee.
    """

    MAX_SUMMARY_LENGTH = 500

    def __init__(self, session: AsyncSession, client_id: UUID) -> None:
        """Guarda la sesión de trabajo y el tenant.

        Args:
            session: `AsyncSession` con contexto de tenant.
            client_id: Tenant al que pertenecen los logs.
        """
        self.session = session
        self.client_id = client_id

    def _truncate(self, text: str | None) -> str | None:
        """Trunca un texto a `MAX_SUMMARY_LENGTH` caracteres.

        Args:
            text: Texto a truncar, o `None`.

        Returns:
            El texto tal cual si cabe, truncado con `...` si no, o `None`.
        """
        if text and len(text) > self.MAX_SUMMARY_LENGTH:
            return text[: self.MAX_SUMMARY_LENGTH] + "..."
        return text

    async def log_action(
        self,
        conversation_id: UUID,
        node_name: str,
        action_type: str,
        *,
        message_id: UUID | None = None,
        input_summary: str | None = None,
        output_summary: str | None = None,
        details: dict[str, Any] | None = None,
        duration_ms: int | None = None,
        tokens_used: int = 0,
        model_used: str | None = None,
        status: str = "success",
        error_message: str | None = None,
    ) -> AgentActionLog:
        """Registra una acción de un nodo del grafo.

        Args:
            conversation_id: Conversación en la que corrió el nodo.
            node_name: Nombre del nodo (`intent_routing`, `rag_query`, ...).
            action_type: Tipo de acción (`decision`, `query`, `response`,
                `tool_call`, `error`, `handoff`).
            message_id: Mensaje que disparó el turno, si se conoce.
            input_summary: Resumen del input al nodo (se trunca solo).
            output_summary: Resumen del output del nodo (se trunca solo).
            details: Detalles adicionales para el JSONB.
            duration_ms: Duración de ejecución del nodo en milisegundos.
            tokens_used: Tokens consumidos por el nodo.
            model_used: Modelo de LLM usado, si aplica.
            status: `success`, `error`, `skipped` o `timeout`.
            error_message: Mensaje de error si `status == "error"`.

        Returns:
            La entrada de log creada (todavía no tiene `id`/`created_at`
            hasta que la transacción haga flush o commit).
        """
        entrada = AgentActionLog(
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
        self.session.add(entrada)

        log_method = logger.error if status == "error" else logger.info
        log_method(
            "Agent action %s.%s [%s] tenant=%s conversation=%s (%sms)",
            node_name,
            action_type,
            status,
            self.client_id,
            conversation_id,
            duration_ms or 0,
        )

        return entrada

    async def get_conversation_log(
        self,
        conversation_id: UUID,
        *,
        limit: int = 100,
        offset: int = 0,
        node_name: str | None = None,
        status: str | None = None,
    ) -> list[AgentActionLog]:
        """Obtiene los logs de una conversación, más recientes primero.

        Args:
            conversation_id: Conversación a consultar.
            limit: Máximo de registros a devolver.
            offset: Offset para paginación.
            node_name: Filtra por nombre de nodo, si se da.
            status: Filtra por estado, si se da.

        Returns:
            Las entradas de log que matchean, ordenadas por `created_at` desc.
        """
        stmt = (
            select(AgentActionLog)
            .where(AgentActionLog.client_id == self.client_id)
            .where(AgentActionLog.conversation_id == conversation_id)
            .order_by(AgentActionLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if node_name:
            stmt = stmt.where(AgentActionLog.node_name == node_name)
        if status:
            stmt = stmt.where(AgentActionLog.status == status)

        return list((await self.session.execute(stmt)).scalars().all())

    async def get_node_stats(self, *, hours: int = 24) -> list[dict[str, Any]]:
        """Calcula estadísticas de rendimiento por nodo en una ventana de tiempo.

        Args:
            hours: Ventana de tiempo, en horas, hacia atrás desde ahora.

        Returns:
            Un dict por nodo con total de acciones, duración promedio, tokens
            totales, conteo de errores y tasa de error (%).
        """
        corte = datetime.now(timezone.utc) - timedelta(hours=hours)
        stmt = (
            select(
                AgentActionLog.node_name,
                func.count().label("total_actions"),
                func.avg(AgentActionLog.duration_ms).label("avg_duration_ms"),
                func.sum(AgentActionLog.tokens_used).label("total_tokens"),
                func.count().filter(AgentActionLog.status == "error").label("error_count"),
            )
            .where(AgentActionLog.client_id == self.client_id)
            .where(AgentActionLog.created_at >= corte)
            .group_by(AgentActionLog.node_name)
        )

        filas = (await self.session.execute(stmt)).all()
        return [
            {
                "node_name": fila.node_name,
                "total_actions": fila.total_actions,
                "avg_duration_ms": round(float(fila.avg_duration_ms or 0), 2),
                "total_tokens": fila.total_tokens or 0,
                "error_count": fila.error_count,
                "error_rate": round(
                    (fila.error_count / fila.total_actions * 100) if fila.total_actions > 0 else 0,
                    2,
                ),
            }
            for fila in filas
        ]
