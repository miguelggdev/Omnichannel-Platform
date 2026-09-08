"""Modelo WebhookDedup — Deduplicación de webhooks entrantes (PAT-001).

TTL: entradas se limpian después de 72 horas.
"""

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantBaseModel


class WebhookDedup(TenantBaseModel):
    """Registro de deduplicación para webhooks entrantes.

    Attributes:
        channel: Canal de origen (whatsapp, instagram, facebook, etc.).
        external_message_id: ID del mensaje en el proveedor externo.
    """

    __tablename__ = "webhook_dedup"
    __table_args__ = (UniqueConstraint("channel", "external_message_id", name="uq_webhook_dedup"),)

    channel: Mapped[str] = mapped_column(String(50), nullable=False)
    external_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
