"""Modelo AuditLog — rastro de cambios sobre las tablas sensibles.

Las filas no las escribe Python: las inserta un trigger de PostgreSQL
(`audit_trigger_function()`, migracion 006). Asi queda auditado cualquier cambio,
venga de la API, de un worker de Celery o de un `psql` a mano — que es
justamente lo que un rastro de auditoria tiene que garantizar.

Este modelo existe para leer esas filas (y para que `alembic check` vea la tabla
en la metadata); la aplicacion nunca hace `session.add(AuditLog(...))`.
"""

from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import Enum, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel


class AuditLog(TenantBaseModel):
    """Cambio registrado sobre una fila de una tabla auditada.

    Attributes:
        table_name: Tabla donde ocurrio el cambio.
        record_id: Clave primaria de la fila afectada.
        action: INSERT, UPDATE o DELETE.
        old_values: Fila completa antes del cambio (NULL en INSERT).
        new_values: Fila completa despues del cambio (NULL en DELETE).
        user_id: Usuario que lo provoco; NULL si fue el sistema (un worker de
            Celery no actua en nombre de nadie).
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        # "que paso en este tenant ultimamente": WHERE y ORDER BY en un indice.
        Index("ix_audit_logs_client_created", "client_id", "created_at"),
    )

    # Se redeclara `client_id` (TenantBaseModel lo trae sin `ondelete`) para
    # dejar la intencion junto al schema: borrar un tenant se lleva su rastro.
    client_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    table_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    record_id: Mapped[_UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    action: Mapped[str] = mapped_column(
        Enum("INSERT", "UPDATE", "DELETE", name="audit_action", create_type=False),
        nullable=False,
    )
    old_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    new_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    user_id: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
