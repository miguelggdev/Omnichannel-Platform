"""Schemas de deals y llamadas agendadas (Sprint 19, slice Dev A).

Contrato de `CRUD /api/v1/deals`, `PATCH /deals/{id}/stage`, `GET /deals/pipeline` y
`CRUD /api/v1/scheduled-calls` (Dev B). Lo que se valida aqui y no en la base: zona horaria
IANA real, moneda en mayusculas, llamada en el futuro y proveedor de voz cuando la llamada no es
solo humana.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DealStage = Literal[
    "new_contact", "qualified", "proposal", "negotiation", "closed_won", "closed_lost"
]  # fmt: skip
CallType = Literal["ai_voice", "human", "hybrid"]
CallStatus = Literal[
    "pending", "confirmed", "in_progress", "completed", "no_show", "cancelled", "rescheduled"
]  # fmt: skip
#: Resultados de una llamada. Codigos, nunca texto libre (las notas van aparte).
CallOutcome = Literal[
    "interested", "not_interested", "follow_up", "meeting_booked", "wrong_number", "voicemail",
    "no_answer",
]  # fmt: skip

MAX_VALUE = Decimal("9999999999.99")  # NUMERIC(12, 2)


def _moneda(valor: str) -> str:
    """ISO 4217: tres letras, en mayusculas."""
    limpio = valor.strip().upper()
    if len(limpio) != 3 or not limpio.isascii() or not limpio.isalpha():
        raise ValueError("La moneda debe ser un codigo ISO de 3 letras (USD, COP, EUR...)")
    return limpio


def validar_zona(valor: str) -> str:
    """Una zona horaria IANA que el sistema conoce (`America/Bogota`).

    Args:
        valor: Nombre de la zona.

    Returns:
        El mismo nombre.

    Raises:
        ValueError: Si no existe.
    """
    try:
        ZoneInfo(valor)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Zona horaria desconocida: {valor!r}") from exc
    return valor


def _exigir_zona(valor: datetime) -> datetime:
    """Un instante con zona horaria: una hora "sin zona" es ambigua."""
    if valor.tzinfo is None or valor.utcoffset() is None:
        raise ValueError("scheduled_at necesita zona horaria (p. ej. 2026-10-20T15:00:00-05:00)")
    return valor


# ─── Deals ──────────────────────────────────────────────────────────────────────────────────


class DealCreate(BaseModel):
    """Alta de un deal.

    Attributes:
        lead_id: Lead del que sale.
        title: Titulo.
        value: Importe (>= 0).
        currency: Moneda ISO 4217.
        stage: Etapa inicial; no puede ser cerrada (un deal se cierra con `PATCH /stage`).
        probability: Probabilidad; por defecto la de la etapa (`PROBABILIDAD_POR_ETAPA`).
        expected_close_date: Cierre previsto.
        assigned_user_id: Responsable; por defecto, el del lead.
        notes: Notas.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    lead_id: UUID
    title: str = Field(min_length=1, max_length=300)
    value: Decimal = Field(default=Decimal(0), ge=0, le=MAX_VALUE, decimal_places=2)
    currency: str = "USD"
    stage: DealStage = "new_contact"
    probability: int | None = Field(default=None, ge=0, le=100)
    expected_close_date: date | None = None
    assigned_user_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=5000)

    @field_validator("currency")
    @classmethod
    def _moneda_valida(cls, valor: str) -> str:
        """ISO 4217."""
        return _moneda(valor)

    @model_validator(mode="after")
    def _abierto(self) -> Self:
        """Un deal nace abierto: ganar o perder deja rastro de fecha y motivo."""
        if self.stage in ("closed_won", "closed_lost"):
            raise ValueError("Un deal se crea abierto; se cierra cambiando su etapa")
        return self


class DealUpdate(BaseModel):
    """Edicion de un deal; la etapa no se cambia aqui (ver `DealStageChange`)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=300)
    value: Decimal | None = Field(default=None, ge=0, le=MAX_VALUE, decimal_places=2)
    currency: str | None = None
    probability: int | None = Field(default=None, ge=0, le=100)
    expected_close_date: date | None = None
    assigned_user_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=5000)

    @field_validator("currency")
    @classmethod
    def _moneda_valida(cls, valor: str | None) -> str | None:
        """ISO 4217."""
        return None if valor is None else _moneda(valor)


class DealStageChange(BaseModel):
    """Mover un deal de etapa.

    Attributes:
        stage: Etapa nueva.
        probability: Probabilidad; por defecto la de la etapa. Cerrado: 100 o 0, siempre.
        lost_reason: Motivo, solo al perder.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    stage: DealStage
    probability: int | None = Field(default=None, ge=0, le=100)
    lost_reason: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _motivo_solo_al_perder(self) -> Self:
        """El motivo de perdida solo va con `closed_lost`; al cerrar no se elige probabilidad."""
        if self.lost_reason is not None and self.stage != "closed_lost":
            raise ValueError("lost_reason solo aplica al pasar a closed_lost")
        if self.probability is not None and self.stage in ("closed_won", "closed_lost"):
            raise ValueError("Un deal cerrado tiene probabilidad 100 (ganado) o 0 (perdido)")
        return self


class DealResponse(BaseModel):
    """Un deal tal como lo devuelve la API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    lead_id: UUID
    assigned_user_id: UUID | None
    title: str
    value: Decimal
    currency: str
    stage: DealStage
    probability: int
    expected_close_date: date | None
    actual_close_date: date | None
    won_at: datetime | None
    lost_at: datetime | None
    lost_reason: str | None
    notes: str | None
    stage_changed_at: datetime
    created_at: datetime
    updated_at: datetime


class PipelineStageSummary(BaseModel):
    """Una etapa del pipeline en una moneda.

    Attributes:
        stage: Etapa.
        currency: Moneda (los importes de monedas distintas nunca se suman).
        count: Deals.
        total_value: Suma de valores.
        weighted_value: Suma de valor x probabilidad.
        avg_days_in_stage: Dias medios en la etapa (salud del pipeline).
    """

    stage: DealStage
    currency: str
    count: int
    total_value: Decimal
    weighted_value: Decimal
    avg_days_in_stage: float


# ─── Llamadas agendadas ─────────────────────────────────────────────────────────────────────


class ScheduledCallCreate(BaseModel):
    """Agendar una llamada.

    Attributes:
        lead_id: Lead.
        deal_id: Deal (opcional).
        scheduled_at: Inicio; con zona horaria (una hora "sin zona" es ambigua).
        duration_minutes: 5-480.
        timezone: Zona del lead (IANA); por defecto la del negocio, la decide quien llama.
        call_type: `human`, `ai_voice` o `hybrid`.
        assigned_user_id: Quien llama; obligatorio salvo en `ai_voice`.
        ai_voice_provider: Proveedor de voz IA; obligatorio salvo en `human`.
        ai_voice_config: Configuracion del proveedor (nunca secretos: van en `.env`).
        notes: Notas.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    lead_id: UUID
    deal_id: UUID | None = None
    scheduled_at: datetime
    duration_minutes: int = Field(default=30, ge=5, le=480)
    timezone: str | None = Field(default=None, max_length=50)
    call_type: CallType = "human"
    assigned_user_id: UUID | None = None
    ai_voice_provider: str | None = Field(default=None, min_length=1, max_length=30)
    ai_voice_config: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = Field(default=None, max_length=5000)

    @field_validator("timezone")
    @classmethod
    def _zona(cls, valor: str | None) -> str | None:
        """Zona IANA real."""
        return None if valor is None else validar_zona(valor)

    @field_validator("scheduled_at")
    @classmethod
    def _con_zona(cls, valor: datetime) -> datetime:
        """Sin zona no se sabe a que hora es."""
        return _exigir_zona(valor)

    @model_validator(mode="after")
    def _quien_llama(self) -> Self:
        """Una persona llama en `human`/`hybrid`; un proveedor en `ai_voice`/`hybrid`."""
        if self.call_type != "ai_voice" and self.assigned_user_id is None:
            raise ValueError("Una llamada humana o mixta necesita assigned_user_id")
        if self.call_type != "human" and not self.ai_voice_provider:
            raise ValueError("Una llamada con voz IA necesita ai_voice_provider")
        if self.call_type == "human" and self.ai_voice_provider:
            raise ValueError("ai_voice_provider no aplica a una llamada humana")
        return self


class ScheduledCallReschedule(BaseModel):
    """Reprogramar: nuevo inicio (y duracion, si cambia)."""

    model_config = ConfigDict(extra="forbid")

    scheduled_at: datetime
    duration_minutes: int | None = Field(default=None, ge=5, le=480)

    @field_validator("scheduled_at")
    @classmethod
    def _con_zona(cls, valor: datetime) -> datetime:
        """Sin zona no se sabe a que hora es."""
        return _exigir_zona(valor)


class ScheduledCallComplete(BaseModel):
    """Cerrar una llamada que ocurrio."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    outcome: CallOutcome
    call_record_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=5000)


class ScheduledCallResponse(BaseModel):
    """Una llamada agendada tal como la devuelve la API (sin transcripcion: esta en
    `call_records`, cifrada, y se pide aparte)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    lead_id: UUID
    deal_id: UUID | None
    assigned_user_id: UUID | None
    call_record_id: UUID | None
    call_type: CallType
    status: CallStatus
    scheduled_at: datetime
    duration_minutes: int
    timezone: str
    ai_voice_provider: str | None
    outcome: str | None
    notes: str | None
    confirmed_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime
