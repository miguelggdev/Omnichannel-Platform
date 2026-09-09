"""Modelo Client — Tabla raíz de tenants.

NO hereda de TenantBaseModel (no tiene client_id propio).
Cada client representa una organización/empresa en la plataforma SaaS.
"""

from datetime import datetime
from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import Boolean, DateTime, Enum, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Client(Base):
    """Tabla raíz de tenants/organizaciones.

    Attributes:
        id: UUID del tenant.
        name: Nombre de la organización.
        slug: Slug único para URLs.
        plan: Plan de suscripción (free, starter, professional, enterprise).
        settings: Configuración JSONB del tenant.
        is_active: Si el tenant está activo.
        admin_assistant_enabled: Si el Admin Assistant está habilitado.
        admin_assistant_voice_enabled: Si la voz del Admin Assistant está activa.
        lead_management_enabled: Si el módulo de leads está activo (ADR-021).
        theme_config: Configuración de tema visual (ADR-025).
        suspension_date: Fecha programada de suspensión por falta de pago.
        payment_alert_config: Config de alertas de pago escalonadas (ADR-015).
        alert_message: Mensaje que recibe el cliente suspendido.
        suspended_at: Timestamp de suspensión efectiva.
    """

    __tablename__ = "clients"

    id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    plan: Mapped[str] = mapped_column(
        Enum(
            "free",
            "starter",
            "professional",
            "enterprise",
            name="plan_type",
            create_type=False,
        ),
        server_default="free",
    )
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")

    # Admin Assistant (ADR-019)
    admin_assistant_enabled: Mapped[bool] = mapped_column(Boolean, server_default="false")
    admin_assistant_voice_enabled: Mapped[bool] = mapped_column(Boolean, server_default="false")

    # Lead Management (ADR-021)
    lead_management_enabled: Mapped[bool] = mapped_column(Boolean, server_default="false")

    # Theme (ADR-025)
    theme_config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")

    # Suspensión por pago (ADR-015)
    suspension_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_alert_config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    alert_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    users: Mapped[list["User"]] = relationship("User", back_populates="client")
    contacts: Mapped[list["Contact"]] = relationship("Contact", back_populates="client")
    conversations: Mapped[list["Conversation"]] = relationship(
        "Conversation", back_populates="client"
    )
    agent_configs: Mapped[list["AgentConfig"]] = relationship(
        "AgentConfig", back_populates="client"
    )
    token_budgets: Mapped[list["TokenBudget"]] = relationship(
        "TokenBudget", back_populates="client"
    )
