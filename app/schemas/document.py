"""Schemas de Document — CRUD de documentos del knowledge base."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DocumentCreate(BaseModel):
    """Schema para crear un documento."""

    title: str = Field(max_length=500)
    file_url: str | None = None
    file_type: str | None = None
    file_size: int | None = None
    content: str | None = None
    metadata: dict[str, Any] = {}


class DocumentUpdate(BaseModel):
    """Schema para actualizar un documento."""

    title: str | None = None
    content: str | None = None
    status: str | None = None
    metadata: dict[str, Any] | None = None


class DocumentResponse(BaseModel):
    """Schema de respuesta de documento."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    client_id: UUID
    title: str
    file_url: str | None
    file_type: str | None
    file_size: int | None
    chunk_count: int
    status: str
    created_at: datetime
    updated_at: datetime


class DocumentListResponse(BaseModel):
    """Schema de lista de documentos."""

    items: list[DocumentResponse]
    total: int
    page: int
    page_size: int
