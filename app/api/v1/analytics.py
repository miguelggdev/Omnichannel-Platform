"""Analitica del dashboard (Sprint 15, fase 2).

GET /api/v1/analytics/dashboard                cifras del inicio
GET /api/v1/analytics/conversations-by-channel conversaciones por canal en un periodo
GET /api/v1/analytics/messages-over-time       mensajes por dia, entrantes y salientes

Todo se calcula con SQL sobre las tablas reales del tenant; nada se inventa ni se
precalcula. Cada consulta lleva `client_id` explicito ademas de la RLS (CLAUDE.md, regla 1).
Los dias se cuentan en UTC: el tenant todavia no tiene una zona horaria que lea la plataforma.
"""

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.schemas.analytics import ChannelCount, DailyMessages, DashboardMetrics

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)

router = APIRouter()

_ROLES = ("super_admin", "admin", "supervisor")

# "Abiertas" = alguien tiene que atenderlas: todos los estados menos resolved y archived.
_CIFRAS = text(
    """
    SELECT
      (SELECT count(*) FROM conversations
        WHERE client_id = :cid AND status IN (
          'new', 'bot_active', 'human_active', 'waiting_human', 'waiting_client'
        )) AS active_conversations,
      (SELECT count(*) FROM conversations
        WHERE client_id = :cid AND status = 'waiting_human') AS waiting_human,
      (SELECT count(*) FROM conversations
        WHERE client_id = :cid AND created_at >= now() - interval '7 days') AS conv_7d,
      (SELECT count(*) FROM conversations
        WHERE client_id = :cid AND created_at >= now() - interval '14 days'
          AND created_at < now() - interval '7 days') AS conv_prev_7d,
      (SELECT count(*) FROM messages
        WHERE client_id = :cid AND created_at >= now() - interval '24 hours') AS msgs_24h,
      (SELECT count(*) FROM messages
        WHERE client_id = :cid AND created_at >= now() - interval '48 hours'
          AND created_at < now() - interval '24 hours') AS msgs_prev_24h,
      (SELECT count(*) FROM contacts
        WHERE client_id = :cid AND merged_into_id IS NULL) AS total_contacts
    """
)

_PRESUPUESTO = text(
    "SELECT total_budget, used_tokens FROM token_budgets "
    "WHERE client_id = :cid AND month = to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM')"
)

# Primera respuesta a cada tanda de mensajes del contacto: para cada mensaje entrante que
# no sigue a otro entrante, el primer saliente posterior de la misma conversacion.
# `min(...) FILTER ... OVER (... ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING)` lo
# calcula en una pasada, sin el JOIN LATERAL por mensaje.
_RESPUESTA = text(
    """
    SELECT avg(extract(epoch FROM (resp - created_at))) AS media,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM (resp - created_at)))
             AS mediana
    FROM (
      SELECT created_at, direction,
             lag(direction) OVER w AS prev_dir,
             min(created_at) FILTER (WHERE direction = 'outbound') OVER (
               PARTITION BY conversation_id ORDER BY created_at
               ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS resp
      FROM messages
      WHERE client_id = :cid AND created_at >= now() - interval '7 days'
      WINDOW w AS (PARTITION BY conversation_id ORDER BY created_at)
    ) t
    WHERE direction = 'inbound' AND prev_dir IS DISTINCT FROM 'inbound' AND resp IS NOT NULL
    """
)

_CSAT = text(
    "SELECT avg(rating) AS media, count(rating) AS respuestas FROM satisfaction_surveys "
    "WHERE client_id = :cid AND rating IS NOT NULL AND responded_at >= now() - interval '30 days'"
)

_POR_CANAL = text(
    "SELECT channel, count(*) AS total FROM conversations "
    "WHERE client_id = :cid AND created_at >= now() - make_interval(days => :days) "
    "GROUP BY channel ORDER BY total DESC, channel"
)

# generate_series rellena los dias sin mensajes con ceros: la grafica no se salta dias.
_POR_DIA = text(
    """
    SELECT d::date AS dia,
           count(m.id) FILTER (WHERE m.direction = 'inbound') AS entrantes,
           count(m.id) FILTER (WHERE m.direction = 'outbound') AS salientes
    FROM generate_series(
           (now() AT TIME ZONE 'UTC')::date - (CAST(:days AS int) - 1),
           (now() AT TIME ZONE 'UTC')::date,
           interval '1 day') AS d
    LEFT JOIN messages m
      ON m.client_id = :cid AND (m.created_at AT TIME ZONE 'UTC')::date = d::date
    GROUP BY d ORDER BY d
    """
)


def _tendencia(actual: int, anterior: int) -> float | None:
    """Variacion porcentual entre dos periodos; `None` si el anterior estaba vacio."""
    if anterior == 0:
        return None
    return round((actual - anterior) / anterior * 100, 1)


@router.get("/dashboard", response_model=DashboardMetrics)
async def dashboard(user: dict[str, Any] = Depends(require_role(*_ROLES))) -> DashboardMetrics:
    """Cifras del inicio del panel.

    Args:
        user: Usuario autenticado; supervisor, admin o super_admin.

    Returns:
        Las metricas del tenant; las que no tienen datos devuelven `None`.
    """
    client_id: UUID = user["client_id"]
    params = {"cid": client_id}
    async with tenant_session(client_id) as session:
        cifras = (await session.execute(_CIFRAS, params)).mappings().one()
        presupuesto = (await session.execute(_PRESUPUESTO, params)).mappings().one_or_none()
        respuesta = (await session.execute(_RESPUESTA, params)).mappings().one()
        csat = (await session.execute(_CSAT, params)).mappings().one()

    uso: float | None = None
    # total_budget = 0 es "sin limite" (UNLIMITED_BUDGET): no hay un porcentaje que dar.
    if presupuesto is not None and presupuesto["total_budget"] > 0:
        uso = round(presupuesto["used_tokens"] / presupuesto["total_budget"] * 100, 1)

    return DashboardMetrics(
        active_conversations=cifras["active_conversations"],
        waiting_human=cifras["waiting_human"],
        conversations_last_7_days=cifras["conv_7d"],
        conversations_trend=_tendencia(cifras["conv_7d"], cifras["conv_prev_7d"]),
        messages_last_24h=cifras["msgs_24h"],
        messages_trend=_tendencia(cifras["msgs_24h"], cifras["msgs_prev_24h"]),
        token_usage_percentage=uso,
        avg_response_time_seconds=(
            round(float(respuesta["media"]), 1) if respuesta["media"] is not None else None
        ),
        median_response_time_seconds=(
            round(float(respuesta["mediana"]), 1) if respuesta["mediana"] is not None else None
        ),
        csat_score=round(float(csat["media"]), 2) if csat["media"] is not None else None,
        csat_responses=csat["respuestas"],
        total_contacts=cifras["total_contacts"],
    )


@router.get("/conversations-by-channel", response_model=list[ChannelCount])
async def conversations_by_channel(
    days: int = Query(default=30, ge=1, le=365, description="Ventana en dias"),
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> list[ChannelCount]:
    """Conversaciones creadas por canal en los ultimos `days` dias, de mas a menos.

    Args:
        days: Ventana a contar (1-365).
        user: Usuario autenticado; supervisor, admin o super_admin.

    Returns:
        Una entrada por canal con actividad; los canales sin conversaciones no salen.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        filas = (
            (await session.execute(_POR_CANAL, {"cid": client_id, "days": days})).mappings().all()
        )
    return [ChannelCount(channel=f["channel"], count=f["total"]) for f in filas]


@router.get("/messages-over-time", response_model=list[DailyMessages])
async def messages_over_time(
    days: int = Query(default=7, ge=1, le=90, description="Dias hasta hoy, incluido"),
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> list[DailyMessages]:
    """Mensajes por dia (UTC) de los ultimos `days` dias, con ceros en los dias vacios.

    Args:
        days: Cuantos dias, contando hoy (1-90).
        user: Usuario autenticado; supervisor, admin o super_admin.

    Returns:
        Exactamente `days` entradas, de la mas antigua a hoy.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        filas = (await session.execute(_POR_DIA, {"cid": client_id, "days": days})).mappings().all()
    return [
        DailyMessages(date=f["dia"].isoformat(), inbound=f["entrantes"], outbound=f["salientes"])
        for f in filas
    ]
