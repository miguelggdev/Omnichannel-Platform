"""Modelo User — Usuarios humanos por tenant.

Roles: super_admin, admin, supervisor, agent.
Password hasheado con bcrypt (ver app.core.security).
"""

from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TenantBaseModel


class User(TenantBaseModel):
    """Usuarios humanos del sistema (agentes, admins, supervisores).

    Attributes:
        email: Email único del usuario (cifrado en DB con pgcrypto).
        password_hash: Hash bcrypt del password.
        first_name: Nombre del usuario.
        last_name: Apellido del usuario.
        role: Rol del usuario (super_admin, admin, supervisor, agent).
        is_active: Si el usuario está activo.
        last_login_at: Último login exitoso.
    """

    __tablename__ = "users"

    client_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(
        Enum(
            "super_admin", "admin", "supervisor", "agent",
            name="user_role",
            create_type=False,
        ),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    client: Mapped["Client"] = relationship("Client", back_populates="users")
    internal_notes: Mapped[list["InternalNote"]] = relationship(
        "InternalNote", back_populates="author"
    )
