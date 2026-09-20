"""Schemas del canal webchat — frames que viajan por el WebSocket.

Diferencias con `specs/sprint-09-channels.md` §2.4, todas por seguridad (ver el
docstring de `app/services/messaging/webchat.py`):

- `WebchatMessage` **no** lleva `session_id`: la sesion es la del socket, que el
  servidor firmo. Si el cliente lo manda, `extra="ignore"` lo descarta.
- `message_id` se renombra a `client_message_id` y solo sirve para que el widget
  correlacione su eco: el `external_message_id` real lo genera el servidor.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Frames que el widget puede mandar.
TipoEntrante = Literal["message", "typing", "read_receipt", "ping"]

#: Frames que el servidor puede mandar.
TipoSaliente = Literal["connected", "message", "ack", "error"]


class WebchatMessage(BaseModel):
    """Frame entrante del widget.

    Attributes:
        type: Tipo de frame. Solo `message` genera una conversacion.
        text: Cuerpo del mensaje.
        client_message_id: Id que el widget usa para correlacionar su propio eco.
    """

    model_config = ConfigDict(extra="ignore")

    type: TipoEntrante = "message"
    text: str | None = Field(default=None, max_length=10_000)
    client_message_id: str | None = Field(default=None, max_length=100)


class WebchatResponse(BaseModel):
    """Frame saliente del servidor.

    Attributes:
        type: Tipo de frame.
        timestamp: Momento de emision, ISO-8601 con timezone.
        session: Token de sesion firmado (solo en `connected`).
        message_id: `external_message_id` del mensaje (en `message` y `ack`).
        client_message_id: Eco del id del widget (solo en `ack`).
        text: Cuerpo del mensaje (solo en `message`).
        media_url: Adjunto del mensaje, si lo hay.
        media_type: Tipo del adjunto.
        caption: Pie del adjunto.
        buttons: Botones de respuesta rapida.
        error_code: Codigo de error (solo en `error`).
        message: Descripcion del error (solo en `error`).
    """

    model_config = ConfigDict(extra="forbid")

    type: TipoSaliente
    timestamp: str
    session: str | None = None
    message_id: str | None = None
    client_message_id: str | None = None
    text: str | None = None
    media_url: str | None = None
    media_type: str | None = None
    caption: str | None = None
    buttons: list[dict[str, Any]] | None = None
    error_code: str | None = None
    message: str | None = None
