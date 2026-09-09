"""Modelo Contact — Contactos/clientes finales por tenant.

Soporta merge de contactos duplicados vía merged_into_id.
"""

from datetime import datetime
from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TenantBaseModel


class Contact(TenantBaseModel):
    """Contactos/clientes finales del tenant.

    Attributes:
        first_name: Nombre del contacto.
        last_name: Apellido del contacto.
        display_name: Nombre para mostrar (puede ser diferente).
        merged_into_id: Si fue mergeado, apunta al contacto destino.
        metadata: Datos adicionales JSONB.
    """

    __tablename__ = "contacts"

    client_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False, index=True
    )
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    merged_into_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=True
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    client: Mapped["Client"] = relationship("Client", back_populates="contacts")
    identifiers: Mapped[list["ContactIdentifier"]] = relationship(
        "ContactIdentifier", back_populates="contact"
    )
    tags: Mapped[list["ContactTag"]] = relationship("ContactTag", back_populates="contact")
    notes: Mapped[list["InternalNote"]] = relationship("InternalNote", back_populates="contact")
    conversations: Mapped[list["Conversation"]] = relationship(
        "Conversation", back_populates="contact"
    )
    merged_into: Mapped["Contact | None"] = relationship("Contact", remote_side="Contact.id")
