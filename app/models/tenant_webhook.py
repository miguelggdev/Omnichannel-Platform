"""Modelo TenantWebhook — webhooks salientes configurados por tenant (Sprint 11).

El `secret` con el que se firma cada envio (HMAC-SHA256) es una credencial, no
un dato de negocio: se guarda cifrado con `EncryptedString` (pgcrypto, misma
mecanica que `contact_identifiers.identifier_value` desde Sprint 8), no como el
`VARCHAR(256)` en claro del spec. El cifrado lo hace PostgreSQL, asi que el
valor no aparece en ningun log de la aplicacion.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedString
from app.models.base import TenantBaseModel

# Fallos consecutivos tras los que el webhook se desactiva solo.
MAX_CONSECUTIVE_FAILURES = 10

# Motivo que queda en `disabled_reason` cuando lo desactiva el propio engine.
AUTO_DISABLED_REASON = "auto_disabled_failures"


class TenantWebhook(TenantBaseModel):
    """Endpoint externo al que un tenant quiere que le lleguen eventos.

    Attributes:
        url: URL destino del POST.
        secret: Secreto compartido con el que se firma el cuerpo (HMAC-SHA256).
            Cifrado en la base.
        events: Eventos suscritos (ver `SUPPORTED_EVENTS` en `app/core/events.py`).
        is_active: Si recibe envios. Lo apaga el admin o el propio engine tras
            `MAX_CONSECUTIVE_FAILURES` fallos seguidos.
        description: Descripcion libre para el panel.
        headers: Cabeceras extra que el tenant quiere que se manden.
        consecutive_failures: Fallos seguidos; vuelve a 0 con cada exito.
        last_triggered_at: Ultimo intento, haya salido bien o mal.
        last_success_at: Ultimo envio con respuesta 2xx.
        last_failure_at: Ultimo intento fallido.
        disabled_reason: Por que esta apagado (`AUTO_DISABLED_REASON` si fue
            el engine); NULL si lo apago una persona o si esta activo.
        updated_at: Ultima modificacion.
    """

    __tablename__ = "tenant_webhooks"
    __table_args__ = (
        # Las consultas del dispatcher son siempre "los activos de este tenant
        # suscritos a este evento": el indice parcial deja fuera los apagados,
        # que en una tabla de configuracion son la mayoria con el tiempo.
        Index(
            "idx_tenant_webhooks_active_events",
            "client_id",
            "is_active",
            postgresql_where=text("is_active"),
        ),
    )

    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    secret: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    events: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    last_triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
