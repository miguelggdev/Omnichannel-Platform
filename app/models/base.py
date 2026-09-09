"""Base models para SQLAlchemy 2.0.

Define DeclarativeBase y TenantBaseModel (abstracto con id, client_id, created_at).
Todos los modelos multi-tenant heredan de TenantBaseModel.
Client es la excepción: hereda de Base directamente (es la tabla raíz).
"""

from datetime import datetime
from uuid import UUID as _UUID

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base declarativa de SQLAlchemy 2.0."""

    pass


class TenantBaseModel(Base):
    """Modelo base abstracto para tablas multi-tenant.

    Todas las tablas con aislamiento por tenant heredan de este modelo.
    Provee id (UUID), client_id (FK a clients) y created_at.

    Attributes:
        id: Clave primaria UUID generada por PostgreSQL.
        client_id: UUID del tenant propietario (índice para RLS).
        created_at: Timestamp de creación con timezone.
    """

    __abstract__ = True

    id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    client_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("clients.id"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
