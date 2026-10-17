"""Analitica de leads, pipeline, ingresos y canales (Sprint 19, Dev B).

Todo se calcula con agregaciones en la base (un tenant puede tener cientos de miles de leads y
actividades), cada consulta con `client_id` explicito ademas de RLS. Cuatro funciones, una por
endpoint, y un cache en Redis de lo que se pide mas (el rango por defecto):

- El **pre-calculo diario** (`app.tasks.bulk_precompute_lead_analytics`) guarda en Redis las cuatro
  respuestas del rango por defecto (ultimos 30 dias) de cada tenant. No hay tabla nueva: son datos
  derivados y se pueden recalcular en cualquier momento; si Redis cae, se calcula en vivo.
- Una consulta con **rango propio** (o con `stalled_days` distinto) se calcula siempre en vivo.
- Una consulta por defecto sin pre-calculo (primera del dia, o Redis vacio) se calcula en vivo y se
  guarda 15 minutos para no repetirla.

Reglas de lo que se mide (ver `app/schemas/lead_analytics.py`): las monedas no se suman, las tasas
son `None` sin denominador y no sale ningun dato personal.
"""

import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, TypeVar
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.models.deal import DEAL_LOST, DEAL_WON, OPEN_STAGES, Deal
from app.models.lead import Lead
from app.models.lead_activity import (
    ACTIVITY_CALL_REMINDER_SENT,
    ACTIVITY_INBOUND_MESSAGE,
    ACTIVITY_MESSAGE_SENT,
    ACTIVITY_SEQUENCE_MESSAGE_SENT,
    ACTIVITY_SEQUENCE_REPLY,
    LeadActivity,
)
from app.models.lead_pipeline_stage import LeadPipelineStage
from app.models.lead_source import LeadSource
from app.models.scheduled_call import ScheduledCall
from app.models.user import User
from app.schemas.lead_analytics import (
    AnalyticsRange,
    CallStats,
    ChannelsAnalytics,
    ChannelStats,
    CountBy,
    CurrencyFigure,
    CycleTime,
    LeadsAnalytics,
    MoneyByStage,
    PipelineAnalytics,
    RevenueAnalytics,
    RevenueByUser,
    RevenueMonth,
    RevenueTotal,
    SourceStats,
    StageCount,
    StalledDeals,
    WinRate,
)
from app.services.deals import ingresos_por_mes, resumen_pipeline
from app.services.dedup import get_redis
from app.services.lead_sequence_timing import ventana_del_tenant

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

#: Dias del rango por defecto y maximo permitido.
DIAS_POR_DEFECTO = 30
MAX_DIAS = 366
STALLED_DIAS_POR_DEFECTO = 14
MAX_RESPONSABLES = 20

KINDS = ("leads", "pipeline", "revenue", "channels")
VIDA_PRECALCULO = 36 * 3600
VIDA_EN_VIVO = 15 * 60

_CENTIMOS = Decimal("0.01")


class RangoInvalidoError(ValueError):
    """El rango pedido no es valido (orden o tamano)."""


def tasa(parte: int, total: int) -> float | None:
    """Porcentaje con un decimal, o `None` sin denominador.

    Args:
        parte: Numerador.
        total: Denominador.

    Returns:
        `parte / total * 100` redondeado a un decimal; `None` si `total` es 0.
    """
    return round(parte * 100 / total, 1) if total else None


def rango_por_defecto(hoy: date) -> tuple[date, date]:
    """Los ultimos `DIAS_POR_DEFECTO` dias, hoy incluido.

    Args:
        hoy: Dia local del negocio.

    Returns:
        `(desde, hasta)`.
    """
    return hoy - timedelta(days=DIAS_POR_DEFECTO - 1), hoy


def validar_rango(desde: date, hasta: date) -> None:
    """Comprueba el orden y el tamano del rango.

    Args:
        desde: Primer dia.
        hasta: Ultimo dia.

    Raises:
        RangoInvalidoError: Si `hasta` es anterior a `desde` o el rango pasa de `MAX_DIAS`.
    """
    if hasta < desde:
        raise RangoInvalidoError("date_to no puede ser anterior a date_from")
    if (hasta - desde).days + 1 > MAX_DIAS:
        raise RangoInvalidoError(f"El rango no puede pasar de {MAX_DIAS} dias")


def limites(desde: date, hasta: date, zona: ZoneInfo) -> tuple[datetime, datetime]:
    """Instantes `[inicio, fin)` de un rango de dias locales.

    Args:
        desde: Primer dia local.
        hasta: Ultimo dia local (incluido).
        zona: Zona del negocio.

    Returns:
        Inicio del primer dia y comienzo del dia siguiente al ultimo, en UTC.
    """
    inicio = datetime.combine(desde, time.min, tzinfo=zona)
    fin = datetime.combine(hasta + timedelta(days=1), time.min, tzinfo=zona)
    return inicio.astimezone(timezone.utc), fin.astimezone(timezone.utc)


async def _contexto(
    session: AsyncSession, client_id: UUID, desde: date, hasta: date
) -> tuple[ZoneInfo, datetime, datetime, AnalyticsRange]:
    """Zona del negocio, limites en UTC y la descripcion del rango."""
    zona = (await ventana_del_tenant(session, client_id)).tz
    inicio, fin = limites(desde, hasta, zona)
    return zona, inicio, fin, AnalyticsRange(date_from=desde, date_to=hasta, timezone=zona.key)


def _dinero(valor: Any) -> Decimal:
    return Decimal(valor or 0).quantize(_CENTIMOS)


async def hoy_del_negocio(session: AsyncSession, client_id: UUID) -> date:
    """El dia de hoy en la zona del negocio."""
    zona = (await ventana_del_tenant(session, client_id)).tz
    return datetime.now(zona).date()


# ─── Leads ──────────────────────────────────────────────────────────────────────────────────


async def analitica_leads(
    session: AsyncSession, client_id: UUID, desde: date, hasta: date
) -> LeadsAnalytics:
    """La cohorte de leads creados en el rango.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        desde: Primer dia local.
        hasta: Ultimo dia local.

    Returns:
        Totales, estados, temperaturas, fuentes y etapas actuales de esa cohorte.
    """
    _, inicio, fin, rango = await _contexto(session, client_id, desde, hasta)
    cohorte = [
        Lead.client_id == client_id,
        Lead.deleted_at.is_(None),
        Lead.created_at >= inicio,
        Lead.created_at < fin,
    ]
    creados, sin_asignar, media = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Lead.assigned_user_id.is_(None)),
                func.avg(Lead.total_score),
            ).where(*cohorte)
        )
    ).one()

    async def por(columna: Any) -> list[CountBy]:
        filas = await session.execute(
            select(columna, func.count()).where(*cohorte).group_by(columna).order_by(columna)
        )
        return [CountBy(key=str(k), count=int(n)) for k, n in filas.all()]

    por_estado = await por(Lead.status)
    ganados = next((c.count for c in por_estado if c.key == "won"), 0)
    fuente = func.coalesce(LeadSource.source_type, "unknown")
    filas_fuente = await session.execute(
        select(fuente, func.count(), func.count().filter(Lead.status == "won"))
        .select_from(Lead)
        .outerjoin(
            LeadSource, (LeadSource.id == Lead.source_id) & (LeadSource.client_id == Lead.client_id)
        )
        .where(*cohorte)
        .group_by(fuente)
        .order_by(func.count().desc(), fuente)
    )
    filas_etapa = await session.execute(
        select(
            LeadPipelineStage.slug,
            LeadPipelineStage.name,
            LeadPipelineStage.position,
            func.count(),
        )
        .select_from(Lead)
        .join(
            LeadPipelineStage,
            (LeadPipelineStage.id == Lead.pipeline_stage_id)
            & (LeadPipelineStage.client_id == Lead.client_id),
        )
        .where(*cohorte)
        .group_by(LeadPipelineStage.slug, LeadPipelineStage.name, LeadPipelineStage.position)
        .order_by(LeadPipelineStage.position)
    )
    return LeadsAnalytics(
        range=rango,
        created=int(creados),
        unassigned=int(sin_asignar),
        avg_total_score=round(float(media), 1) if media is not None else None,
        by_status=por_estado,
        by_temperature=await por(Lead.temperature),
        by_source=[
            SourceStats(
                source_type=str(tipo),
                created=int(n),
                won=int(g),
                conversion_rate=tasa(int(g), int(n)),
            )
            for tipo, n, g in filas_fuente.all()
        ],
        by_stage=[
            StageCount(slug=s, name=nombre, position=int(p), count=int(n))
            for s, nombre, p, n in filas_etapa.all()
        ],
        conversion_rate=tasa(ganados, int(creados)),
    )


# ─── Pipeline ───────────────────────────────────────────────────────────────────────────────


async def analitica_pipeline(
    session: AsyncSession,
    client_id: UUID,
    desde: date,
    hasta: date,
    *,
    stalled_days: int = STALLED_DIAS_POR_DEFECTO,
) -> PipelineAnalytics:
    """La salud del pipeline: abiertos, previsto, tasa de cierre, ciclo y deals atascados.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        desde: Primer dia local de los cierres a medir.
        hasta: Ultimo dia local.
        stalled_days: Dias en una etapa a partir de los cuales un deal abierto cuenta como atascado.

    Returns:
        Los deals abiertos ahora (no dependen del rango) y los cierres del rango.
    """
    _, _, _, rango = await _contexto(session, client_id, desde, hasta)
    ahora = datetime.now(timezone.utc)
    abiertos = await resumen_pipeline(session, client_id, ahora=ahora, solo_abiertos=True)
    previsto: dict[str, Decimal] = defaultdict(Decimal)
    for fila in abiertos:
        previsto[fila.currency] += fila.weighted_value

    en_rango = [
        Deal.client_id == client_id,
        Deal.actual_close_date >= desde,
        Deal.actual_close_date <= hasta,
    ]
    cerrados: dict[str, dict[str, int]] = defaultdict(lambda: {DEAL_WON: 0, DEAL_LOST: 0})
    for moneda, etapa, n in (
        await session.execute(
            select(Deal.currency, Deal.stage, func.count())
            .where(*en_rango, Deal.stage.in_((DEAL_WON, DEAL_LOST)))
            .group_by(Deal.currency, Deal.stage)
        )
    ).all():
        cerrados[moneda][etapa] = int(n)

    ciclo = (
        await session.execute(
            select(
                Deal.currency,
                func.count(),
                func.avg(extract("epoch", Deal.won_at - Deal.created_at) / 86400),
            )
            .where(*en_rango, Deal.stage == DEAL_WON, Deal.won_at.is_not(None))
            .group_by(Deal.currency)
            .order_by(Deal.currency)
        )
    ).all()
    atascados = (
        await session.execute(
            select(Deal.currency, func.count(), func.coalesce(func.sum(Deal.value), 0))
            .where(
                Deal.client_id == client_id,
                Deal.stage.in_(OPEN_STAGES),
                Deal.stage_changed_at < ahora - timedelta(days=stalled_days),
            )
            .group_by(Deal.currency)
            .order_by(Deal.currency)
        )
    ).all()
    return PipelineAnalytics(
        range=rango,
        stalled_days=stalled_days,
        open_by_stage=[
            MoneyByStage(
                stage=f.stage,
                currency=f.currency,
                count=f.count,
                total_value=f.total_value,
                weighted_value=f.weighted_value,
                avg_days_in_stage=f.avg_days_in_stage,
            )
            for f in abiertos
        ],
        weighted_forecast=[
            CurrencyFigure(currency=m, value=v.quantize(_CENTIMOS))
            for m, v in sorted(previsto.items())
        ],
        win_rates=[
            WinRate(
                currency=m,
                won=c[DEAL_WON],
                lost=c[DEAL_LOST],
                win_rate=tasa(c[DEAL_WON], c[DEAL_WON] + c[DEAL_LOST]),
            )
            for m, c in sorted(cerrados.items())
        ],
        cycle_times=[
            CycleTime(currency=m, won=int(n), avg_days=round(float(media or 0), 1))
            for m, n, media in ciclo
        ],
        stalled=[
            StalledDeals(currency=m, count=int(n), total_value=_dinero(v)) for m, n, v in atascados
        ],
    )


# ─── Ingresos ───────────────────────────────────────────────────────────────────────────────


async def analitica_ingresos(
    session: AsyncSession, client_id: UUID, desde: date, hasta: date
) -> RevenueAnalytics:
    """Ingresos ganados del rango (deals `closed_won` por su dia de cierre).

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        desde: Primer dia local.
        hasta: Ultimo dia local.

    Returns:
        Totales por moneda, por mes y por responsable.
    """
    _, _, _, rango = await _contexto(session, client_id, desde, hasta)
    ganados = [
        Deal.client_id == client_id,
        Deal.stage == DEAL_WON,
        Deal.actual_close_date >= desde,
        Deal.actual_close_date <= hasta,
    ]
    totales = (
        await session.execute(
            select(Deal.currency, func.count(), func.sum(Deal.value))
            .where(*ganados)
            .group_by(Deal.currency)
            .order_by(Deal.currency)
        )
    ).all()
    por_usuario = (
        await session.execute(
            select(
                Deal.assigned_user_id,
                User.first_name,
                Deal.currency,
                func.count(),
                func.sum(Deal.value).label("ingresos"),
            )
            .select_from(Deal)
            .outerjoin(
                User, (User.id == Deal.assigned_user_id) & (User.client_id == Deal.client_id)
            )
            .where(*ganados)
            .group_by(Deal.assigned_user_id, User.first_name, Deal.currency)
            .order_by(func.sum(Deal.value).desc(), Deal.currency)
            .limit(MAX_RESPONSABLES)
        )
    ).all()
    return RevenueAnalytics(
        range=rango,
        totals=[
            RevenueTotal(
                currency=m,
                deals=int(n),
                revenue=_dinero(v),
                avg_deal_size=_dinero(Decimal(v or 0) / int(n)) if n else Decimal("0.00"),
            )
            for m, n, v in totales
        ],
        by_month=[
            RevenueMonth(
                month=f["month"],
                currency=f["currency"],
                deals=f["deals"],
                revenue=_dinero(f["revenue"]),
            )
            for f in await ingresos_por_mes(session, client_id, desde=desde, hasta=hasta)
        ],
        by_user=[
            RevenueByUser(
                user_id=str(uid) if uid else None,
                first_name=nombre,
                currency=m,
                deals=int(n),
                revenue=_dinero(v),
            )
            for uid, nombre, m, n, v in por_usuario
        ],
    )


# ─── Canales ────────────────────────────────────────────────────────────────────────────────

_TIPO_A_CAMPO = {
    ACTIVITY_SEQUENCE_MESSAGE_SENT: "sequence_sent",
    ACTIVITY_SEQUENCE_REPLY: "sequence_replies",
    ACTIVITY_MESSAGE_SENT: "manual_sent",
    ACTIVITY_CALL_REMINDER_SENT: "reminders_sent",
    ACTIVITY_INBOUND_MESSAGE: "inbound_messages",
}


async def analitica_canales(
    session: AsyncSession, client_id: UUID, desde: date, hasta: date
) -> ChannelsAnalytics:
    """Actividad por canal (secuencias, mensajes manuales, recordatorios, entrantes) y llamadas.

    Los mensajes entrantes cuentan como mucho uno cada 30 minutos por lead (la anotacion del
    historial esta acotada, ADR-084): miden lo activo que esta el canal, no el volumen exacto.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        desde: Primer dia local.
        hasta: Ultimo dia local.

    Returns:
        Una fila por canal con actividad y una por tipo de llamada.
    """
    _, inicio, fin, rango = await _contexto(session, client_id, desde, hasta)
    canal = LeadActivity.metadata_["channel"].astext
    filas = (
        await session.execute(
            select(LeadActivity.activity_type, canal, func.count())
            .where(
                LeadActivity.client_id == client_id,
                LeadActivity.created_at >= inicio,
                LeadActivity.created_at < fin,
                LeadActivity.activity_type.in_(list(_TIPO_A_CAMPO)),
                canal.is_not(None),
            )
            .group_by(LeadActivity.activity_type, canal)
        )
    ).all()
    por_canal: dict[str, dict[str, int]] = defaultdict(
        lambda: dict.fromkeys(_TIPO_A_CAMPO.values(), 0)
    )
    for tipo, nombre, n in filas:
        por_canal[nombre][_TIPO_A_CAMPO[tipo]] = int(n)

    llamadas: dict[str, dict[str, int]] = defaultdict(
        lambda: {"scheduled": 0, "completed": 0, "no_show": 0, "cancelled": 0}
    )
    for tipo, estado, n in (
        await session.execute(
            select(ScheduledCall.call_type, ScheduledCall.status, func.count())
            .where(
                ScheduledCall.client_id == client_id,
                ScheduledCall.scheduled_at >= inicio,
                ScheduledCall.scheduled_at < fin,
                # Una llamada reprogramada se cuenta por la nueva, no por la vieja.
                ScheduledCall.status != "rescheduled",
            )
            .group_by(ScheduledCall.call_type, ScheduledCall.status)
        )
    ).all():
        llamadas[tipo]["scheduled"] += int(n)
        if estado in ("completed", "no_show", "cancelled"):
            llamadas[tipo][estado] += int(n)
    return ChannelsAnalytics(
        range=rango,
        channels=[
            ChannelStats(
                channel=nombre,
                reply_rate=tasa(c["sequence_replies"], c["sequence_sent"]),
                **c,
            )
            for nombre, c in sorted(por_canal.items())
        ],
        calls=[
            CallStats(
                call_type=tipo,
                completion_rate=tasa(c["completed"], c["scheduled"] - c["cancelled"]),
                **c,
            )
            for tipo, c in sorted(llamadas.items())
        ],
    )


# ─── Cache ──────────────────────────────────────────────────────────────────────────────────


def _clave(client_id: UUID, kind: str) -> str:
    return f"analytics:{client_id}:{kind}"


async def leer_cache(client_id: UUID, kind: str, modelo: type[T]) -> T | None:
    """La respuesta pre-calculada de un tipo, si existe (y Redis responde).

    Args:
        client_id: Tenant.
        kind: Uno de `KINDS`.
        modelo: Clase de la respuesta.

    Returns:
        La respuesta con `cached=True`, o `None`.
    """
    try:
        bruto = await get_redis().get(_clave(client_id, kind))
    except Exception:
        logger.warning("Redis no responde: la analitica se calcula en vivo")
        return None
    if not bruto:
        return None
    try:
        respuesta = modelo.model_validate_json(bruto)
    except ValueError:
        return None
    return respuesta.model_copy(update={"cached": True})


async def guardar_cache(client_id: UUID, kind: str, respuesta: BaseModel, *, vida: int) -> None:
    """Guarda una respuesta (con su hora de calculo). Un fallo de Redis no es un error."""
    ahora = datetime.now(timezone.utc)
    try:
        await get_redis().set(
            _clave(client_id, kind),
            respuesta.model_copy(update={"computed_at": ahora, "cached": False}).model_dump_json(),
            ex=vida,
        )
    except Exception:
        logger.warning("No se pudo guardar el pre-calculo de analitica (kind=%s)", kind)


async def calcular(
    session: AsyncSession, client_id: UUID, kind: str, desde: date, hasta: date
) -> BaseModel:
    """Calcula una analitica por su nombre (para el pre-calculo)."""
    if kind == "leads":
        return await analitica_leads(session, client_id, desde, hasta)
    if kind == "pipeline":
        return await analitica_pipeline(session, client_id, desde, hasta)
    if kind == "revenue":
        return await analitica_ingresos(session, client_id, desde, hasta)
    if kind == "channels":
        return await analitica_canales(session, client_id, desde, hasta)
    raise ValueError(f"Analitica desconocida: {kind!r}")


async def precalcular_tenant(client_id: UUID) -> int:
    """Calcula y guarda las cuatro analiticas del rango por defecto de un tenant.

    Args:
        client_id: Tenant.

    Returns:
        Cuantas se guardaron.
    """
    guardadas = 0
    async with tenant_session(client_id) as session:
        desde, hasta = rango_por_defecto(await hoy_del_negocio(session, client_id))
        for kind in KINDS:
            try:
                respuesta = await calcular(session, client_id, kind, desde, hasta)
            except Exception:
                logger.exception("Fallo precalculando %s (client_id=%s)", kind, client_id)
                continue
            await guardar_cache(client_id, kind, respuesta, vida=VIDA_PRECALCULO)
            guardadas += 1
    return guardadas


# Re-exportado para los tests y el endpoint.
__all__ = [
    "KINDS",
    "VIDA_EN_VIVO",
    "RangoInvalidoError",
    "analitica_canales",
    "analitica_ingresos",
    "analitica_leads",
    "analitica_pipeline",
    "calcular",
    "guardar_cache",
    "hoy_del_negocio",
    "leer_cache",
    "precalcular_tenant",
    "rango_por_defecto",
    "tasa",
    "validar_rango",
]
