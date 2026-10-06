"""Modelo LeadActivity — historial de un lead (Sprint 16, ADR-084).

Cada fila es un hecho (se creo, cambio de etapa, se asigno...). Lleva **ids y slugs en
`metadata_`, nunca datos personales**: asi la anonimizacion RGPD del lead no tiene que tocar su
historial, y el historial no vuelve a exponer lo que se borro. `description` queda para texto
corto sin datos de la persona.
"""

from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import ForeignKey, Index, String, Text, desc
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel

#: Tipos de actividad que escribe el modulo de leads. No es un CHECK: es un log, y los
#: Sprints 17-19 anaden tipos (enriquecimiento, scoring, llamadas) sin migrar.
ACTIVITY_CREATED = "created"
ACTIVITY_CAPTURED = "captured"
ACTIVITY_RECAPTURED = "recaptured"
ACTIVITY_IMPORTED = "imported"
ACTIVITY_STAGE_CHANGED = "stage_changed"
ACTIVITY_ASSIGNED = "assigned"
ACTIVITY_UNASSIGNED = "unassigned"
ACTIVITY_UPDATED = "updated"
ACTIVITY_CONTACT_LINKED = "contact_linked"
ACTIVITY_CONTACT_UNLINKED = "contact_unlinked"
ACTIVITY_CONTACT_CREATED = "contact_created"
ACTIVITY_DELETED = "deleted"
ACTIVITY_ANONYMIZED = "gdpr_anonymized"
ACTIVITY_INBOUND_MESSAGE = "inbound_message"


class LeadActivity(TenantBaseModel):
    """Una entrada del historial de un lead.

    Attributes:
        lead_id: Lead al que pertenece (`ON DELETE CASCADE`).
        user_id: Quien lo hizo; `None` si lo hizo el sistema (captura publica) o el usuario se
            borro (`ON DELETE SET NULL`).
        activity_type: Una de las constantes `ACTIVITY_*`.
        description: Texto corto opcional, sin datos personales.
        metadata_: Datos del hecho (ids, slugs, nombres de campo), sin datos personales.
    """

    __tablename__ = "lead_activities"
    __table_args__ = (Index("ix_lead_activities_lead", "lead_id", desc("created_at")),)

    lead_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    activity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default="{}", nullable=False
    )
