"""Modelos ServiceType y Appointment — catálogo de servicios y citas agendadas.

`service_types` no estaba en el schema original de Sprint 1: la introduce el
Sprint 7 junto con `appointments`, la tabla que el scheduling agent llena al
confirmar una cita en el calendario de Google del tenant.
"""

from datetime import datetime
from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TenantBaseModel

# Estados posibles de `appointments.status`. Se valida en el schema Pydantic,
# no con un tipo ENUM de Postgres: a diferencia de `conversations.status`
# (7 estados fijos desde Sprint 1), esta lista es mas probable que crezca
# (ej. "rescheduled") y no vale la pena el costo de migrar un tipo ENUM cada vez.
APPOINTMENT_STATUSES = ("confirmed", "cancelled", "completed", "no_show")


class ServiceType(TenantBaseModel):
    """Tipo de servicio agendable del tenant (ej. "Consulta", "Corte de cabello").

    Attributes:
        name: Nombre del servicio, único por tenant.
        description: Descripción opcional.
        duration_minutes: Duración de una cita de este tipo.
        buffer_minutes: Tiempo de separación con la siguiente cita.
        is_active: Si el tenant lo sigue ofreciendo (no se borra, se desactiva).
        metadata_: Datos adicionales JSONB.
    """

    __tablename__ = "service_types"
    __table_args__ = (UniqueConstraint("client_id", "name", name="uq_service_types_name"),)

    client_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="60")
    buffer_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="15")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Appointment(TenantBaseModel):
    """Cita agendada, reflejo de un evento en el Google Calendar del tenant.

    `google_event_id` es nullable: la cita se crea primero en la base (para
    responderle al contacto sin esperar la ida y vuelta a la API de Google) y
    se completa en cuanto `GoogleCalendarService` confirma el evento.

    Attributes:
        contact_id: Contacto que agenda la cita.
        conversation_id: Conversación desde la que se agendó (si la hay).
        service_type_id: Tipo de servicio agendado.
        google_event_id: ID del evento en Google Calendar, una vez creado.
        title: Título de la cita.
        starts_at: Inicio de la cita.
        ends_at: Fin de la cita.
        status: Uno de `APPOINTMENT_STATUSES`.
        notes: Notas libres sobre la cita.
        cancelled_reason: Motivo si `status == "cancelled"`.
        metadata_: Datos adicionales JSONB.
    """

    __tablename__ = "appointments"

    client_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False, index=True
    )
    contact_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False, index=True
    )
    conversation_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=True
    )
    service_type_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_types.id"), nullable=False
    )
    google_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="confirmed")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships: `selectin` porque las tools del scheduling agent siempre
    # necesitan el nombre del contacto y del servicio para responderle al LLM,
    # nunca solo los IDs.
    contact: Mapped["Contact"] = relationship("Contact", lazy="selectin")
    service_type: Mapped["ServiceType"] = relationship("ServiceType", lazy="selectin")
