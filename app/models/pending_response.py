"""Modelo PendingResponse — Respuestas pendientes de aprobación (training mode, ADR-005)."""

from uuid import UUID as _UUID

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel


class PendingResponse(TenantBaseModel):
    """Respuesta generada por el agente pendiente de aprobación humana.

    Attributes:
        conversation_id: FK a la conversación origen.
        question: Pregunta del contacto.
        generated_response: Respuesta generada por el LLM.
        status: Estado de aprobación (pending, approved, rejected).
        reviewed_by: UUID del usuario que revisó.
    """

    __tablename__ = "pending_responses"

    conversation_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    generated_response: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    reviewed_by: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
