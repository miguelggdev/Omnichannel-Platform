"""Schemas de Message — CRUD de mensajes y NormalizedMessage."""

from datetime import datetime
from enum import StrEnum
from typing import Any
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
    metadata: dict[str, Any] = {}


class MessageUpdate(BaseModel):
    """Schema para actualizar un mensaje."""

    content: str | None = None
    metadata: dict[str, Any] | None = None


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


class ChannelEnum(StrEnum):
    """Canal de origen/destino de un mensaje, independiente del proveedor."""

    whatsapp = "whatsapp"
    telegram = "telegram"
    instagram = "instagram"
    webchat = "webchat"
    email = "email"
    phone = "phone"
    facebook = "facebook"


class MessageTypeEnum(StrEnum):
    """Tipo de contenido de un mensaje normalizado."""

    text = "text"
    image = "image"
    audio = "audio"
    video = "video"
    document = "document"
    location = "location"
    template = "template"
    interactive = "interactive"


class NormalizedMessage(BaseModel):
    """Mensaje normalizado, independiente del proveedor (PAT-002).

    Todo webhook entrante se convierte a este formato antes de encolarse: el
    worker de Celery y, mas adelante, el grafo de agentes solo conocen este
    contrato, nunca el payload crudo de YCloud o Meta.

    Attributes:
        channel: Canal de origen del mensaje.
        sender_identifier: Telefono, PSID o username del remitente segun el canal.
        text: Cuerpo de texto del mensaje, si lo tiene.
        media_url: URL del recurso multimedia adjunto, si lo hay.
        media_type: Tipo del recurso multimedia adjunto.
        timestamp: Momento en que el proveedor registro el mensaje, con timezone.
        external_message_id: ID unico del mensaje en el proveedor de origen —
            clave de deduplicacion.
        raw_payload: Payload original del webhook, para debugging y auditoria.
        location: Coordenadas `{"latitude": ..., "longitude": ...}` si el
            mensaje es de ubicacion.
        interactive_response: Payload de la respuesta a un boton/quick reply.
    """

    channel: ChannelEnum
    sender_identifier: str
    text: str | None = None
    media_url: str | None = None
    media_type: MessageTypeEnum | None = None
    timestamp: datetime
    external_message_id: str
    raw_payload: dict[str, Any]

    location: dict[str, Any] | None = None
    interactive_response: dict[str, Any] | None = None
