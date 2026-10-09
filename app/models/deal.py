"""Deals: oportunidades de venta de un lead (Sprint 19, migracion 029)."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel

DEAL_NEW_CONTACT = "new_contact"
DEAL_QUALIFIED = "qualified"
DEAL_PROPOSAL = "proposal"
DEAL_NEGOTIATION = "negotiation"
DEAL_WON = "closed_won"
DEAL_LOST = "closed_lost"
#: Debe coincidir con `DEAL_STAGES` de la migracion 029 (un test lo comprueba).
DEAL_STAGES: tuple[str, ...] = (
    DEAL_NEW_CONTACT,
    DEAL_QUALIFIED,
    DEAL_PROPOSAL,
    DEAL_NEGOTIATION,
    DEAL_WON,
    DEAL_LOST,
)
#: Etapas en las que el deal sigue en juego (cuentan para el pipeline).
OPEN_STAGES: tuple[str, ...] = (DEAL_NEW_CONTACT, DEAL_QUALIFIED, DEAL_PROPOSAL, DEAL_NEGOTIATION)
CLOSED_STAGES: tuple[str, ...] = (DEAL_WON, DEAL_LOST)


class Deal(TenantBaseModel):
    """Una oportunidad de venta.

    Attributes:
        lead_id: Lead del que sale (`NO ACTION`: la historia de ingresos no se borra en cascada).
        assigned_user_id: Responsable (`SET NULL` si se borra el usuario).
        title: Titulo (texto del tenant).
        value: Importe, >= 0, en `currency`.
        currency: Moneda ISO 4217 (3 letras). Los importes de monedas distintas nunca se suman.
        stage: Etapa (`DEAL_STAGES`).
        probability: Probabilidad de cierre 0-100.
        expected_close_date: Cierre previsto.
        actual_close_date: Dia en que se gano o perdio.
        won_at: Cuando se gano.
        lost_at: Cuando se perdio.
        lost_reason: Motivo de perdida (texto libre: la supresion RGPD lo anonimiza).
        notes: Notas (texto libre: idem).
        metadata_: Datos extra del tenant.
        stage_changed_at: Desde cuando esta en su etapa.
    """

    __tablename__ = "deals"
    __table_args__ = (
        CheckConstraint(
            "stage IN (" + ", ".join(f"'{s}'" for s in DEAL_STAGES) + ")", name="ck_deals_stage"
        ),
        CheckConstraint("probability BETWEEN 0 AND 100", name="ck_deals_probability"),
        CheckConstraint("value >= 0", name="ck_deals_value"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_deals_currency"),
        CheckConstraint("stage <> 'closed_won' OR won_at IS NOT NULL", name="ck_deals_won_at"),
        CheckConstraint("stage <> 'closed_lost' OR lost_at IS NOT NULL", name="ck_deals_lost_at"),
        CheckConstraint("won_at IS NULL OR lost_at IS NULL", name="ck_deals_won_xor_lost"),
        Index("ix_deals_client_stage", "client_id", "stage"),
        Index(
            "ix_deals_client_value",
            "client_id",
            text("value DESC"),
            postgresql_where=text("stage NOT IN ('closed_won', 'closed_lost')"),
        ),
        Index("ix_deals_lead", "lead_id"),
        Index(
            "ix_deals_client_won_at",
            "client_id",
            "won_at",
            postgresql_where=text("stage = 'closed_won'"),
        ),
    )

    lead_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id"), nullable=False
    )
    assigned_user_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(12, 2), server_default="0", nullable=False)
    currency: Mapped[str] = mapped_column(String(3), server_default="USD", nullable=False)
    stage: Mapped[str] = mapped_column(String(20), server_default="new_contact", nullable=False)
    probability: Mapped[int] = mapped_column(Integer, server_default="10", nullable=False)
    expected_close_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    actual_close_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    won_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lost_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lost_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default="{}", nullable=False
    )
    stage_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
