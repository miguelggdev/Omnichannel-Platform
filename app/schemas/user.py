"""Schemas de User — CRUD de usuarios."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


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
    """Cuerpo de `PUT /api/v1/admin/users/{id}`: solo se cambia lo que se envia.

    Attributes:
        first_name: Nombre nuevo.
        last_name: Apellido nuevo.
        role: Rol nuevo. Como en `UserCreate`, `super_admin` no es asignable: es un
            rol de plataforma y permitirlo seria una escalada de privilegios.
        is_active: `False` desactiva al usuario (no puede entrar ni renovar su sesion);
            `True` lo reactiva.
        password: Contrasena nueva (restablecimiento por un administrador).
    """

    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    role: Literal["admin", "supervisor", "agent", "medical"] | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=72)

    @model_validator(mode="after")
    def _al_menos_un_campo(self) -> "UserUpdate":
        """Una peticion sin ningun cambio es un error del cliente, no un no-op silencioso."""
        if not self.model_fields_set or all(
            getattr(self, campo) is None for campo in self.model_fields_set
        ):
            raise ValueError("Indica al menos un campo a cambiar")
        return self


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
