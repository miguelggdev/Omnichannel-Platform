"""Nodo de control de presupuesto de tokens (ADR-004).

Es el punto de entrada del grafo: corre antes que el router de intents para que
un tenant sin presupuesto no gaste ni la llamada de clasificacion.

Tres niveles de degradacion, medidos sobre `token_budgets` del mes en curso:

- `< 90%` -> `ok`: se usa el modelo configurado por el tenant.
- `90-99%` -> `degraded`: se baja a `OPENAI_FALLBACK_MODEL` (gpt-4o-mini).
- `>= 100%` -> `exceeded`: handoff a humano sin invocar al LLM.

El uso se cachea en Redis 60s (`token_budget:{client_id}`) para no consultar la
base en cada mensaje. La cache es una optimizacion, no una dependencia: si Redis
no responde, se consulta la base y se sigue. `TokenBudgetGuard.record_usage()`
invalida la clave despues de cada registro de consumo.

Desviaciones sobre `specs/sprint-06-langgraph.md` §6 (el modelo de Sprint 1 no
tiene las columnas que asume la spec):

- `token_budgets` no tiene `period_start`/`period_end`/`max_tokens`: tiene
  `month` (YYYY-MM) y `total_budget`. El periodo es el mes calendario UTC.
- El modelo del tenant sale de `agent_configs.model` (via `get_agent_settings`),
  no de un `agent_configs.model_name` por `agent_type`.
"""

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select

from app.agents.nodes._state import ConversationState
from app.agents.nodes._tenant import get_agent_settings
from app.core.config import get_settings
from app.core.database import tenant_session
from app.models.token_budget import TokenBudget
from app.services.dedup import get_redis

if TYPE_CHECKING:
    from collections.abc import Awaitable

logger = logging.getLogger(__name__)

# Prefijo de la clave de cache. `TokenBudgetGuard.record_usage()` borra la misma
# clave: si cambia aqui, cambia alla (por eso el helper `cache_key()`).
CACHE_KEY_PREFIX = "token_budget:"
CACHE_TTL_SECONDS = 60

# Umbrales de ADR-004, en porcentaje de presupuesto consumido.
THRESHOLD_DEGRADED_PCT = 90.0
THRESHOLD_EXCEEDED_PCT = 100.0

# Presupuesto asumido cuando el tenant no tiene fila del mes: sin limite. Un
# tenant sin presupuesto configurado no debe quedarse sin servicio; el corte es
# una decision explicita de quien crea la fila en `token_budgets`.
UNLIMITED_BUDGET = 0


def cache_key(client_id: str | UUID) -> str:
    """Devuelve la clave de Redis donde se cachea el uso de un tenant.

    Args:
        client_id: Tenant del que se quiere la clave.

    Returns:
        Clave completa, con prefijo.
    """
    return f"{CACHE_KEY_PREFIX}{client_id}"


async def _read_cache(client_id: str) -> dict[str, int] | None:
    """Lee el uso cacheado en Redis.

    Args:
        client_id: Tenant a consultar.

    Returns:
        Dict con `total_budget` y `used_tokens`, o None si no hay cache o si
        Redis no responde.
    """
    try:
        crudo = await cast("Awaitable[str | None]", get_redis().get(cache_key(client_id)))
    except Exception:
        logger.warning("Redis no disponible al leer el presupuesto; se consulta la base")
        return None

    if not crudo:
        return None
    try:
        datos = json.loads(crudo)
    except json.JSONDecodeError:
        logger.warning("Cache de presupuesto corrupta para %s; se ignora", client_id)
        return None
    return {
        "total_budget": int(datos.get("total_budget", UNLIMITED_BUDGET)),
        "used_tokens": int(datos.get("used_tokens", 0)),
    }


async def _write_cache(client_id: str, usage: dict[str, int]) -> None:
    """Cachea el uso de tokens de un tenant durante `CACHE_TTL_SECONDS`.

    Args:
        client_id: Tenant a cachear.
        usage: Dict con `total_budget` y `used_tokens`.
    """
    try:
        await cast(
            "Awaitable[Any]",
            get_redis().set(cache_key(client_id), json.dumps(usage), ex=CACHE_TTL_SECONDS),
        )
    except Exception:
        logger.warning("Redis no disponible al cachear el presupuesto de %s", client_id)


async def _get_token_usage(client_id: str) -> dict[str, int]:
    """Obtiene el consumo de tokens del mes en curso.

    Args:
        client_id: Tenant a consultar.

    Returns:
        Dict con `total_budget` (0 = sin limite) y `used_tokens`.
    """
    cacheado = await _read_cache(client_id)
    if cacheado is not None:
        return cacheado

    month = datetime.now(timezone.utc).strftime("%Y-%m")
    async with tenant_session(UUID(client_id)) as session:
        # client_id explicito ademas de RLS (BUG-020/022, ver MEMORY.md): mismo
        # criterio que el resto del proyecto para no depender solo de la politica.
        stmt = select(TokenBudget).where(
            TokenBudget.client_id == UUID(client_id), TokenBudget.month == month
        )
        budget = (await session.execute(stmt)).scalar_one_or_none()
        usage = (
            {"total_budget": UNLIMITED_BUDGET, "used_tokens": 0}
            if budget is None
            else {
                "total_budget": int(budget.total_budget),
                "used_tokens": int(budget.used_tokens),
            }
        )

    await _write_cache(client_id, usage)
    return usage


def _usage_pct(usage: dict[str, int]) -> float:
    """Calcula el porcentaje de presupuesto consumido.

    Args:
        usage: Dict con `total_budget` y `used_tokens`.

    Returns:
        Porcentaje consumido; 0.0 si el tenant no tiene limite.
    """
    total = usage.get("total_budget", UNLIMITED_BUDGET)
    if total <= 0:
        return 0.0
    return usage.get("used_tokens", 0) / total * 100


async def token_budget_check_node(state: ConversationState) -> dict[str, Any]:
    """Decide con que modelo seguir, o si hay que escalar por presupuesto agotado.

    Args:
        state: Estado del grafo; usa `client_id`.

    Returns:
        Dict parcial con `budget_status`, `model_to_use`, `budget_usage_pct` y
        `requires_handoff` (con `handoff_reason` si el presupuesto se agoto).
    """
    client_id = state["client_id"]

    usage = await _get_token_usage(client_id)
    usage_pct = _usage_pct(usage)
    settings = await get_agent_settings(UUID(client_id))
    tenant_model = settings.model or get_settings().OPENAI_CHAT_MODEL

    if usage_pct >= THRESHOLD_EXCEEDED_PCT:
        logger.warning(
            "Presupuesto agotado para %s (%.1f%%): handoff sin invocar al LLM",
            client_id,
            usage_pct,
        )
        return {
            "budget_status": "exceeded",
            "model_to_use": tenant_model,
            "budget_usage_pct": usage_pct,
            "requires_handoff": True,
            "handoff_reason": "budget_exceeded",
        }

    if usage_pct >= THRESHOLD_DEGRADED_PCT:
        degraded_model = get_settings().OPENAI_FALLBACK_MODEL
        logger.info(
            "Presupuesto al %.1f%% para %s: se degrada a %s", usage_pct, client_id, degraded_model
        )
        return {
            "budget_status": "degraded",
            "model_to_use": degraded_model,
            "budget_usage_pct": usage_pct,
            "requires_handoff": False,
        }

    return {
        "budget_status": "ok",
        "model_to_use": tenant_model,
        "budget_usage_pct": usage_pct,
        "requires_handoff": False,
    }
