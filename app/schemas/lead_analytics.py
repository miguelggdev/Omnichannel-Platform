"""Respuestas de la analitica de leads, pipeline, ingresos y canales (Sprint 19, Dev B).

Reglas de lo que se mide:

- **Nunca se suman monedas distintas:** todo importe va por moneda.
- Las tasas son porcentajes con un decimal y `None` cuando el denominador es 0 (una tasa del 0 %
  y una tasa sin datos no son lo mismo).
- Ningun dato personal: solo conteos, importes y nombres de pila de responsables.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel


class AnalyticsRange(BaseModel):
    """El rango analizado (dias locales del negocio, ambos incluidos)."""

    date_from: date
    date_to: date
    timezone: str


class Cached(BaseModel):
    """Si la respuesta viene del pre-calculo diario.

    Attributes:
        cached: `True` si se leyo del pre-calculo.
        computed_at: Cuando se calculo.
    """

    cached: bool = False
    computed_at: datetime | None = None


class CountBy(BaseModel):
    """Un conteo por clave (estado, temperatura, canal...)."""

    key: str
    count: int


class SourceStats(BaseModel):
    """Leads de un tipo de fuente."""

    source_type: str
    created: int
    won: int
    conversion_rate: float | None


class StageCount(BaseModel):
    """Leads de la cohorte en una etapa del pipeline."""

    slug: str
    name: str
    position: int
    count: int


class LeadsAnalytics(Cached):
    """`GET /analytics/leads`: la cohorte de leads creados en el rango."""

    range: AnalyticsRange
    created: int
    unassigned: int
    avg_total_score: float | None
    by_status: list[CountBy]
    by_temperature: list[CountBy]
    by_source: list[SourceStats]
    by_stage: list[StageCount]
    conversion_rate: float | None


class MoneyByStage(BaseModel):
    """Deals abiertos en una etapa y moneda."""

    stage: str
    currency: str
    count: int
    total_value: Decimal
    weighted_value: Decimal
    avg_days_in_stage: float


class CurrencyFigure(BaseModel):
    """Una cifra por moneda."""

    currency: str
    value: Decimal


class WinRate(BaseModel):
    """Deals cerrados en el rango, en una moneda."""

    currency: str
    won: int
    lost: int
    win_rate: float | None


class CycleTime(BaseModel):
    """Dias medios desde que se abre un deal hasta que se gana."""

    currency: str
    won: int
    avg_days: float


class StalledDeals(BaseModel):
    """Deals abiertos que llevan demasiado en su etapa."""

    currency: str
    count: int
    total_value: Decimal


class PipelineAnalytics(Cached):
    """`GET /analytics/pipeline`: la salud del pipeline."""

    range: AnalyticsRange
    stalled_days: int
    open_by_stage: list[MoneyByStage]
    weighted_forecast: list[CurrencyFigure]
    win_rates: list[WinRate]
    cycle_times: list[CycleTime]
    stalled: list[StalledDeals]


class RevenueMonth(BaseModel):
    """Ingresos ganados de un mes y moneda."""

    month: str
    currency: str
    deals: int
    revenue: Decimal


class RevenueTotal(BaseModel):
    """Ingresos ganados del rango en una moneda."""

    currency: str
    deals: int
    revenue: Decimal
    avg_deal_size: Decimal


class RevenueByUser(BaseModel):
    """Ingresos ganados por responsable y moneda (sin responsable: `user_id` nulo)."""

    user_id: str | None
    first_name: str | None
    currency: str
    deals: int
    revenue: Decimal


class RevenueAnalytics(Cached):
    """`GET /analytics/revenue`: ingresos ganados (por `actual_close_date`)."""

    range: AnalyticsRange
    totals: list[RevenueTotal]
    by_month: list[RevenueMonth]
    by_user: list[RevenueByUser]


class ChannelStats(BaseModel):
    """Actividad de un canal en el rango."""

    channel: str
    sequence_sent: int
    sequence_replies: int
    reply_rate: float | None
    manual_sent: int
    reminders_sent: int
    inbound_messages: int


class CallStats(BaseModel):
    """Llamadas agendadas en el rango por tipo."""

    call_type: str
    scheduled: int
    completed: int
    no_show: int
    cancelled: int
    completion_rate: float | None


class ChannelsAnalytics(Cached):
    """`GET /analytics/channels`: rendimiento por canal y por tipo de llamada."""

    range: AnalyticsRange
    channels: list[ChannelStats]
    calls: list[CallStats]
