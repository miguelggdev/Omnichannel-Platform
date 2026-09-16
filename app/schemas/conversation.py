"""Schemas de Conversation — CRUD de conversaciones."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreate(BaseModel):
    """Schema para crear una conversación."""

    contact_id: UUID
    channel: str = Field(max_length=50)
    subject: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] = {}


class ConversationUpdate(BaseModel):
    """Schema para actualizar una conversación."""

    status: str | None = None
    assigned_user_id: UUID | None = None
    subject: str | None = None
    metadata: dict[str, Any] | None = None


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


class ConversationMessageResponse(BaseModel):
    """Mensaje tal como aparece dentro del detalle de una conversacion."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    direction: str
    message_type: str
    content: str | None
    media_url: str | None
    sender_type: str
    sender_id: UUID | None
    created_at: datetime


class ConversationDetailResponse(ConversationResponse):
    """Conversacion con una pagina de mensajes en orden cronologico.

    `started_at` (spec §13) no existe en el modelo: el inicio de la conversacion
    es `created_at`, que ya viene heredado de `ConversationResponse`.
    """

    messages: list[ConversationMessageResponse] = []
    total_messages: int = 0
    page: int = 1
    page_size: int = 50


class ConversationAssignRequest(BaseModel):
    """Body para asignar una conversacion a un agente humano."""

    user_id: UUID = Field(description="UUID del agente humano a asignar")


class ConversationStatusChangeRequest(BaseModel):
    """Body para cambiar el estado de una conversacion."""

    status: str = Field(description="Nuevo estado de la conversacion")
