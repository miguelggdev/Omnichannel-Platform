"""Tarea de Celery Beat que avanza las secuencias de follow-up (Sprint 18, Dev B).

`app.tasks.bulk_process_lead_sequences` corre cada minuto en la cola `bulk` (routing
`app.tasks.bulk_*`): envia mensajes a miles de leads, y en la cola de los mensajes entrantes
dejaria sin atender a los clientes reales (misma razon que las campanas, `campaign_tasks.py`).

Recorre los tenants activos que tienen el modulo de leads y llama a
`lead_sequence_runner.procesar_tenant()` en cada uno. Un tenant que falla no detiene a los demas.

**Un solo pase a la vez:** una pasada con muchos mensajes puede durar mas de un minuto y Beat
encolaria otra encima. Una marca en Redis (`SET NX EX`) hace que la segunda no haga nada. Si Redis
no responde la pasada sigue: lo que impide enviar dos veces es el bloqueo de fila
(`FOR UPDATE SKIP LOCKED`), no la marca.
"""

import logging
from typing import Any
from uuid import UUID

from celery import shared_task
from sqlalchemy import text

from app.core.database import run_isolated, tenant_session
from app.services.dedup import get_redis
from app.services.lead_sequence_runner import ResumenPasada, procesar_tenant
from app.tasks.auto_close import _load_active_client_ids

logger = logging.getLogger(__name__)

#: Clave y vida maxima de la marca de "hay una pasada en curso" (caduca sola si el worker muere).
CLAVE_PASADA = "lead-sequences:running"
VIDA_MARCA_SEGUNDOS = 600


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


async def _tomar_marca() -> bool:
    """Intenta reservar la pasada; `True` si es nuestra (o si Redis no responde)."""
    try:
        return bool(await get_redis().set(CLAVE_PASADA, "1", nx=True, ex=VIDA_MARCA_SEGUNDOS))
    except Exception:
        logger.warning("Redis no responde: la pasada de secuencias sigue sin marca")
        return True


async def _soltar_marca() -> None:
    """Libera la reserva; si falla, caduca sola."""
    try:
        await get_redis().delete(CLAVE_PASADA)
    except Exception:
        logger.warning("No se pudo soltar la marca de la pasada de secuencias")


async def procesar_todos() -> dict[str, Any]:
    """Una pasada sobre todos los tenants con el modulo de leads.

    Returns:
        Resumen sin datos personales: `skipped` si ya habia otra pasada, o `tenants`,
        `processed` y `by_result`.
    """
    if not await _tomar_marca():
        return {"skipped": True}
    try:
        total = ResumenPasada()
        tenants = 0
        for client_id in await _load_active_client_ids():
            try:
                if not await _tenant_con_leads(client_id):
                    continue
                resumen = await procesar_tenant(client_id)
            except Exception:
                logger.exception("Fallo procesando secuencias del tenant %s", client_id)
                continue
            tenants += 1
            total.procesadas += resumen.procesadas
            for clave, cuenta in resumen.por_resultado.items():
                total.por_resultado[clave] = total.por_resultado.get(clave, 0) + cuenta
        if total.procesadas:
            logger.info(
                "Secuencias: %s inscripciones en %s tenant(s): %s",
                total.procesadas,
                tenants,
                total.por_resultado,
            )
        return {
            "skipped": False,
            "tenants": tenants,
            "processed": total.procesadas,
            "by_result": total.por_resultado,
        }
    finally:
        await _soltar_marca()


@shared_task(
    name="app.tasks.bulk_process_lead_sequences",
    acks_late=True,
    time_limit=900,
    soft_time_limit=840,
)
def process_lead_sequences() -> dict[str, Any]:
    """Avanza las inscripciones vencidas de todos los tenants (la lanza Beat cada minuto).

    Returns:
        El resumen de `procesar_todos()`.
    """
    return run_isolated(procesar_todos())
