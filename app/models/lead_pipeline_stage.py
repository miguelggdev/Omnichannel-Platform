"""Modelo LeadPipelineStage — etapas configurables del pipeline de leads (Sprint 16, ADR-082)."""

from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel


class LeadPipelineStage(TenantBaseModel):
    """Una etapa del pipeline de un tenant.

    Attributes:
        name: Nombre visible.
        slug: Identificador estable dentro del tenant (las automatizaciones lo usan).
        position: Orden en el tablero, desde 0.
        color: Color `#RRGGBB` de la columna del Kanban.
        auto_actions: Acciones que dispara entrar en la etapa (Sprints 17-18).
        is_terminal: Si es una etapa final (ganado, perdido, descalificado).
    """

    __tablename__ = "lead_pipeline_stages"
    __table_args__ = (
        UniqueConstraint("client_id", "slug", name="uq_lead_stage_slug"),
        # DEFERRABLE: reordenar (intercambiar dos posiciones) pasa por un estado intermedio
        # con posiciones repetidas; se comprueba al cerrar la transaccion, no por fila.
        UniqueConstraint(
            "client_id",
            "position",
            name="uq_lead_stage_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("position >= 0", name="ck_lead_stage_position"),
        CheckConstraint("color IS NULL OR color ~ '^#[0-9a-fA-F]{6}$'", name="ck_lead_stage_color"),
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(50), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    color: Mapped[str | None] = mapped_column(String(7), nullable=True)
    auto_actions: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    is_terminal: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
