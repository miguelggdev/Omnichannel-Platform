"""Modelo ApprovedResponse — Respuestas aprobadas con embeddings (few-shot, ADR-005).

Usa pgvector para la columna embedding vector(1536).
Threshold de similaridad: 0.80 (más estricto que RAG general).
"""

from uuid import UUID as PyUUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel


class ApprovedResponse(TenantBaseModel):
    """Par (pregunta, respuesta) aprobado para few-shot learning.

    Attributes:
        conversation_id: FK a la conversación origen.
        question: Pregunta original del contacto.
        response: Respuesta aprobada por el humano.
        embedding: Vector de embedding de la pregunta (1536 dims).
    """

    __tablename__ = "approved_responses"

    conversation_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
