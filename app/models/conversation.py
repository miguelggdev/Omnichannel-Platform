"""Modelo Conversation — Conversaciones con 7 estados.

Estados: new, bot_active, human_active, waiting_human, waiting_client,
resolved, archived.
"""

from datetime import datetime
from uuid import UUID as _UUID

from sqlalchemy import DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TenantBaseModel


class Conversation(TenantBaseModel):
    """Conversación entre un contacto y el sistema (bot o humano).

    Attributes:
        contact_id: FK al contacto participante.
        channel: Canal de comunicación (whatsapp, instagram, facebook, etc.).
        status: Estado actual de la conversación (7 estados posibles).
        assigned_user_id: FK al agente humano asignado (nullable).
        subject: Tema de la conversación (opcional).
        metadata_: Datos adicionales JSONB.
        last_message_at: Timestamp del último mensaje.
        resolved_at: Timestamp de resolución.
    """

    __tablename__ = "conversations"

    client_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False, index=True
    )
    contact_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(
            "new",
            "bot_active",
            "human_active",
            "waiting_human",
            "waiting_client",
            "resolved",
            "archived",
            name="conversation_status",
            create_type=False,
        ),
        server_default="new",
    )
    assigned_user_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    client: Mapped["Client"] = relationship("Client", back_populates="conversations")
    contact: Mapped["Contact"] = relationship("Contact", back_populates="conversations")
    assigned_user: Mapped["User | None"] = relationship("User")
    messages: Mapped[list["Message"]] = relationship("Message", back_populates="conversation")
