"""Modelo TokenUsageLog — Log granular de uso de tokens."""

from uuid import UUID as PyUUID

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel


class TokenUsageLog(TenantBaseModel):
    """Registro individual de consumo de tokens.

    Attributes:
        conversation_id: FK a la conversación que generó el consumo.
        model: Modelo LLM utilizado.
        prompt_tokens: Tokens del prompt.
        completion_tokens: Tokens de la respuesta.
        total_tokens: Total de tokens consumidos.
        operation: Tipo de operación (chat, embedding, rag, etc.).
    """

    __tablename__ = "token_usage_logs"

    conversation_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=True
    )
    model: Mapped[str] = mapped_column(String(50), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(String(50), nullable=False)
