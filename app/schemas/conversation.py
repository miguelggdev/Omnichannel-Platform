"""Schemas de Conversation — CRUD de conversaciones."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreate(BaseModel):
    """Schema para crear una conversación."""

    contact_id: UUID
    channel: str = Field(max_length=50)
    subject: str | None = Field(default=None, max_length=255)
    metadata: dict = {}


class ConversationUpdate(BaseModel):
    """Schema para actualizar una conversación."""

    status: str | None = None
    assigned_user_id: UUID | None = None
    subject: str | None = None
    metadata: dict | None = None


class ConversationResponse(BaseModel):
    """Schema de respuesta de conversación."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    client_id: UUID
    contact_id: UUID
    channel: str
    status: str
    assigned_user_id: UUID | None
    subject: str | None
    last_message_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(BaseModel):
    """Schema de lista de conversaciones."""

    items: list[ConversationResponse]
    total: int
    page: int
    page_size: int
