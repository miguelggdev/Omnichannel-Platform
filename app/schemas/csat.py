"""Schemas de CSAT (Sprint 11, Dev B)."""

from pydantic import BaseModel


class CsatSummaryResponse(BaseModel):
    """Resumen de CSAT del tenant en un periodo (`GET /admin/csat/summary`)."""

    period_days: int
    total_surveys_sent: int
    total_responses: int
    response_rate: float
    average_rating: float
    promoters: int
    detractors: int
    distribution: dict[int, int]
    weekly_trend: list[dict[str, object]]
