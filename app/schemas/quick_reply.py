"""Schemas de QuickReply — respuestas rapidas predefinidas por tenant."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Un atajo se teclea en medio de una conversacion, asi que se acota a algo que
# se pueda escribir de un tiron: barra inicial y despues letras, numeros, guion
# o guion bajo.
SHORTCUT_PATTERN = r"^/[a-z0-9][a-z0-9_-]{0,48}$"


class QuickReplyCreate(BaseModel):
    """Schema para crear una respuesta rapida."""

    shortcut: str = Field(
        max_length=50,
        pattern=SHORTCUT_PATTERN,
        description="Atajo en minusculas, empezando por '/', ej. /saludo",
    )
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    category: str | None = Field(default=None, max_length=100)


class QuickReplyUpdate(BaseModel):
    """Schema para actualizar una respuesta rapida."""

    shortcut: str | None = Field(default=None, max_length=50, pattern=SHORTCUT_PATTERN)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=1)
    category: str | None = Field(default=None, max_length=100)


class QuickReplyResponse(BaseModel):
    """Schema de respuesta de una respuesta rapida."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    shortcut: str
    title: str
    content: str
    category: str | None
    created_by: UUID | None
    created_at: datetime


class QuickReplyRenderRequest(BaseModel):
    """Body para resolver las variables de una respuesta rapida."""

    conversation_id: UUID = Field(
        description="Conversacion desde la que se usa; de ahi salen el contacto y el ticket"
    )


class QuickReplyRenderResponse(BaseModel):
    """Contenido de la respuesta rapida con las variables ya resueltas."""

    shortcut: str
    content: str
    unresolved: list[str] = []
