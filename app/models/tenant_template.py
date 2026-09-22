"""Modelos TenantTemplate y TemplateInstantiation — clonacion de tenants (Sprint 10).

Ninguno de los dos hereda de TenantBaseModel: son datos de plataforma, no de
un tenant (ver migracion 010 y ADR-064 en MEMORY.md, misma excepcion que
`Client`).
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class InstantiationStatus(StrEnum):
    """Estado de una instanciacion de template en curso."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class TenantTemplate(Base):
    """Snapshot reutilizable de la configuracion de un tenant.

    Attributes:
        id: UUID del template.
        name: Nombre para mostrar en el listado.
        description: Descripcion libre, opcional.
        source_client_id: Tenant del que se tomo el snapshot. Solo referencia
            (no aisla por RLS): el tenant origen puede darse de baja despues
            sin invalidar el template.
        config: Snapshot completo (agent_configs, quick_replies, tags,
            documents, token_budget, settings), armado por
            `TenantCloner.create_snapshot()`. Nunca incluye secretos.
        created_by: Usuario (`super_admin`) que creo el template.
        is_public: Si otros `super_admin` pueden verlo y usarlo, no solo el
            que lo creo.
        version: Version del formato del snapshot, para poder migrarlo si
            cambia de forma en el futuro.
        created_at: Fecha de creacion.
        updated_at: Ultima modificacion.
    """

    __tablename__ = "tenant_templates"

    id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_client_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False, index=True
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    is_public: Mapped[bool] = mapped_column(Boolean, server_default="false")
    version: Mapped[str] = mapped_column(String(20), server_default="1.0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TemplateInstantiation(Base):
    """Seguimiento de una clonacion de tenant en curso.

    Attributes:
        id: UUID de la instanciacion.
        template_id: Template del que se clona.
        target_client_id: Tenant nuevo creado; NULL hasta que la task lo crea.
        status: `InstantiationStatus`.
        error_message: Motivo del fallo, si `status == "failed"`. Nunca un
            traceback (CLAUDE.md, regla 3).
        progress: Avance legible por humanos (paso actual, documentos
            procesados) y, mientras no exista un flujo de invitacion, el
            password temporal del admin del tenant nuevo. Solo lo lee
            `super_admin`.
        started_at: Cuando la task empezo a procesar.
        completed_at: Cuando termino (bien o mal).
        created_at: Cuando se encolo.
    """

    __tablename__ = "template_instantiations"

    id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    template_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant_templates.id"), nullable=False, index=True
    )
    target_client_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20), server_default=InstantiationStatus.PENDING.value
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
