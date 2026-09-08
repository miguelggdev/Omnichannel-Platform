"""Modelo TokenBudget — Presupuesto mensual de tokens por tenant (ADR-004)."""

from uuid import UUID as PyUUID

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TenantBaseModel


class TokenBudget(TenantBaseModel):
    """Presupuesto mensual de tokens de un tenant.

    Attributes:
        month: Mes del presupuesto en formato YYYY-MM.
        total_budget: Presupuesto total en tokens.
        used_tokens: Tokens consumidos en el mes.
        model_default: Modelo por defecto del tenant.
    """

    __tablename__ = "token_budgets"

    client_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False, index=True
    )
    month: Mapped[str] = mapped_column(String(7), nullable=False)
    total_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    used_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    model_default: Mapped[str] = mapped_column(String(50), server_default="gpt-4o")

    # Relationships
    client: Mapped["Client"] = relationship("Client", back_populates="token_budgets")
