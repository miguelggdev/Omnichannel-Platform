"""Schemas de InternalNote — notas internas del equipo sobre un contacto."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class NoteCreate(BaseModel):
    """Schema para crear una nota interna."""

    content: str = Field(min_length=1)


class NoteResponse(BaseModel):
    """Schema de respuesta de una nota interna.

    `author_id` es el nombre real de la columna en `internal_notes`; la spec la
    llama `user_id` (`specs/sprint-07-scheduling-crm.md` §13), pero el modelo de
    Sprint 1 nunca tuvo ese campo.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    contact_id: UUID
    author_id: UUID
    content: str
    created_at: datetime
