"""Tarea de enriquecimiento de leads (Sprint 17, Dev B).

`app.tasks.enrichment_enrich_lead` cae sola en la cola `lead_enrichment` (routing
`app.tasks.enrichment_*`, ADR-022). Hace el recorrido completo en UNA transaccion del tenant:
enriquece (`enriquecer_lead`), recalcula el FIT con el disparador `enrichment` y evalua la
calificacion automatica. Si algun proveedor fallo de forma pasajera, lo ya guardado se conserva
(commit) y la tarea se reintenta: el motor no pisa lo escrito, asi que repetir es seguro.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from celery import shared_task
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import run_isolated, tenant_session
from app.models.lead import Lead
from app.models.lead_score import TRIGGER_ENRICHMENT, TRIGGER_ICP_CHANGED
from app.services.enrichment.cache import CompanyCache
from app.services.enrichment.engine import enriquecer_lead
from app.services.enrichment.registry import UnknownProviderError, build_providers
from app.services.lead_qualification import calificar_automaticamente
from app.services.lead_score_service import icp_y_pesos_del_tenant, recalcular_fit

logger = logging.getLogger(__name__)

ESTADO_OK = "ok"
ESTADO_SIN_LEAD = "lead_not_found"
ESTADO_SIN_PROVEEDORES = "no_providers"
ESTADO_CONFIG_INVALIDA = "invalid_config"

#: Espera base entre reintentos cuando el proveedor no dijo cuanto esperar (se duplica).
ESPERA_BASE_SEGUNDOS = 60
ESPERA_MAXIMA_SEGUNDOS = 3600


def espera_de_reintento(intento: int, retry_after: int | None) -> int:
    """Segundos hasta el siguiente intento.

    Args:
        intento: Reintentos ya hechos (0 en el primero).
        retry_after: Lo que pidio esperar el proveedor, si lo dijo.

    Returns:
        La espera, nunca mayor de `ESPERA_MAXIMA_SEGUNDOS`.
    """
    base = ESPERA_BASE_SEGUNDOS * (2**intento)
    espera: int = min(max(base, retry_after or 0), ESPERA_MAXIMA_SEGUNDOS)
    return espera


async def enriquecer(client_id: UUID, lead_id: UUID, user_id: UUID | None = None) -> dict[str, Any]:
    """Enriquece un lead, recalcula su FIT y evalua la calificacion automatica.

    Args:
        client_id: Tenant.
        lead_id: Lead.
        user_id: Quien lo pidio; `None` si fue el sistema.

    Returns:
        Resumen sin datos personales: `status`, `skipped`, `providers`, `fields`, `errors`,
        `retryable` y `retry_after`.
    """
    ajustes = get_settings()
    resumen: dict[str, Any] = {
        "status": ESTADO_OK,
        "skipped": None,
        "providers": [],
        "fields": [],
        "errors": {},
        "retryable": False,
        "retry_after": None,
    }
    try:
        proveedores = build_providers(ajustes.ENRICHMENT_PROVIDERS, client_id=client_id)
    except UnknownProviderError:
        # Error de configuracion del despliegue: reintentar no lo arregla.
        logger.exception("ENRICHMENT_PROVIDERS tiene un proveedor desconocido")
        resumen["status"] = ESTADO_CONFIG_INVALIDA
        return resumen
    if not proveedores:
        resumen["status"] = ESTADO_SIN_PROVEEDORES
        return resumen

    ahora = datetime.now(timezone.utc)
    async with tenant_session(client_id, user_id=user_id) as session:
        lead = (
            await session.execute(
                select(Lead).where(
                    Lead.id == lead_id, Lead.client_id == client_id, Lead.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if lead is None:
            resumen["status"] = ESTADO_SIN_LEAD
            return resumen

        salida = await enriquecer_lead(
            session,
            lead,
            proveedores,
            ahora=ahora,
            cache=CompanyCache(),
            enriquecer_persona=ajustes.ENRICHMENT_PERSON_ENABLED,
            user_id=user_id,
        )
        resumen.update(
            skipped=salida.skipped,
            providers=list(salida.providers_used),
            fields=list(salida.fields_filled),
            errors=dict(salida.errors),
            retryable=salida.retryable,
            retry_after=salida.retry_after,
        )
        if salida.skipped is None and salida.found_anything:
            await recalcular_fit(session, lead, trigger=TRIGGER_ENRICHMENT, user_id=user_id)
            await calificar_automaticamente(session, lead)
    return resumen


@shared_task(
    name="app.tasks.enrichment_enrich_lead",
    bind=True,
    max_retries=3,
    acks_late=True,
    time_limit=120,
    soft_time_limit=100,
)
def enrich_lead(
    self: Any, client_id: str, lead_id: str, user_id: str | None = None
) -> dict[str, Any]:
    """Enriquece un lead en segundo plano.

    Args:
        self: Instancia de la tarea (bind=True), para los reintentos.
        client_id: Tenant, serializado.
        lead_id: Lead, serializado.
        user_id: Quien lo pidio, serializado; `None` si fue el sistema.

    Returns:
        El resumen de `enriquecer()`.

    Raises:
        Retry: Si un proveedor fallo de forma pasajera (hasta 3 veces, con espera creciente).
    """
    resumen = run_isolated(
        enriquecer(UUID(client_id), UUID(lead_id), UUID(user_id) if user_id else None)
    )
    if resumen["retryable"] and self.request.retries < self.max_retries:
        espera = espera_de_reintento(self.request.retries, resumen["retry_after"])
        logger.warning("Enriquecimiento del lead %s a reintentar en %ss", lead_id, espera)
        raise self.retry(countdown=espera)
    return resumen


#: Leads por transaccion al recalcular el FIT tras cambiar el ICP: una transaccion por tenant
#: entera mantendria bloqueadas miles de filas.
LOTE_RECALCULO = 200


async def recalcular_fit_por_icp(client_id: UUID, user_id: UUID | None = None) -> int:
    """Recalcula el FIT de todos los leads activos con el ICP vigente del tenant.

    Va en lotes de `LOTE_RECALCULO` (una transaccion por lote, paginando por id) y lee el ICP y
    los pesos una sola vez por lote. Si el tenant ya no tiene ICP, el FIT no es aplicable y los
    valores vigentes no se tocan (ADR-085).

    Args:
        client_id: Tenant.
        user_id: Quien cambio el ICP.

    Returns:
        Cuantos leads se examinaron.
    """
    examinados = 0
    ultimo: UUID | None = None
    while True:
        async with tenant_session(client_id, user_id=user_id) as session:
            consulta = select(Lead).where(
                Lead.client_id == client_id, Lead.deleted_at.is_(None), Lead.status == "active"
            )
            if ultimo is not None:
                consulta = consulta.where(Lead.id > ultimo)
            leads = list(
                (await session.execute(consulta.order_by(Lead.id).limit(LOTE_RECALCULO)))
                .scalars()
                .all()
            )
            if not leads:
                return examinados
            icp, pesos = await icp_y_pesos_del_tenant(session, client_id)
            for lead in leads:
                await recalcular_fit(
                    session,
                    lead,
                    trigger=TRIGGER_ICP_CHANGED,
                    user_id=user_id,
                    icp=icp,
                    pesos=pesos,
                )
                await calificar_automaticamente(session, lead)
            examinados += len(leads)
            ultimo = leads[-1].id


@shared_task(
    name="app.tasks.enrichment_recalc_fit_icp",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
    acks_late=True,
    time_limit=900,
    soft_time_limit=840,
)
def recalc_fit_icp(self: Any, client_id: str, user_id: str | None = None) -> int:
    """Recalcula el FIT de los leads activos de un tenant tras cambiar su ICP.

    Args:
        self: Instancia de la tarea (bind=True), para los reintentos.
        client_id: Tenant, serializado.
        user_id: Quien cambio el ICP, serializado.

    Returns:
        Cuantos leads se examinaron.

    Raises:
        Retry: Si falla la base (hasta 2 veces); lo ya recalculado queda guardado.
    """
    try:
        return run_isolated(
            recalcular_fit_por_icp(UUID(client_id), UUID(user_id) if user_id else None)
        )
    except Exception as exc:
        logger.exception("Fallo recalculando el FIT del tenant %s", client_id)
        raise self.retry(exc=exc) from exc
