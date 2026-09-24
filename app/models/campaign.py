"""Modelo Campaign — campanas de mensajeria masiva por tenant (Sprint 12).

`segment_criteria` guarda los criterios, no la lista de contactos: el segmento
se resuelve al enviar, no al crear la campana. Si se congelara la lista, una
campana programada para manana saldria con el segmento de hoy.

`error_log` guarda como mucho `MAX_ERRORES_GUARDADOS` entradas: una campana a
50.000 contactos con el canal mal configurado falla 50.000 veces, y meter eso
en una fila JSONB es una forma barata de tumbar la base.
"""

from datetime import datetime
from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel

# Estados por los que pasa una campana.
CAMPAIGN_DRAFT = "draft"
CAMPAIGN_SCHEDULED = "scheduled"
CAMPAIGN_SENDING = "sending"
CAMPAIGN_COMPLETED = "completed"
CAMPAIGN_FAILED = "failed"

CAMPAIGN_STATUSES: tuple[str, ...] = (
    CAMPAIGN_DRAFT,
    CAMPAIGN_SCHEDULED,
    CAMPAIGN_SENDING,
    CAMPAIGN_COMPLETED,
    CAMPAIGN_FAILED,
)

# Estados desde los que una campana todavia puede lanzarse.
CAMPAIGN_LANZABLE: tuple[str, ...] = (CAMPAIGN_DRAFT, CAMPAIGN_SCHEDULED)

MAX_ERRORES_GUARDADOS = 100


class Campaign(TenantBaseModel):
    """Campana de mensajeria masiva.

    Attributes:
        name: Nombre descriptivo.
        channel: Canal de envio (whatsapp, telegram, email, ...).
        segment_criteria: Criterios de segmentacion, resueltos al enviar.
        message_template: Texto con variables `{{contact_name}}`, etc.
        status: Uno de `CAMPAIGN_STATUSES`.
        target_count: Contactos del segmento al empezar el envio.
        delivered_count: Mensajes aceptados por el proveedor.
        read_count: Leidos, si el canal lo reporta.
        replied_count: Contactos que respondieron.
        failed_count: Envios fallidos.
        scheduled_for: Cuando debe salir; NULL es envio inmediato.
        started_at: Cuando empezo el envio.
        completed_at: Cuando termino (bien o mal).
        created_by: Usuario que la creo; NULL si la creo un agente.
        error_log: Hasta `MAX_ERRORES_GUARDADOS` errores del envio.
        updated_at: Ultima modificacion.
    """

    __tablename__ = "campaigns"
    __table_args__ = (Index("idx_campaigns_client_status", "client_id", "status"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    channel: Mapped[str] = mapped_column(String(50), nullable=False)
    segment_criteria: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    message_template: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), server_default=CAMPAIGN_DRAFT, nullable=False)
    target_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    delivered_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    read_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    replied_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[_UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    error_log: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
