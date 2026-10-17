"""API de analitica de leads (Sprint 19, Dev B), en `/api/v1/analytics` junto a la del dashboard.

    GET /api/v1/analytics/leads      cohorte de leads creados en el rango
    GET /api/v1/analytics/pipeline   salud del pipeline: abiertos, previsto, cierre, ciclo, atascados
    GET /api/v1/analytics/revenue    ingresos ganados por moneda, mes y responsable
    GET /api/v1/analytics/channels   rendimiento por canal y por tipo de llamada

Solo `supervisor` para arriba (son cifras del negocio, no de un lead). Sin parametros se sirve el
**pre-calculo diario** de los ultimos 30 dias (`cached: true`); con `date_from`/`date_to` (o
`stalled_days` distinto en el pipeline) se calcula en vivo. Hay un limite de peticiones por usuario y
minuto: una consulta en vivo agrega miles de filas.

Los importes van siempre por moneda y las tasas son `null` sin denominador (ver
`app/schemas/lead_analytics.py`).
"""

import logging
from datetime import date
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.database import tenant_session
from app.core.dependencies import require_leads_module
from app.core.exceptions import VALIDATION_ERROR, AppException
from app.core.rate_limit import hit
from app.schemas.lead_analytics import (
    ChannelsAnalytics,
    LeadsAnalytics,
    PipelineAnalytics,
    RevenueAnalytics,
)
from app.services import lead_analytics as svc

logger = logging.getLogger(__name__)

router = APIRouter()

_ROLES = ("super_admin", "admin", "supervisor")
PETICIONES_POR_MINUTO = 60

T = TypeVar("T", bound=BaseModel)


async def _servir(
    user: dict[str, Any],
    kind: str,
    modelo: type[T],
    date_from: date | None,
    date_to: date | None,
    *,
    en_vivo: bool = False,
) -> T:
    """Sirve una analitica: el pre-calculo si la consulta es la de por defecto, si no en vivo.

    Args:
        user: Usuario autenticado.
        kind: Uno de `svc.KINDS`.
        modelo: Clase de la respuesta.
        date_from: Primer dia local (con `date_to`, o ninguno de los dos).
        date_to: Ultimo dia local.
        en_vivo: Forzar el calculo en vivo (parametros propios del endpoint).

    Returns:
        La respuesta.

    Raises:
        AppException: 400 si falta una de las fechas, el orden es incorrecto o el rango es enorme;
            429 por el limite de peticiones.
    """
    client_id = user["client_id"]
    await hit(f"analytics:{client_id}:{user['user_id']}", PETICIONES_POR_MINUTO, 60)
    if (date_from is None) != (date_to is None):
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message="Indica date_from y date_to juntos, o ninguno",
        )
    por_defecto = date_from is None and not en_vivo
    if por_defecto:
        guardada = await svc.leer_cache(client_id, kind, modelo)
        if guardada is not None:
            return guardada
    async with tenant_session(client_id) as session:
        if date_from is None or date_to is None:
            date_from, date_to = svc.rango_por_defecto(
                await svc.hoy_del_negocio(session, client_id)
            )
        try:
            svc.validar_rango(date_from, date_to)
        except svc.RangoInvalidoError as exc:
            raise AppException(
                status_code=400, error_code=VALIDATION_ERROR, message=str(exc)
            ) from exc
        respuesta = await svc.calcular(session, client_id, kind, date_from, date_to)
    if por_defecto:
        await svc.guardar_cache(client_id, kind, respuesta, vida=svc.VIDA_EN_VIVO)
    return modelo.model_validate(respuesta.model_dump())


@router.get("/leads", response_model=LeadsAnalytics)
async def leads_analytics(
    date_from: date | None = None,
    date_to: date | None = None,
    user: dict[str, Any] = Depends(require_leads_module(*_ROLES)),
) -> LeadsAnalytics:
    """Los leads creados en el rango: estados, temperaturas, fuentes, etapas y conversion.

    Args:
        date_from: Primer dia local (por defecto, hace 30 dias).
        date_to: Ultimo dia local (por defecto, hoy).
        user: Usuario autenticado.

    Returns:
        La analitica.
    """
    return await _servir(user, "leads", LeadsAnalytics, date_from, date_to)


@router.get("/pipeline", response_model=PipelineAnalytics)
async def pipeline_analytics(
    date_from: date | None = None,
    date_to: date | None = None,
    stalled_days: int = Query(default=svc.STALLED_DIAS_POR_DEFECTO, ge=1, le=365),
    user: dict[str, Any] = Depends(require_leads_module(*_ROLES)),
) -> PipelineAnalytics:
    """Salud del pipeline: abiertos por etapa y moneda, previsto, tasa de cierre, ciclo y atascados.

    Los deals abiertos son los de ahora; la tasa de cierre y el ciclo miden los cierres del rango.

    Args:
        date_from: Primer dia local de los cierres.
        date_to: Ultimo dia local.
        stalled_days: Dias en una etapa para contar un deal como atascado.
        user: Usuario autenticado.

    Returns:
        La analitica.
    """
    if stalled_days != svc.STALLED_DIAS_POR_DEFECTO:
        client_id = user["client_id"]
        await hit(f"analytics:{client_id}:{user['user_id']}", PETICIONES_POR_MINUTO, 60)
        if (date_from is None) != (date_to is None):
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message="Indica date_from y date_to juntos, o ninguno",
            )
        async with tenant_session(client_id) as session:
            if date_from is None or date_to is None:
                date_from, date_to = svc.rango_por_defecto(
                    await svc.hoy_del_negocio(session, client_id)
                )
            try:
                svc.validar_rango(date_from, date_to)
            except svc.RangoInvalidoError as exc:
                raise AppException(
                    status_code=400, error_code=VALIDATION_ERROR, message=str(exc)
                ) from exc
            return await svc.analitica_pipeline(
                session, client_id, date_from, date_to, stalled_days=stalled_days
            )
    return await _servir(user, "pipeline", PipelineAnalytics, date_from, date_to)


@router.get("/revenue", response_model=RevenueAnalytics)
async def revenue_analytics(
    date_from: date | None = None,
    date_to: date | None = None,
    user: dict[str, Any] = Depends(require_leads_module(*_ROLES)),
) -> RevenueAnalytics:
    """Ingresos ganados del rango: totales y ticket medio por moneda, por mes y por responsable.

    Args:
        date_from: Primer dia local (por defecto, hace 30 dias).
        date_to: Ultimo dia local (por defecto, hoy).
        user: Usuario autenticado.

    Returns:
        La analitica.
    """
    return await _servir(user, "revenue", RevenueAnalytics, date_from, date_to)


@router.get("/channels", response_model=ChannelsAnalytics)
async def channels_analytics(
    date_from: date | None = None,
    date_to: date | None = None,
    user: dict[str, Any] = Depends(require_leads_module(*_ROLES)),
) -> ChannelsAnalytics:
    """Rendimiento por canal (secuencias, respuestas, recordatorios, entrantes) y por tipo de llamada.

    Args:
        date_from: Primer dia local (por defecto, hace 30 dias).
        date_to: Ultimo dia local (por defecto, hoy).
        user: Usuario autenticado.

    Returns:
        La analitica.
    """
    return await _servir(user, "channels", ChannelsAnalytics, date_from, date_to)
