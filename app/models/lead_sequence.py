"""Modelos de las secuencias de follow-up de leads (Sprint 18, migracion 028).

- `LeadSequence`: la secuencia y como se dispara.
- `LeadSequenceStep`: un paso; su `config` lo valida `app.schemas.lead_sequence` segun el tipo.
- `LeadSequenceEnrollment`: un lead dentro de una secuencia y por donde va.

Ninguna de las tres guarda datos personales: los mensajes que envia un paso son plantillas del
tenant, y lo que llega a enviarse queda en `messages` (que ya cubre el RGPD del contacto).
"""

from datetime import datetime
from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel

STEP_MESSAGE = "message"
STEP_WAIT = "wait"
STEP_CONDITION = "condition"
STEP_TASK = "task"
#: Debe coincidir con `STEP_TYPES` de la migracion 028 (un test lo comprueba).
STEP_TYPES: tuple[str, ...] = (STEP_MESSAGE, STEP_WAIT, STEP_CONDITION, STEP_TASK)

ENROLLMENT_ACTIVE = "active"
ENROLLMENT_PAUSED = "paused"
ENROLLMENT_COMPLETED = "completed"
ENROLLMENT_EXITED = "exited"
#: Debe coincidir con `ENROLLMENT_STATUSES` de la migracion 028.
ENROLLMENT_STATUSES: tuple[str, ...] = (
    ENROLLMENT_ACTIVE,
    ENROLLMENT_PAUSED,
    ENROLLMENT_COMPLETED,
    ENROLLMENT_EXITED,
)
#: Inscripciones "vivas": las que cuentan para el UNIQUE parcial (un lead, una vez por secuencia).
LIVE_STATUSES: tuple[str, ...] = (ENROLLMENT_ACTIVE, ENROLLMENT_PAUSED)

#: Motivos de salida. Son codigos, nunca texto libre (podria llevar datos de la persona).
EXIT_REPLIED = "replied"
EXIT_NEGATIVE_REPLY = "negative_reply"
EXIT_UNSUBSCRIBED = "unsubscribed"
EXIT_BOUNCED = "bounced"
EXIT_LEAD_CLOSED = "lead_closed"
EXIT_LEAD_DELETED = "lead_deleted"
EXIT_GDPR = "gdpr"
EXIT_MANUAL = "manual"
EXIT_CONDITION = "condition"
EXIT_NO_CHANNEL = "no_channel"
EXIT_SEQUENCE_DISABLED = "sequence_disabled"
EXIT_STEP_LIMIT = "step_limit"
EXIT_INVALID_STEP = "invalid_step"


class LeadSequence(TenantBaseModel):
    """Una secuencia de follow-up del tenant.

    Attributes:
        name: Nombre, unico por tenant.
        description: Descripcion libre (del tenant, no de un lead).
        trigger_conditions: Cuando inscribir a un lead automaticamente (ver `TriggerConditions`).
        channel_priority: Canales en orden de preferencia para los pasos `message` en `auto`.
        is_active: Una secuencia apagada no inscribe ni avanza a nadie.
        created_by_user_id: Quien la creo (`SET NULL` si se borra el usuario).
    """

    __tablename__ = "lead_sequences"
    __table_args__ = (UniqueConstraint("client_id", "name", name="uq_lead_sequence_name"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger_conditions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default="{}", nullable=False
    )
    channel_priority: Mapped[list[str]] = mapped_column(
        JSONB, server_default='["whatsapp", "email", "instagram"]', nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    created_by_user_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class LeadSequenceStep(TenantBaseModel):
    """Un paso de una secuencia.

    Attributes:
        sequence_id: Secuencia (`ON DELETE CASCADE`).
        position: Orden, desde 1 (UNIQUE por secuencia, diferible).
        step_type: `message`, `wait`, `condition` o `task`.
        config: Configuracion del paso (ver `app.schemas.lead_sequence`).
    """

    __tablename__ = "lead_sequence_steps"
    __table_args__ = (
        UniqueConstraint(
            "sequence_id",
            "position",
            name="uq_lead_sequence_step_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("position >= 1", name="ck_lead_sequence_step_position"),
        CheckConstraint(
            "step_type IN (" + ", ".join(f"'{t}'" for t in STEP_TYPES) + ")",
            name="ck_lead_sequence_step_type",
        ),
    )

    sequence_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lead_sequences.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(String(20), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LeadSequenceEnrollment(TenantBaseModel):
    """Un lead inscrito en una secuencia.

    Attributes:
        lead_id: Lead (`ON DELETE CASCADE`).
        sequence_id: Secuencia (`NO ACTION`: una secuencia con inscripciones se desactiva).
        enrolled_by_user_id: Quien lo inscribio; `None` si fue un disparador automatico.
        current_step: Posicion del paso que toca ejecutar.
        status: `active`, `paused`, `completed` o `exited`.
        steps_executed: Pasos ejecutados (tope contra bucles de condiciones).
        next_step_at: Cuando toca el siguiente paso (`None` = ya).
        last_step_at: Cuando se ejecuto el ultimo.
        completed_at: Cuando termino o salio.
        exit_reason: Codigo `EXIT_*` si salio antes de terminar.
    """

    __tablename__ = "lead_sequence_enrollments"
    __table_args__ = (
        CheckConstraint("current_step >= 1", name="ck_lead_enrollment_step"),
        CheckConstraint("steps_executed >= 0", name="ck_lead_enrollment_executed"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in ENROLLMENT_STATUSES) + ")",
            name="ck_lead_enrollment_status",
        ),
        CheckConstraint(
            "status NOT IN ('completed', 'exited') OR completed_at IS NOT NULL",
            name="ck_lead_enrollment_closed_at",
        ),
        Index(
            "uq_lead_enrollment_live",
            "lead_id",
            "sequence_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'paused')"),
        ),
        Index(
            "ix_lead_sequence_enrollments_due",
            "client_id",
            "next_step_at",
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_lead_sequence_enrollments_sequence", "sequence_id", "status"),
        Index("ix_lead_sequence_enrollments_lead", "lead_id"),
    )

    lead_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    sequence_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lead_sequences.id"), nullable=False
    )
    enrolled_by_user_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    current_step: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    status: Mapped[str] = mapped_column(String(20), server_default="active", nullable=False)
    steps_executed: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    next_step_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_step_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
