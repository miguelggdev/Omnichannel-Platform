"""Modelo OutgoingWebhookLog — intentos de envio de webhooks salientes (Sprint 11).

Una fila por intento, no por evento: los reintentos de un mismo evento
comparten `payload["webhook_delivery_id"]` y se distinguen por `attempt`.

La FK a `tenant_webhooks` si lleva `ON DELETE CASCADE`, a diferencia de la
convencion del resto del repo (`004_service_types` y siguientes): el log no
tiene sentido sin su webhook, y sin cascada el DELETE del endpoint de Dev B
fallaria con una violacion de FK en cuanto el webhook tuviera un solo envio.
"""

from typing import Any
from uuid import UUID as _UUID

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel


class OutgoingWebhookLog(TenantBaseModel):
    """Registro de un intento de envio a un webhook saliente.

    Una fila por intento: los reintentos de un mismo evento comparten
    `payload["webhook_delivery_id"]` y se distinguen por `attempt`.

    Attributes:
        webhook_id: Webhook al que se intento enviar.
        event: Nombre del evento (`message.received`, ...).
        payload: Cuerpo exacto que se firmo y se envio.
        status: `success` o `failed`.
        response_code: Codigo HTTP de la respuesta, si hubo respuesta.
        response_body: Primeros 1000 caracteres del cuerpo de la respuesta.
        duration_ms: Cuanto tardo el POST.
        attempt: Numero de intento (1 es el primero).
        error: Motivo del fallo, recortado; NULL si salio bien.
    """

    __tablename__ = "outgoing_webhook_logs"
    __table_args__ = (
        Index("idx_webhook_logs_webhook_status", "webhook_id", "status"),
        Index("idx_webhook_logs_created_at", "created_at"),
    )

    webhook_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenant_webhooks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
