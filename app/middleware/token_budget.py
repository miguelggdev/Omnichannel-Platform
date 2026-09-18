"""TokenBudgetGuard — registro de consumo de tokens (ADR-004, Sprint 6).

Contrato: `specs/sprint-06-langgraph.md` §7. Lo llaman los nodos que invocan al
LLM (`intent_router`, `rag_query`) despues de cada respuesta.

Cada registro hace tres cosas, en este orden:

1. Escribe una fila en `token_usage_logs` (detalle por llamada).
2. Suma el consumo a `token_budgets` del mes en curso, si el tenant tiene fila.
3. Invalida la cache de Redis que lee `token_budget_check_node`, para que el
   siguiente mensaje vea el consumo real y no el de hasta 60s atras.

El registro es best-effort a proposito: un fallo escribiendo la contabilidad no
puede tumbar una conversacion que el contacto ya recibio. Los errores se loguean
(y se pierde exactitud en el presupuesto), no se propagan.

Desviaciones sobre la spec:

- `token_usage_logs` no tiene columna `cost_usd`. `estimate_cost_usd()` se
  conserva porque el costo aparece en el log de la llamada y lo consumira el
  dashboard de Grafana de Sprint 8, pero no se persiste: inventar una columna
  aqui obligaria a una migracion que no es de este sprint.
- `token_budgets` se busca por `month` (YYYY-MM, UTC), no por
  `period_start`/`period_end`.
- Desaparece el `check_budget()` del placeholder de Sprint 3: la verificacion es
  ahora del nodo `token_budget_check_node`, que es quien conoce el estado del
  grafo. Nadie lo usaba.
"""

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import update

from app.agents.nodes.token_budget import cache_key
from app.core.database import tenant_session
from app.core.metrics import record_tokens
from app.models.token_budget import TokenBudget
from app.models.token_usage_log import TokenUsageLog
from app.services.dedup import get_redis

if TYPE_CHECKING:
    from collections.abc import Awaitable

logger = logging.getLogger(__name__)

# Precio por 1M de tokens, en USD. Actualizar cuando cambie el pricing de OpenAI.
PRICING_PER_MILLION: dict[str, dict[str, float]] = {
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
    "text-embedding-3-large": {"input": 0.13, "output": 0.0},
}

# Modelo cuyo precio se usa cuando el modelo no esta en la tabla: el mas barato,
# para no inflar el costo estimado de un modelo desconocido.
FALLBACK_PRICING_MODEL = "gpt-4o-mini"


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estima el costo en USD de una llamada al LLM.

    Args:
        model: Modelo usado en la llamada.
        prompt_tokens: Tokens de entrada.
        completion_tokens: Tokens generados.

    Returns:
        Costo estimado en USD, redondeado a 6 decimales.
    """
    pricing = PRICING_PER_MILLION.get(model, PRICING_PER_MILLION[FALLBACK_PRICING_MODEL])
    costo = (prompt_tokens / 1_000_000) * pricing["input"] + (
        completion_tokens / 1_000_000
    ) * pricing["output"]
    return round(costo, 6)


class TokenBudgetGuard:
    """Contabiliza el consumo de tokens de cada tenant.

    Attributes:
        THRESHOLD_DEGRADED: Porcentaje a partir del cual se degrada el modelo.
        THRESHOLD_EXCEEDED: Porcentaje a partir del cual se corta el servicio.
    """

    THRESHOLD_DEGRADED: int = 90
    THRESHOLD_EXCEEDED: int = 100

    @staticmethod
    async def record_usage(
        client_id: str | UUID,
        conversation_id: str | UUID | None,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        operation: str,
    ) -> None:
        """Registra el consumo de una llamada al LLM.

        Args:
            client_id: Tenant que consumio los tokens.
            conversation_id: Conversacion asociada, si la llamada nace de una.
            model: Modelo usado.
            prompt_tokens: Tokens de entrada.
            completion_tokens: Tokens generados.
            operation: Operacion que consumio (`intent_routing`, `rag_query`, ...).
        """
        total_tokens = prompt_tokens + completion_tokens
        if total_tokens <= 0:
            # Sin datos de consumo no hay nada que contabilizar: registrar un 0
            # solo ensucia `token_usage_logs` sin aportar informacion.
            logger.debug("Llamada a %s sin datos de consumo; no se registra", model)
            return

        tenant_id = UUID(str(client_id))
        costo = estimate_cost_usd(model, prompt_tokens, completion_tokens)

        try:
            async with tenant_session(tenant_id) as session:
                session.add(
                    TokenUsageLog(
                        client_id=tenant_id,
                        conversation_id=UUID(str(conversation_id)) if conversation_id else None,
                        model=model,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                        operation=operation,
                    )
                )

                # UPDATE atomico (BUG-022): un SELECT + `budget.used_tokens = ... + n`
                # en Python es un lost update bajo concurrencia -- dos mensajes del
                # mismo tenant procesados en paralelo (ai_inference corre con
                # concurrency=2) pueden leer el mismo valor y el commit que llega
                # ultimo pisa al otro, subcontando el consumo real. El incremento
                # tiene que pasar dentro de la sentencia SQL, no en Python.
                month = datetime.now(timezone.utc).strftime("%Y-%m")
                await session.execute(
                    update(TokenBudget)
                    .where(TokenBudget.client_id == tenant_id, TokenBudget.month == month)
                    .values(used_tokens=TokenBudget.used_tokens + total_tokens)
                )
        except Exception:
            logger.exception(
                "No se pudo registrar el consumo de tokens (client_id=%s, model=%s, total=%s)",
                tenant_id,
                model,
                total_tokens,
            )
            return

        # La metrica se registra despues del commit y no antes: contar tokens
        # que no llegaron a `token_usage_logs` dejaria el dashboard por encima
        # del consumo real facturado.
        record_tokens(
            client_id=str(tenant_id),
            model=model,
            operation=operation,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=costo,
        )

        logger.info(
            "Consumo registrado: client_id=%s model=%s operation=%s tokens=%s costo_usd=%s",
            tenant_id,
            model,
            operation,
            total_tokens,
            costo,
        )
        await TokenBudgetGuard._invalidate_cache(tenant_id)

    @staticmethod
    async def _invalidate_cache(client_id: UUID) -> None:
        """Borra la cache de presupuesto del tenant.

        Args:
            client_id: Tenant cuya cache hay que invalidar.
        """
        try:
            await cast("Awaitable[Any]", get_redis().delete(cache_key(client_id)))
        except Exception:
            # La cache expira sola en 60s; no vale la pena fallar por esto.
            logger.warning("No se pudo invalidar la cache de presupuesto de %s", client_id)


token_budget_guard = TokenBudgetGuard()
