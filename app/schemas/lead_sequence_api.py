"""Schemas de la API de secuencias de follow-up (Sprint 18, Dev B).

El contrato de lo que *define* una secuencia (`SequenceDefinition`, los pasos) lo dejo Dev A en
`app/schemas/lead_sequence.py`; aqui solo estan las envolturas de la API: lo que se lista, lo que
se edita y lo que se devuelve al inscribir o al mostrar el Kanban.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.lead_sequence import (
    OutreachChannel,
    SequenceDefinition,
    StepDefinition,
    TriggerConditions,
)


class SequenceSave(SequenceDefinition):
    """Cuerpo de `POST` y `PUT /lead-sequences`: la definicion completa y si esta encendida.

    Attributes:
        is_active: Una secuencia apagada no inscribe ni avanza a nadie.
    """

    is_active: bool = True


class SequenceSummary(BaseModel):
    """Una secuencia en el listado."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    is_active: bool
    trigger_conditions: TriggerConditions
    channel_priority: list[OutreachChannel]
    steps_count: int
    live_enrollments: int
    created_at: datetime
    updated_at: datetime


class StepResponse(BaseModel):
    """Un paso con su posicion."""

    position: int
    step: StepDefinition


class SequenceDetail(SequenceSummary):
    """Una secuencia con sus pasos.

    Attributes:
        steps: Pasos en orden.
        live_enrollments_warning: Aviso cuando se edita una secuencia con inscripciones vivas:
            siguen por la **posicion** en la que iban, no por el paso que estaban ejecutando.
    """

    steps: list[StepResponse]
    live_enrollments_warning: str | None = None


class TemplateInfo(BaseModel):
    """Una secuencia predefinida que el tenant puede copiar."""

    key: str
    name: str
    description: str | None
    steps_count: int
    auto_trigger: bool


class FromTemplateRequest(BaseModel):
    """Cuerpo de `POST /lead-sequences/from-template`."""

    model_config = ConfigDict(extra="forbid")

    template: str = Field(min_length=1, max_length=50)
    name: str | None = Field(default=None, min_length=1, max_length=200)


class EnrollmentResponse(BaseModel):
    """Una inscripcion."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    lead_id: UUID
    sequence_id: UUID
    status: str
    current_step: int
    steps_executed: int
    next_step_at: datetime | None
    last_step_at: datetime | None
    completed_at: datetime | None
    exit_reason: str | None
    created_at: datetime


class ManualMessageRequest(BaseModel):
    """Cuerpo de `POST /leads/{id}/message`."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    channel: OutreachChannel
    text: str = Field(min_length=1, max_length=4000)
    subject: str | None = Field(default=None, min_length=1, max_length=200)


class ManualMessageResponse(BaseModel):
    """Resultado de un mensaje manual."""

    lead_id: UUID
    channel: OutreachChannel
    sent: bool = True


FollowUpColumnKey = Literal["overdue", "upcoming", "paused", "replied", "finished"]


class FollowUpCard(BaseModel):
    """Una tarjeta del Kanban de follow-up: un lead en una secuencia."""

    enrollment_id: UUID
    lead_id: UUID
    sequence_id: UUID
    sequence_name: str
    first_name: str | None
    last_name: str | None
    company_name: str | None
    total_score: int
    temperature: str
    assigned_user_id: UUID | None
    status: str
    current_step: int
    next_step_at: datetime | None
    exit_reason: str | None
    last_step_at: datetime | None


class FollowUpColumn(BaseModel):
    """Una columna del Kanban de follow-up.

    Attributes:
        key: `overdue` (le toca ya), `upcoming` (programada), `paused` (esperando a una persona),
            `replied` (contesto en los ultimos 30 dias) o `finished` (termino o salio por otro
            motivo en los ultimos 30 dias).
        total: Inscripciones de la columna (no solo las que se devuelven).
        cards: Las primeras `per_column`.
    """

    key: FollowUpColumnKey
    total: int
    cards: list[FollowUpCard]
