"""Modelo QuickReply — Respuestas rápidas predefinidas por tenant."""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel


class QuickReply(TenantBaseModel):
    """Respuesta rápida predefinida que los agentes pueden usar.

    Attributes:
        title: Título corto para mostrar en UI.
        content: Contenido completo de la respuesta.
        category: Categoría para organizar (e.g., saludo, despedida, faq).
    """

    __tablename__ = "quick_replies"

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
