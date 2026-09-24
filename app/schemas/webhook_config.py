"""Schemas de configuracion de webhooks salientes (Sprint 11, Dev B).

El `secret` nunca sale en una respuesta: `WebhookResponse` no lo expone (se
guarda cifrado en `tenant_webhooks.secret` via `EncryptedString`, y devolverlo
en el CRUD anularia el proposito de cifrarlo). Quien necesita el valor lo
recibe una sola vez, en la respuesta de `POST` (`WebhookCreateResponse`), igual
que un API key: no hay endpoint para volver a leerlo despues.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.events import SUPPORTED_EVENTS


class WebhookCreate(BaseModel):
    """Body para configurar un webhook saliente nuevo."""

    url: str = Field(max_length=2048, description="URL destino del POST")
    events: list[str] = Field(min_length=1, description="Eventos suscritos")
    secret: str | None = Field(
        default=None,
        min_length=16,
        max_length=256,
        description="Secreto HMAC; se genera uno si no se manda",
    )
    description: str | None = Field(default=None, max_length=500)
    headers: dict[str, str] | None = Field(default=None, description="Cabeceras extra del envio")


class WebhookUpdate(BaseModel):
    """Body para actualizar un webhook saliente. Solo se tocan los campos enviados."""

    url: str | None = Field(default=None, max_length=2048)
    events: list[str] | None = Field(default=None, min_length=1)
    secret: str | None = Field(default=None, min_length=16, max_length=256)
    description: str | None = Field(default=None, max_length=500)
    headers: dict[str, str] | None = None
    is_active: bool | None = None


class WebhookResponse(BaseModel):
    """Configuracion de un webhook saliente, sin el secreto."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    url: str
    events: list[str]
    is_active: bool
    description: str | None
    headers: dict[str, str]
    consecutive_failures: int
    last_triggered_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    disabled_reason: str | None
    created_at: datetime
    updated_at: datetime


class WebhookCreateResponse(WebhookResponse):
    """Respuesta de la creacion: la unica vez que el secreto se devuelve."""

    secret: str


class WebhookLogResponse(BaseModel):
    """Un intento de entrega registrado en `outgoing_webhook_logs`."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event: str
    status: str
    response_code: int | None
    duration_ms: int | None
    attempt: int
    error: str | None
    created_at: datetime


class WebhookTestResponse(BaseModel):
    """Resultado de un envio de prueba (`POST /{id}/test`)."""

    success: bool
    status_code: int | None = None
    duration_ms: int | None = None
    error: str | None = None


def eventos_no_soportados(events: list[str]) -> set[str]:
    """Devuelve los eventos de `events` que no estan en `SUPPORTED_EVENTS`.

    Args:
        events: Eventos que el tenant quiere suscribir.

    Returns:
        El subconjunto no reconocido; vacio si todos son validos.
    """
    return set(events) - set(SUPPORTED_EVENTS)
