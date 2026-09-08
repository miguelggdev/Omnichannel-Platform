"""Modelo DocumentChunk — Chunks con embeddings para RAG.

Usa pgvector para la columna embedding vector(1536).
SIEMPRE filtro pre-vectorial por client_id en WHERE antes del cálculo de distancia.
"""

from uuid import UUID as _UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TenantBaseModel


class DocumentChunk(TenantBaseModel):
    """Chunk de documento con embedding para búsqueda semántica RAG.

    Attributes:
        document_id: FK al documento fuente.
        chunk_index: Índice del chunk dentro del documento.
        content: Texto del chunk.
        embedding: Vector de embedding (1536 dimensiones, text-embedding-3-small).
        token_count: Número de tokens del chunk.
        metadata_: Datos adicionales (página, sección, etc.).
    """

    __tablename__ = "document_chunks"

    document_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}")

    # Relationships
    document: Mapped["Document"] = relationship("Document", back_populates="chunks")
