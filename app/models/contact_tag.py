"""Modelo ContactTag — Relación N:M entre contactos y tags."""

from uuid import UUID as _UUID

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TenantBaseModel


class ContactTag(TenantBaseModel):
    """Relación many-to-many entre contactos y tags.

    Attributes:
        contact_id: FK al contacto.
        tag_id: FK a la etiqueta.
    """

    __tablename__ = "contact_tags"
    __table_args__ = (UniqueConstraint("contact_id", "tag_id", name="uq_contact_tag"),)

    contact_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False, index=True
    )
    tag_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tags.id"), nullable=False, index=True
    )

    # Relationships
    contact: Mapped["Contact"] = relationship("Contact", back_populates="tags")
    tag: Mapped["Tag"] = relationship("Tag", back_populates="contact_tags")
