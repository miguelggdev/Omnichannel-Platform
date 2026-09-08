"""Modelo ContactIdentifier — Teléfonos, emails por contacto (multi-canal).

Columnas sensibles (phone, email_address) cifradas con pgcrypto en DB.
"""

from uuid import UUID as PyUUID

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TenantBaseModel


class ContactIdentifier(TenantBaseModel):
    """Identificadores de contacto multi-canal.

    Attributes:
        contact_id: FK al contacto propietario.
        channel: Canal de comunicación (whatsapp, instagram, facebook, email, etc.).
        identifier_value: Valor del identificador (teléfono, email, ID de red social).
    """

    __tablename__ = "contact_identifiers"
    __table_args__ = (
        UniqueConstraint("client_id", "channel", "identifier_value", name="uq_contact_identifier"),
    )

    contact_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(50), nullable=False)
    identifier_value: Mapped[str] = mapped_column(String(255), nullable=False)

    # Relationships
    contact: Mapped["Contact"] = relationship(
        "Contact", back_populates="identifiers"
    )
