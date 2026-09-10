"""Modelo InternalNote — Notas internas sobre contactos."""

from uuid import UUID as _UUID

from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TenantBaseModel


class InternalNote(TenantBaseModel):
    """Notas internas que los agentes/admins escriben sobre un contacto.

    Attributes:
        contact_id: FK al contacto sobre el que se escribe la nota.
        author_id: FK al usuario que escribió la nota.
        content: Contenido de la nota.
    """

    __tablename__ = "internal_notes"

    contact_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False, index=True
    )
    author_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Relationships
    contact: Mapped["Contact"] = relationship("Contact", back_populates="notes")
    author: Mapped["User"] = relationship("User", back_populates="internal_notes")
