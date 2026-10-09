"""Modelo LeadScore — historial del scoring de un lead (Sprint 17, migracion 027).

Una fila por cada calculo que **cambio** un score (FIT, comportamiento o IA). El valor vigente
vive en las columnas de `leads`; esta tabla explica de donde salio y como evoluciono. La escribe
`app.services.lead_score_service.aplicar_score()`, nunca el codigo de un endpoint a mano.
"""

from datetime import datetime
from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, desc, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel

SCORE_FIT = "fit"
SCORE_BEHAVIORAL = "behavioral"
SCORE_AI = "ai"
#: Debe coincidir con `SCORE_TYPES` de la migracion 027 (un test lo comprueba).
SCORE_TYPES: tuple[str, ...] = (SCORE_FIT, SCORE_BEHAVIORAL, SCORE_AI)

#: Que provoco el calculo. No es un CHECK: es un log y los Sprints 18-19 anaden disparadores.
TRIGGER_ENRICHMENT = "enrichment"
TRIGGER_RECALC = "recalc"
TRIGGER_INBOUND_MESSAGE = "inbound_message"
TRIGGER_ICP_CHANGED = "icp_changed"
TRIGGER_STAGE_CHANGED = "stage_changed"
TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual"


class LeadScore(TenantBaseModel):
    """Un cambio de score de un lead.

    Attributes:
        lead_id: Lead puntuado (`ON DELETE CASCADE`).
        user_id: Quien pidio el calculo; `None` si fue el sistema.
        score_type: `fit`, `behavioral` o `ai`.
        score: Valor nuevo, 0-100.
        previous_score: Valor de la columna antes del calculo (`None` si aun no tenia).
        trigger: Una de las constantes `TRIGGER_*`.
        factors: Explicacion del calculo (dimensiones, puntos y codigos de motivo).
    """

    __tablename__ = "lead_scores"
    __table_args__ = (
        CheckConstraint(
            "score_type IN (" + ", ".join(f"'{t}'" for t in SCORE_TYPES) + ")",
            name="ck_lead_scores_type",
        ),
        CheckConstraint("score BETWEEN 0 AND 100", name="ck_lead_scores_score"),
        CheckConstraint(
            "previous_score IS NULL OR previous_score BETWEEN 0 AND 100",
            name="ck_lead_scores_previous",
        ),
        Index("ix_lead_scores_lead_type", "lead_id", "score_type", desc("created_at")),
    )

    lead_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    score_type: Mapped[str] = mapped_column(String(20), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trigger: Mapped[str] = mapped_column(String(30), nullable=False)
    factors: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
