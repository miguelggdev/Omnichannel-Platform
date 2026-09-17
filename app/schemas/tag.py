"""Schemas de Tag — etiquetas configurables por tenant."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TagCreate(BaseModel):
    """Schema para crear una etiqueta."""

    name: str = Field(min_length=1, max_length=100)
    color: str | None = Field(
        default=None,
        max_length=7,
        pattern=r"^#[0-9A-Fa-f]{6}$",
        description="Color hex de 6 digitos con almohadilla, ej. #FF5733",
    )


class TagResponse(BaseModel):
    """Schema de respuesta de una etiqueta."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    color: str | None
    created_at: datetime
