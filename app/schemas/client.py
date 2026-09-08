"""Schemas de Client — CRUD de tenants."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ClientCreate(BaseModel):
    """Schema para crear un nuevo tenant.

    Attributes:
        name: Nombre de la organización.
        slug: Slug único para URLs.
        plan: Plan de suscripción.
    """

    name: str = Field(max_length=255)
    slug: str = Field(max_length=100, pattern=r"^[a-z0-9-]+$")
    plan: str = "free"
    settings: dict = {}


class ClientUpdate(BaseModel):
    """Schema para actualizar un tenant."""

    name: str | None = None
    plan: str | None = None
    settings: dict | None = None
    is_active: bool | None = None
    admin_assistant_enabled: bool | None = None
    admin_assistant_voice_enabled: bool | None = None
    lead_management_enabled: bool | None = None
    theme_config: dict | None = None
    alert_message: str | None = None


class ClientResponse(BaseModel):
    """Schema de respuesta de tenant."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    plan: str
    settings: dict
    is_active: bool
    admin_assistant_enabled: bool
    admin_assistant_voice_enabled: bool
    lead_management_enabled: bool
    theme_config: dict
    created_at: datetime
    updated_at: datetime


class ClientListResponse(BaseModel):
    """Schema de lista de tenants."""

    items: list[ClientResponse]
    total: int
    page: int
    page_size: int
