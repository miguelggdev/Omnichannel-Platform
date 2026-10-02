"""Schemas de User — CRUD de usuarios."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    """Schema para crear un nuevo usuario.

    Attributes:
        email: Email único del usuario.
        password: Password (mínimo 8 caracteres).
        first_name: Nombre del usuario.
        last_name: Apellido del usuario.
        role: Rol asignado. `super_admin` no se puede asignar por la API: es
            un rol de plataforma, no del tenant, y permitirlo seria una
            escalada de privilegios para cualquier admin.
    """

    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    role: Literal["admin", "supervisor", "agent", "medical"] = "agent"


class UserUpdate(BaseModel):
    """Schema para actualizar un usuario."""

    first_name: str | None = None
    last_name: str | None = None
    role: str | None = None
    is_active: bool | None = None


class UserResponse(BaseModel):
    """Schema de respuesta de usuario (sin password_hash)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    client_id: UUID
    email: str
    first_name: str
    last_name: str
    role: str
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UserListResponse(BaseModel):
    """Schema de lista de usuarios."""

    items: list[UserResponse]
    total: int
    page: int
    page_size: int
