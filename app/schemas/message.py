"""Schemas de Message — CRUD de mensajes."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MessageCreate(BaseModel):
    """Schema para crear un mensaje."""

    conversation_id: UUID
    direction: str = Field(pattern=r"^(inbound|outbound)$")
    message_type: str = "text"
    content: str | None = None
    media_url: str | None = None
    sender_type: str = Field(max_length=20)
    sender_id: UUID | None = None
    metadata: dict = {}


class MessageUpdate(BaseModel):
    """Schema para actualizar un mensaje."""

    content: str | None = None
    metadata: dict | None = None


class MessageResponse(BaseModel):
    """Schema de respuesta de mensaje."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    client_id: UUID
    conversation_id: UUID
    direction: str
    message_type: str
    content: str | None
    media_url: str | None
    external_message_id: str | None
    sender_type: str
    sender_id: UUID | None
    created_at: datetime
    updated_at: datetime


class MessageListResponse(BaseModel):
    """Schema de lista de mensajes."""

    items: list[MessageResponse]
    total: int
    page: int
    page_size: int
