"""TokenBudgetGuard — Placeholder para Sprint 6.

Verifica el presupuesto de tokens del tenant antes de procesar con IA.
Implementación completa en Sprint 6 (integración con LangGraph).

Niveles (ADR-004):
    - < 90%: ok (usa modelo configurado del tenant)
    - 90-99%: degraded (cambia a gpt-4o-mini)
    - >= 100%: exceeded (rechaza y hace handoff a humano)
"""

from typing import Any
from uuid import UUID


class TokenBudgetGuard:
    """Verifica el presupuesto de tokens del tenant.

    Placeholder: siempre retorna 'ok'. La implementación real consultará
    Redis para obtener el uso acumulado y aplicará los umbrales de ADR-004.

    Attributes:
        THRESHOLD_DEGRADED: Porcentaje a partir del cual se degrada el modelo.
        THRESHOLD_EXCEEDED: Porcentaje a partir del cual se corta el servicio.
    """

    THRESHOLD_DEGRADED: int = 90
    THRESHOLD_EXCEEDED: int = 100

    async def check_budget(self, client_id: UUID) -> dict[str, Any]:
        """Verifica el presupuesto de tokens para un tenant.

        Args:
            client_id: UUID del tenant a verificar.

        Returns:
            Dict con status ('ok', 'degraded', 'exceeded'),
            usage_pct (porcentaje de uso) y model_to_use.
        """
        # Placeholder: siempre retorna ok
        # Sprint 6: consultar Redis para uso acumulado
        return {
            "status": "ok",
            "usage_pct": 0,
            "model_to_use": "gpt-4o",
        }


token_budget_guard = TokenBudgetGuard()
