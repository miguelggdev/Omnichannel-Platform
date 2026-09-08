"""Modelo Message — Mensajes entrantes y salientes."""

from datetime import datetime
from uuid import UUID as _UUID

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TenantBaseModel


class Message(TenantBaseModel):
    """Mensaje individual dentro de una conversación.

    Attributes:
        conversation_id: FK a la conversación.
        direction: inbound (del contacto) o outbound (del sistema/agente).
        message_type: text, image, audio, video, document, location, template, interactive.
        content: Contenido textual del mensaje.
        media_url: URL del archivo multimedia (si aplica).
        external_message_id: ID del mensaje en el proveedor externo.
        sender_type: bot, human, contact.
        sender_id: UUID del usuario que envió (si es humano).
        metadata_: Datos adicionales (provider-specific).
    """

    __tablename__ = "messages"

    conversation_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(
        Enum("inbound", "outbound", name="message_direction", create_type=False),
        nullable=False,
    )
    message_type: Mapped[str] = mapped_column(
        Enum(
            "text",
            "image",
            "audio",
            "video",
            "document",
            "location",
            "template",
            "interactive",
            name="message_type",
            create_type=False,
        ),
        server_default="text",
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    external_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    sender_type: Mapped[str] = mapped_column(String(20), nullable=False)
    sender_id: Mapped[_UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    conversation: Mapped["Conversation"] = relationship("Conversation", back_populates="messages")
