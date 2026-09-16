"""Schemas de Contact — CRUD de contactos."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ContactCreate(BaseModel):
    """Schema para crear un contacto."""

    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    display_name: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = {}


class ContactUpdate(BaseModel):
    """Schema para actualizar un contacto."""

    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None
    metadata: dict[str, Any] | None = None


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


class IdentifierResponse(BaseModel):
    """Identificador de un contacto en un canal concreto.

    El modelo `ContactIdentifier` de Sprint 1 no tiene `is_primary` ni
    `verified_at`, que la spec (§13) da por hechos: solo `channel` y
    `identifier_value`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    channel: str
    identifier_value: str
    created_at: datetime


class ContactTagResponse(BaseModel):
    """Etiqueta asignada a un contacto, con el nombre y color resueltos."""

    id: UUID
    name: str
    color: str | None


class ContactNoteResponse(BaseModel):
    """Nota interna tal como aparece dentro del detalle de un contacto."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    author_id: UUID
    content: str
    created_at: datetime


class ContactDetailResponse(ContactResponse):
    """Contacto con sus identificadores, etiquetas y notas recientes."""

    metadata: dict[str, Any] = {}
    identifiers: list[IdentifierResponse] = []
    tags: list[ContactTagResponse] = []
    notes: list[ContactNoteResponse] = []
