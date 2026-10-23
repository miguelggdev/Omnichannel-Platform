"""Schemas de Message — CRUD de mensajes y NormalizedMessage."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Esquemas que puede tener un `media_url`: URLs web y la referencia interna de Telegram
#: (`telegram-file:<file_id>`, que se resuelve en el worker, no se abre en el navegador).
ESQUEMAS_MEDIA_URL = ("http", "https", "telegram-file")
#: Largo de la columna `messages.media_url`.
MAX_CHARS_MEDIA_URL = 1000


def sanear_media_url(valor: str | None) -> str | None:
    """Deja pasar un `media_url` solo si su esquema es conocido.

    Un adjunto viene de un canal externo: un `javascript:` o `data:` guardado aqui se
    ejecutaria al abrirse en el navegador de un agente. Un valor invalido no rechaza el
    mensaje (el texto sigue siendo valido): solo se descarta el adjunto.

    Args:
        valor: `media_url` tal como llego del canal o del cliente.

    Returns:
        El valor recortado si es valido; `None` si falta, es demasiado largo, tiene
        caracteres de control o un esquema fuera de `ESQUEMAS_MEDIA_URL`.
    """
    if valor is None:
        return None
    limpio = valor.strip()
    if not limpio or len(limpio) > MAX_CHARS_MEDIA_URL:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in limpio):
        return None
    esquema, separador, _ = limpio.partition(":")
    if not separador or esquema.lower() not in ESQUEMAS_MEDIA_URL:
        return None
    return limpio


class MessageCreate(BaseModel):
    """Schema para crear un mensaje."""

    conversation_id: UUID
    direction: str = Field(pattern=r"^(inbound|outbound)$")
    message_type: str = "text"
    content: str | None = None
    media_url: str | None = None
    sender_type: str = Field(max_length=20)
    sender_id: UUID | None = None
    metadata: dict[str, Any] = {}

    @field_validator("media_url")
    @classmethod
    def media_url_segura(cls, v: str | None) -> str | None:
        """Descarta un adjunto con esquema no permitido (ver `sanear_media_url`)."""
        return sanear_media_url(v)


class MessageUpdate(BaseModel):
    """Schema para actualizar un mensaje."""

    content: str | None = None
    metadata: dict[str, Any] | None = None


class MessageResponse(BaseModel):
    """Schema de respuesta de mensaje."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    client_id: UUID
    conversation_id: UUID
    direction: str
    message_type: str
    content: str | None
    media_url: str | None
    external_message_id: str | None
    sender_type: str
    sender_id: UUID | None
    created_at: datetime
    updated_at: datetime


class MessageListResponse(BaseModel):
    """Schema de lista de mensajes."""

    items: list[MessageResponse]
    total: int
    page: int
    page_size: int


class ChannelEnum(StrEnum):
    """Canal de origen/destino de un mensaje, independiente del proveedor."""

    whatsapp = "whatsapp"
    telegram = "telegram"
    instagram = "instagram"
    webchat = "webchat"
    email = "email"
    phone = "phone"
    facebook = "facebook"
    #: Llamadas telefonicas (Twilio, Sprint 13). `phone` es anterior y no lo usa
    #: ningun proveedor.
    voice = "voice"


class MessageTypeEnum(StrEnum):
    """Tipo de contenido de un mensaje normalizado."""

    text = "text"
    image = "image"
    audio = "audio"
    video = "video"
    document = "document"
    location = "location"
    template = "template"
    interactive = "interactive"


class NormalizedMessage(BaseModel):
    """Mensaje normalizado, independiente del proveedor (PAT-002).

    Todo webhook entrante se convierte a este formato antes de encolarse: el
    worker de Celery y, mas adelante, el grafo de agentes solo conocen este
    contrato, nunca el payload crudo de YCloud o Meta.

    Attributes:
        channel: Canal de origen del mensaje.
        sender_identifier: Telefono, PSID o username del remitente segun el canal.
        text: Cuerpo de texto del mensaje, si lo tiene.
        media_url: URL del recurso multimedia adjunto, si lo hay.
        media_type: Tipo del recurso multimedia adjunto.
        timestamp: Momento en que el proveedor registro el mensaje, con timezone.
        external_message_id: ID unico del mensaje en el proveedor de origen —
            clave de deduplicacion.
        raw_payload: Payload original del webhook, para debugging y auditoria.
        location: Coordenadas `{"latitude": ..., "longitude": ...}` si el
            mensaje es de ubicacion.
        interactive_response: Payload de la respuesta a un boton/quick reply.
        sender_name: Nombre que el remitente publica en su perfil, si el canal lo
            trae (Telegram, cabecera `From` de un email). Solo sirve para que el
            agente reconozca al contacto en la bandeja; nunca identifica: para eso
            esta `sender_identifier`.
        verified_phone: Telefono que el canal PRUEBA que es de quien escribe,
            distinto del identificador del canal. Solo lo rellena un provider
            cuando el propio canal lo garantiza (Telegram, si el usuario comparte
            su propio contacto); nunca desde un dato que el remitente pueda
            falsear. Habilita la unificacion automatica de contactos
            (`app/services/phone_unification.py`).
    """

    channel: ChannelEnum
    sender_identifier: str
    text: str | None = None
    media_url: str | None = None
    media_type: MessageTypeEnum | None = None
    timestamp: datetime
    external_message_id: str
    raw_payload: dict[str, Any]

    location: dict[str, Any] | None = None
    interactive_response: dict[str, Any] | None = None
    sender_name: str | None = None
    verified_phone: str | None = None

    @field_validator("media_url")
    @classmethod
    def media_url_segura(cls, v: str | None) -> str | None:
        """Descarta un adjunto con esquema no permitido (ver `sanear_media_url`)."""
        return sanear_media_url(v)

    @field_validator("sender_identifier")
    @classmethod
    def sender_identifier_no_vacio(cls, v: str) -> str:
        """Rechaza un identificador vacio o solo espacios.

        `_resolve_contact()` (`app/tasks/webhook_processor.py`) busca el
        contacto por el hash ciego de este valor (`blind_index()`), que es
        determinista: un payload mal formado con el remitente ausente
        (`.get("from", "")` en los providers) produciria siempre el mismo
        hash para el mismo tenant y canal, y el primer mensaje asi creado
        se convertiria en un iman que atrae a cualquier otro remitente real
        que tambien llegue sin identificador — mezclando las conversaciones
        de clientes finales distintos bajo un solo contacto. Rechazarlo aqui,
        antes de que exista una fila, es mas simple que defenderse despues.

        Args:
            v: Valor del campo tal como llego del payload del proveedor.

        Returns:
            El valor sin espacios al borde.

        Raises:
            ValueError: Si queda vacio despues de recortar los espacios.
        """
        recortado = v.strip()
        if not recortado:
            raise ValueError("sender_identifier no puede estar vacio")
        return recortado
