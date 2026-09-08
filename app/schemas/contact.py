"""Schemas de Contact — CRUD de contactos."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ContactCreate(BaseModel):
    """Schema para crear un contacto."""

    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    display_name: str | None = Field(default=None, max_length=200)
    metadata: dict = {}


class ContactUpdate(BaseModel):
    """Schema para actualizar un contacto."""

    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None
    metadata: dict | None = None


class ContactResponse(BaseModel):
    """Schema de respuesta de contacto."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    client_id: UUID
    first_name: str | None
    last_name: str | None
    display_name: str | None
    merged_into_id: UUID | None
    created_at: datetime
    updated_at: datetime


class ContactListResponse(BaseModel):
    """Schema de lista de contactos."""

    items: list[ContactResponse]
    total: int
    page: int
    page_size: int
