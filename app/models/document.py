"""Modelo Document — Documentos de base de conocimiento por tenant."""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TenantBaseModel


class Document(TenantBaseModel):
    """Documento subido al knowledge base del tenant para RAG.

    Attributes:
        title: Título del documento.
        file_url: URL del archivo en Supabase Storage.
        file_type: Tipo MIME del archivo.
        file_size: Tamaño en bytes.
        content: Contenido extraído (texto plano).
        chunk_count: Número de chunks generados.
        status: Estado de procesamiento (pending, processing, completed, failed).
        metadata_: Datos adicionales JSONB.
    """

    __tablename__ = "documents"

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    file_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    file_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, server_default="0")
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    chunks: Mapped[list["DocumentChunk"]] = relationship("DocumentChunk", back_populates="document")
