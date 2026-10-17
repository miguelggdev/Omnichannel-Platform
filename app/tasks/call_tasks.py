"""Tareas de Celery Beat de las llamadas agendadas (Sprint 19, Dev B).

Las dos van en la cola `bulk` (routing `app.tasks.bulk_*`):

- `app.tasks.bulk_send_call_reminders` (cada 5 min): recordatorios de 24 h y de 1 h a los leads
  (`call_notifications.procesar_recordatorios`). El de 1 h necesita esa granularidad.
- `app.tasks.bulk_start_due_ai_calls` (cada minuto): inicia, a su hora, las llamadas con voz IA.
  **Apagada por defecto** (`VOICE_AI_AUTO_START`): llamar a una persona sin que nadie lo pida en
  ese momento es una decision que se toma con conciencia. Solo las que vencieron hace menos de
  `VOICE_AI_START_GRACE_MINUTES` (una llamada de ayer ya no se hace), y solo en el tenant del canal
  de voz (`DEFAULT_CLIENT_ID`, ADR-030).

Cada una usa una marca en Redis para no solaparse consigo misma (la fila bloqueada es lo que evita
el doble envio; la marca evita apilar pasadas).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from celery import shared_task
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.database import run_isolated, tenant_session
from app.models.scheduled_call import CALL_AI_VOICE, CALL_CONFIRMED, CALL_PENDING, ScheduledCall
from app.services.call_notifications import procesar_recordatorios
from app.services.dedup import get_redis
from app.services.lead_outreach import datos_del_tenant
from app.services.scheduled_call_voice import iniciar_llamada_ia
from app.services.scheduled_calls import CallError
from app.tasks.auto_close import _load_active_client_ids

logger = logging.getLogger(__name__)

CLAVE_RECORDATORIOS = "call-reminders:running"
CLAVE_VOZ = "call-ai-start:running"
VIDA_MARCA_SEGUNDOS = 600
#: Llamadas de voz IA que se inician por pasada.
MAX_LLAMADAS_POR_PASADA = 20


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


async def _tomar_marca(clave: str) -> bool:
    """Reserva la pasada; `True` si es nuestra (o si Redis no responde)."""
    try:
        return bool(await get_redis().set(clave, "1", nx=True, ex=VIDA_MARCA_SEGUNDOS))
    except Exception:
        logger.warning("Redis no responde: la pasada %s sigue sin marca", clave)
        return True


async def _soltar_marca(clave: str) -> None:
    """Libera la reserva; si falla, caduca sola."""
    try:
        await get_redis().delete(clave)
    except Exception:
        logger.warning("No se pudo soltar la marca %s", clave)


async def enviar_recordatorios_todos() -> dict[str, Any]:
    """Una pasada de recordatorios sobre todos los tenants con el modulo de leads.

    Returns:
        `skipped` si ya habia otra pasada; si no, `tenants`, `processed` y `by_result`.
    """
    if not await _tomar_marca(CLAVE_RECORDATORIOS):
        return {"skipped": True}
    try:
        ahora = datetime.now(timezone.utc)
        tenants = procesados = 0
        por_resultado: dict[str, int] = {}
        for client_id in await _load_active_client_ids():
            try:
                if not await _tenant_con_leads(client_id):
                    continue
                resumen = await procesar_recordatorios(client_id, ahora)
            except Exception:
                logger.exception("Fallo enviando recordatorios del tenant %s", client_id)
                continue
            tenants += 1
            procesados += resumen.procesados
            for clave, cuenta in resumen.por_resultado.items():
                por_resultado[clave] = por_resultado.get(clave, 0) + cuenta
        if procesados:
            logger.info("Recordatorios: %s en %s tenant(s): %s", procesados, tenants, por_resultado)
        return {
            "skipped": False,
            "tenants": tenants,
            "processed": procesados,
            "by_result": por_resultado,
        }
    finally:
        await _soltar_marca(CLAVE_RECORDATORIOS)


async def iniciar_llamadas_ia_vencidas(client_id: UUID, ahora: datetime) -> dict[str, int]:
    """Inicia las llamadas de voz IA de un tenant cuya hora ya llego.

    Args:
        client_id: Tenant.
        ahora: Instante actual.

    Returns:
        Cuantas se iniciaron y cuantas fallaron (sin detalle: puede nombrar a la persona).
    """
    ajustes = get_settings()
    desde = ahora - timedelta(minutes=ajustes.VOICE_AI_START_GRACE_MINUTES)
    async with tenant_session(client_id) as session:
        ids = list(
            (
                await session.execute(
                    select(ScheduledCall.id)
                    .where(
                        ScheduledCall.client_id == client_id,
                        ScheduledCall.call_type == CALL_AI_VOICE,
                        ScheduledCall.status.in_((CALL_PENDING, CALL_CONFIRMED)),
                        ScheduledCall.scheduled_at <= ahora,
                        ScheduledCall.scheduled_at > desde,
                    )
                    .order_by(ScheduledCall.scheduled_at, ScheduledCall.id)
                    .limit(MAX_LLAMADAS_POR_PASADA)
                )
            )
            .scalars()
            .all()
        )
        _, idioma = await datos_del_tenant(session, client_id)
    resultado = {"started": 0, "failed": 0}
    for call_id in ids:
        try:
            await iniciar_llamada_ia(client_id, call_id, ahora=ahora, idioma=idioma, user_id=None)
        except CallError as exc:
            # `already_starting` es normal si otra pasada la estaba iniciando.
            logger.info("Llamada IA %s no iniciada: %s", call_id, exc.code)
            resultado["failed"] += 1
        except Exception:
            logger.exception("Fallo iniciando la llamada IA %s (client_id=%s)", call_id, client_id)
            resultado["failed"] += 1
        else:
            resultado["started"] += 1
    return resultado


async def iniciar_llamadas_ia_todos() -> dict[str, Any]:
    """Una pasada de inicio de llamadas de voz IA (solo si `VOICE_AI_AUTO_START`).

    Returns:
        `disabled` si esta apagada, `skipped` si hay otra pasada; si no, el resumen.
    """
    ajustes = get_settings()
    if not ajustes.VOICE_AI_AUTO_START:
        return {"disabled": True}
    if not await _tomar_marca(CLAVE_VOZ):
        return {"skipped": True}
    try:
        client_id = UUID(str(ajustes.DEFAULT_CLIENT_ID))
        if not await _tenant_con_leads(client_id):
            return {"started": 0, "failed": 0}
        return await iniciar_llamadas_ia_vencidas(client_id, datetime.now(timezone.utc))
    finally:
        await _soltar_marca(CLAVE_VOZ)


@shared_task(
    name="app.tasks.bulk_send_call_reminders",
    acks_late=True,
    time_limit=600,
    soft_time_limit=540,
)
def send_call_reminders() -> dict[str, Any]:
    """Envia los recordatorios de 24 h y 1 h (la lanza Beat cada 5 minutos).

    Returns:
        El resumen de `enviar_recordatorios_todos()`.
    """
    return run_isolated(enviar_recordatorios_todos())


@shared_task(
    name="app.tasks.bulk_start_due_ai_calls",
    acks_late=True,
    time_limit=600,
    soft_time_limit=540,
)
def start_due_ai_calls() -> dict[str, Any]:
    """Inicia las llamadas de voz IA que ya tocan (la lanza Beat cada minuto).

    Returns:
        El resumen de `iniciar_llamadas_ia_todos()`.
    """
    return run_isolated(iniciar_llamadas_ia_todos())
