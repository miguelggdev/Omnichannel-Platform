"""Pre-calculo diario de la analitica de leads (Sprint 19, Dev B).

`app.tasks.bulk_precompute_lead_analytics` corre a diario (06:00 UTC, cola `bulk`) y deja en Redis
las cuatro analiticas del rango por defecto de cada tenant con el modulo de leads
(`lead_analytics.precalcular_tenant`). Un tenant que falla no detiene a los demas, y una marca en
Redis evita dos pasadas a la vez.
"""

import logging
from typing import Any
from uuid import UUID

from celery import shared_task
from sqlalchemy import text

from app.core.database import run_isolated, tenant_session
from app.services.dedup import get_redis
from app.services.lead_analytics import precalcular_tenant
from app.tasks.auto_close import _load_active_client_ids

logger = logging.getLogger(__name__)

CLAVE_PASADA = "lead-analytics:running"
VIDA_MARCA_SEGUNDOS = 1800


async def _tenant_con_leads(client_id: UUID) -> bool:
    """Si el tenant tiene el modulo de leads activo."""
    async with tenant_session(client_id) as session:
        activo = (
            await session.execute(
                text("SELECT lead_management_enabled FROM clients WHERE id = :cid"),
                {"cid": str(client_id)},
            )
        ).scalar_one_or_none()
    return bool(activo)


async def precalcular_todos() -> dict[str, Any]:
    """Una pasada de pre-calculo sobre todos los tenants con el modulo de leads.

    Returns:
        `skipped` si ya habia otra pasada; si no, `tenants` y `computed`.
    """
    redis = get_redis()
    try:
        if not await redis.set(CLAVE_PASADA, "1", nx=True, ex=VIDA_MARCA_SEGUNDOS):
            return {"skipped": True}
    except Exception:
        logger.warning("Redis no responde: no hay donde guardar el pre-calculo de analitica")
        return {"skipped": True, "redis": False}
    try:
        tenants = calculadas = 0
        for client_id in await _load_active_client_ids():
            try:
                if not await _tenant_con_leads(client_id):
                    continue
                calculadas += await precalcular_tenant(client_id)
            except Exception:
                logger.exception("Fallo precalculando la analitica del tenant %s", client_id)
                continue
            tenants += 1
        return {"skipped": False, "tenants": tenants, "computed": calculadas}
    finally:
        try:
            await redis.delete(CLAVE_PASADA)
        except Exception:
            logger.warning("No se pudo soltar la marca del pre-calculo de analitica")


@shared_task(
    name="app.tasks.bulk_precompute_lead_analytics",
    acks_late=True,
    time_limit=1500,
    soft_time_limit=1400,
)
def precompute_lead_analytics() -> dict[str, Any]:
    """Pre-calcula la analitica de leads de todos los tenants (la lanza Beat a diario).

    Returns:
        El resumen de `precalcular_todos()`.
    """
    return run_isolated(precalcular_todos())
