"""Llamadas agendadas con un lead (Sprint 19, migracion 029).

La llamada real (audio, transcripcion cifrada) vive en `call_records` (Sprint 13); aqui solo la
cita y su estado, con `call_record_id` hacia ella cuando ocurre.
"""

from datetime import datetime
from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel

CALL_AI_VOICE = "ai_voice"
CALL_HUMAN = "human"
CALL_HYBRID = "hybrid"
#: Debe coincidir con `CALL_TYPES` de la migracion 029.
CALL_TYPES: tuple[str, ...] = (CALL_AI_VOICE, CALL_HUMAN, CALL_HYBRID)

CALL_PENDING = "pending"
CALL_CONFIRMED = "confirmed"
CALL_IN_PROGRESS = "in_progress"
CALL_COMPLETED = "completed"
CALL_NO_SHOW = "no_show"
CALL_CANCELLED = "cancelled"
CALL_RESCHEDULED = "rescheduled"
#: Debe coincidir con `CALL_STATUSES` de la migracion 029.
CALL_STATUSES: tuple[str, ...] = (
    CALL_PENDING,
    CALL_CONFIRMED,
    CALL_IN_PROGRESS,
    CALL_COMPLETED,
    CALL_NO_SHOW,
    CALL_CANCELLED,
    CALL_RESCHEDULED,
)
#: Llamadas que aun ocupan agenda (cuentan para solapes y recordatorios).
UPCOMING_STATUSES: tuple[str, ...] = (CALL_PENDING, CALL_CONFIRMED)


class ScheduledCall(TenantBaseModel):
    """Una llamada agendada.

    Attributes:
        lead_id: Lead (`ON DELETE CASCADE`).
        deal_id: Deal al que pertenece (`SET NULL`).
        assigned_user_id: Quien llama (humana o mixta).
        call_record_id: La llamada real en `call_records`, cuando ocurre.
        call_type: `ai_voice`, `human` o `hybrid`.
        status: `CALL_STATUSES`.
        scheduled_at: Inicio (aware, se guarda en UTC).
        duration_minutes: Duracion, 5-480.
        timezone: Zona del lead, para mostrarle la hora y enviarle los recordatorios.
        ai_voice_provider: Proveedor de voz IA (obligatorio si no es `human`).
        ai_voice_config: Configuracion para ese proveedor (sin secretos).
        outcome: Resultado como codigo.
        notes: Notas (texto libre: la supresion RGPD las anonimiza).
        reminder_24h_sent_at: Cuando se envio el aviso de 24 h.
        reminder_1h_sent_at: Cuando se envio el de 1 h.
        confirmed_at: Cuando el lead confirmo.
        completed_at: Cuando termino.
        cancelled_at: Cuando se cancelo.
    """

    __tablename__ = "scheduled_calls"
    __table_args__ = (
        CheckConstraint(
            "call_type IN (" + ", ".join(f"'{t}'" for t in CALL_TYPES) + ")",
            name="ck_scheduled_calls_type",
        ),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in CALL_STATUSES) + ")",
            name="ck_scheduled_calls_status",
        ),
        CheckConstraint("duration_minutes BETWEEN 5 AND 480", name="ck_scheduled_calls_duration"),
        CheckConstraint(
            "status <> 'completed' OR completed_at IS NOT NULL",
            name="ck_scheduled_calls_completed_at",
        ),
        CheckConstraint(
            "status <> 'confirmed' OR confirmed_at IS NOT NULL",
            name="ck_scheduled_calls_confirmed_at",
        ),
        CheckConstraint(
            "status <> 'cancelled' OR cancelled_at IS NOT NULL",
            name="ck_scheduled_calls_cancelled_at",
        ),
        CheckConstraint(
            "call_type = 'human' OR ai_voice_provider IS NOT NULL",
            name="ck_scheduled_calls_ai_provider",
        ),
        Index(
            "ix_scheduled_calls_upcoming",
            "client_id",
            "scheduled_at",
            postgresql_where=text("status IN ('pending', 'confirmed')"),
        ),
        Index(
            "ix_scheduled_calls_user_upcoming",
            "assigned_user_id",
            "scheduled_at",
            postgresql_where=text("status IN ('pending', 'confirmed')"),
        ),
        Index("ix_scheduled_calls_lead", "lead_id"),
        Index("ix_scheduled_calls_deal", "deal_id"),
    )

    lead_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    deal_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deals.id", ondelete="SET NULL"), nullable=True
    )
    assigned_user_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    call_record_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("call_records.id", ondelete="SET NULL"), nullable=True
    )
    call_type: Mapped[str] = mapped_column(String(20), server_default="human", nullable=False)
    status: Mapped[str] = mapped_column(String(20), server_default="pending", nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, server_default="30", nullable=False)
    timezone: Mapped[str] = mapped_column(
        String(50), server_default="America/Bogota", nullable=False
    )
    ai_voice_provider: Mapped[str | None] = mapped_column(String(30), nullable=True)
    ai_voice_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default="{}", nullable=False
    )
    outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reminder_24h_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_1h_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
