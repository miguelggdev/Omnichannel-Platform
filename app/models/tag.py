"""Modelo Tag — Etiquetas por tenant."""

from uuid import UUID as PyUUID

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TenantBaseModel


class Tag(TenantBaseModel):
    """Etiquetas configurables por tenant para clasificar contactos.

    Attributes:
        name: Nombre de la etiqueta (único por tenant).
        color: Color hex para UI (e.g., #FF5733).
    """

    __tablename__ = "tags"
    __table_args__ = (
        UniqueConstraint("client_id", "name", name="uq_tag_name_per_client"),
    )

    client_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    color: Mapped[str | None] = mapped_column(String(7), nullable=True)

    # Relationships
    contact_tags: Mapped[list["ContactTag"]] = relationship(
        "ContactTag", back_populates="tag"
    )
