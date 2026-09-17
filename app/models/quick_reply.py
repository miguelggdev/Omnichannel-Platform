"""Modelo QuickReply — Respuestas rápidas predefinidas por tenant."""

from uuid import UUID as _UUID

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel


class QuickReply(TenantBaseModel):
    """Respuesta rápida predefinida que los agentes pueden usar.

    Attributes:
        shortcut: Atajo que el agente teclea para insertarla (e.g. "/saludo").
            Único por tenant.
        title: Título corto para mostrar en UI.
        content: Contenido completo. Admite variables `{{contact_name}}`,
            `{{agent_name}}`, `{{ticket_id}}` y `{{date}}`, que resuelve
            `app/services/quick_reply.py` al usarla.
        category: Categoría para organizar (e.g., saludo, despedida, faq).
        created_by: Usuario que la creó; NULL si el usuario fue dado de baja.
    """

    __tablename__ = "quick_replies"
    __table_args__ = (UniqueConstraint("client_id", "shortcut", name="uq_quick_reply_shortcut"),)

    shortcut: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_by: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
