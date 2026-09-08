"""Modelo AgentConfig — Configuración del agente IA por tenant."""

from uuid import UUID as _UUID

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TenantBaseModel


class AgentConfig(TenantBaseModel):
    """Configuración del agente de IA para un tenant.

    Attributes:
        name: Nombre del agente.
        system_prompt: Prompt del sistema para el LLM.
        welcome_message: Mensaje de bienvenida al iniciar conversación.
        model: Modelo LLM a usar (default: gpt-4o).
        temperature: Temperatura del LLM (0.0 - 1.0).
        max_tokens: Máximo de tokens por respuesta.
        training_mode: Si el modo entrenamiento está activo (ADR-005).
        similarity_threshold: Umbral de similaridad para few-shot (0.80).
        handoff_message: Mensaje al transferir a humano.
        config: Configuración adicional JSONB.
    """

    __tablename__ = "agent_configs"

    client_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    welcome_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String(50), server_default="gpt-4o")
    temperature: Mapped[float] = mapped_column(server_default="0.7")
    max_tokens: Mapped[int] = mapped_column(Integer, server_default="1024")
    training_mode: Mapped[bool] = mapped_column(Boolean, server_default="false")
    similarity_threshold: Mapped[float] = mapped_column(server_default="0.80")
    handoff_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")

    # Relationships
    client: Mapped["Client"] = relationship("Client", back_populates="agent_configs")
