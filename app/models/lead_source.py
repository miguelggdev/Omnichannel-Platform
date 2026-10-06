"""Modelo LeadSource — fuentes de captura de leads por tenant (Sprint 16, ADR-082)."""

from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel

#: Tipos de fuente admitidos. Es un CHECK y no un ENUM de PostgreSQL: anadir un valor no
#: exige `ALTER TYPE` (no transaccional). Debe coincidir con la migracion 025.
LEAD_SOURCE_TYPES: tuple[str, ...] = (
    "web_form",
    "linkedin",
    "facebook_ad",
    "google_ad",
    "instagram",
    "referral",
    "manual",
    "api",
    "whatsapp",
    "import",
)


class LeadSource(TenantBaseModel):
    """Una fuente de leads (formulario web, anuncio, importacion...).

    Attributes:
        name: Nombre visible.
        source_type: Uno de `LEAD_SOURCE_TYPES`.
        config: Ajustes propios de la fuente (campos del formulario, mapeo CSV...).
        utm_tracking: Parametros UTM por defecto.
        is_active: Si acepta capturas nuevas.
        capture_token_hash: SHA-256 del token del endpoint publico de captura. El token en
            claro se muestra una sola vez al crearlo y no se guarda.
    """

    __tablename__ = "lead_sources"
    __table_args__ = (
        CheckConstraint(
            "source_type IN (" + ", ".join(f"'{t}'" for t in LEAD_SOURCE_TYPES) + ")",
            name="ck_lead_source_type",
        ),
        Index(
            "ix_lead_sources_capture_token_hash",
            "capture_token_hash",
            unique=True,
            postgresql_where=text("capture_token_hash IS NOT NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    utm_tracking: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    capture_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
