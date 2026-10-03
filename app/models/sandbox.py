"""Modelos del sandbox por tenant (Sprint 14c, ADR-078).

`TenantSandbox` guarda de que tenant es el sandbox y `ConfigHistory` las
instantaneas de configuracion que permiten el rollback. Los dos viven en el
tenant **de produccion**: la fila `clients` del sandbox no es visible desde ahi.
"""

from datetime import datetime
from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel

CONFIG_TYPES: tuple[str, ...] = ("agent_configs", "quick_replies")
HISTORY_REASONS: tuple[str, ...] = ("publish", "rollback")


def _en(valores: tuple[str, ...], columna: str) -> str:
    """`columna IN ('a', 'b')`, derivado de las tuplas para que no se separen."""
    return f"{columna} IN ({', '.join(repr(v) for v in valores)})"


class TenantSandbox(TenantBaseModel):
    """De que tenant de produccion es sandbox otro tenant.

    Attributes:
        sandbox_client_id: El tenant sandbox (`clients.is_sandbox = true`).
        created_by: Usuario que lo creo.
        reset_at: Ultima vez que se rehizo desde produccion.
        last_published_at: Ultima publicacion al tenant de produccion.
    """

    __tablename__ = "tenant_sandboxes"
    __table_args__ = (
        CheckConstraint("client_id <> sandbox_client_id", name="ck_tenant_sandboxes_distinct"),
        UniqueConstraint("client_id", name="uq_tenant_sandboxes_client"),
        UniqueConstraint("sandbox_client_id", name="uq_tenant_sandboxes_sandbox"),
    )

    sandbox_client_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False
    )
    created_by: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ConfigHistory(TenantBaseModel):
    """Instantanea de la configuracion de produccion antes de publicar o volver atras.

    Attributes:
        config_type: `agent_configs` o `quick_replies`.
        version: Numero de version del tenant; las dos filas de una misma
            publicacion comparten version.
        reason: `publish` o `rollback`: por que se tomo la instantanea.
        config_data: Las filas tal como estaban, en JSON.
        created_by: Usuario que publico o revirtio.
    """

    __tablename__ = "config_history"
    __table_args__ = (
        CheckConstraint(_en(CONFIG_TYPES, "config_type"), name="ck_config_history_type"),
        CheckConstraint(_en(HISTORY_REASONS, "reason"), name="ck_config_history_reason"),
        UniqueConstraint("client_id", "config_type", "version", name="uq_config_history_version"),
    )

    config_type: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    config_data: Mapped[Any] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
