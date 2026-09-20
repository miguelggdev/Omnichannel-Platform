"""WebchatProvider — widget de chat web sobre WebSocket.

El WebSocket vive en el proceso de la API (`app/api/v1/webchat.py`); la respuesta
del agente se genera y se envia desde un worker de Celery (`ai_inference`). Son
procesos distintos, asi que el provider **no puede** escribir en el socket: lo que
hace `send_message()` es publicar en Redis, y el proceso que tiene el socket abierto
esta suscrito a ese canal y reenvia el frame (ADR-056).

Desviaciones deliberadas sobre `specs/sprint-09-channels.md` §2:

- **Sin `connection_manager` en el constructor.** El spec construye
  `WebchatProvider(provider_config, connection_manager)` con un `dict` de
  conexiones en memoria. Eso solo funciona si quien envia comparte proceso con el
  socket, y no es el caso ni con un worker de Celery ni con dos replicas de la
  API detras de Traefik: el 50% de las respuestas se perderia en silencio. La
  factory real construye sin argumentos (ADR-053) y la entrega va por Redis.
- **`validate_signature()` devuelve False, no True.** El spec lo justifica con
  que "el WebSocket ya esta autenticado", pero el provider tambien queda
  registrado en la factory, y con `True` cualquiera podria inyectar mensajes por
  `POST /api/v1/webhooks/webchat/webchat` haciendose pasar por la sesion de otro
  visitante. Webchat no entra por HTTP: ese camino se cierra.
- **El `session_id` que manda el cliente se ignora.** Es el campo obligatorio del
  `WebchatMessage` del spec, pero el cliente no puede elegir de quien es el
  mensaje: la sesion es la del socket, que el servidor firmo (ADR-057).
- **El `message_id` del cliente tampoco es el id externo.** La deduplicacion es
  global por canal (`webhook_dedup`, `UNIQUE(channel, external_message_id)`): un
  visitante que mandara ids ajenos podria descartar los mensajes de otro antes de
  que llegaran. El id externo lo genera el servidor.
- **Los adjuntos entrantes se descartan.** No hay endpoint de subida para el
  widget en este sprint, asi que un `media_url` solo puede venir del propio
  visitante — una URL arbitraria que despues alguien descarga (la transcripcion
  de audio lo haria) es un SSRF servido en bandeja. Queda pendiente junto con el
  endpoint de subida.

El canal no tiene ventana de sesion: el agente puede escribir cuando quiera, y si
el visitante no esta conectado el mensaje queda persistido igual y se le entrega
al reconectar (`last_message_id`).
"""

import base64
import hmac
import json
import logging
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from app.core.config import get_settings
from app.schemas.message import ChannelEnum, MessageTypeEnum, NormalizedMessage
from app.services.messaging.base import (
    ChannelConstraints,
    IgnoredWebhookError,
    MessageContent,
    MessagingProvider,
    TemplateMessage,
    TemplateNotSupportedError,
)

logger = logging.getLogger(__name__)

#: Longitud maxima que se le anuncia al grafo. No hay limite del proveedor: un
#: navegador renderiza cualquier texto, asi que aqui no se trocea nada.
MAX_TEXT_LENGTH = 10_000

#: Prefijo de los canales de Redis Pub/Sub por los que viaja lo saliente.
PUBSUB_PREFIX = "webchat:out"

#: Prefijo de los ids externos que genera el servidor para este canal.
EXTERNAL_ID_PREFIX = "wc-"

#: Separador entre el id de sesion y su firma dentro del token.
_SEPARADOR_TOKEN = "."  # noqa: S105 — un separador, no una credencial

#: Etiqueta de dominio de la firma: el mismo secreto (`JWT_SECRET`) firma tokens
#: de acceso; sin esta etiqueta las dos firmas serian intercambiables.
_DOMINIO_FIRMA = b"webchat-session:"

#: Tipos de frame que el widget puede mandar y que no son un mensaje.
_FRAMES_SIN_MENSAJE = ("typing", "read_receipt", "ping")


def _clave_de_firma() -> bytes:
    """Devuelve el secreto con el que se firman los ids de sesion.

    Returns:
        `JWT_SECRET` en bytes. Es el unico secreto de servidor garantizado (tiene
        validador de longitud minima y la app no arranca sin el).
    """
    return get_settings().JWT_SECRET.encode("utf-8")


def firmar_sesion(session_id: str) -> str:
    """Firma un id de sesion y devuelve el token que se le entrega al widget.

    Args:
        session_id: Id de sesion en claro (un UUID4 en hexadecimal).

    Returns:
        Token `"<session_id>.<firma>"`, apto para una query string.
    """
    firma = hmac.new(
        _clave_de_firma(), _DOMINIO_FIRMA + session_id.encode("utf-8"), sha256
    ).digest()
    return session_id + _SEPARADOR_TOKEN + base64.urlsafe_b64encode(firma).decode().rstrip("=")


def sesion_de_token(token: str | None) -> str | None:
    """Verifica el token de sesion del widget y extrae el id de sesion.

    Sin esta verificacion, reconectar con el `session_id` de otro visitante
    bastaria para leer su conversacion (la reposicion de mensajes perdidos) y
    para escribir en su nombre.

    Args:
        token: Valor recibido del cliente, o `None`.

    Returns:
        El id de sesion si la firma es valida; `None` en cualquier otro caso.
    """
    if not token or _SEPARADOR_TOKEN not in token:
        return None
    session_id, _, _firma = token.partition(_SEPARADOR_TOKEN)
    if not session_id:
        return None
    # compare_digest sobre el token completo: comparar la firma con `==` filtra
    # por tiempo cuantos bytes iniciales acerto quien la esta adivinando.
    if not hmac.compare_digest(token, firmar_sesion(session_id)):
        return None
    return session_id


def nueva_sesion() -> tuple[str, str]:
    """Crea un id de sesion nuevo y su token firmado.

    Returns:
        Tupla `(session_id, token)`.
    """
    session_id = uuid4().hex
    return session_id, firmar_sesion(session_id)


def canal_de_sesion(client_id: str | UUID, session_id: str) -> str:
    """Nombre del canal de Redis por el que viaja lo saliente de una sesion.

    Args:
        client_id: Tenant dueno de la conversacion.
        session_id: Id de sesion en claro.

    Returns:
        `webchat:out:{client_id}:{session_id}`.
    """
    return f"{PUBSUB_PREFIX}:{client_id}:{session_id}"


def nuevo_id_externo() -> str:
    """Genera el `external_message_id` de un mensaje de webchat.

    Returns:
        Id con el prefijo `wc-`, unico por construccion.
    """
    return EXTERNAL_ID_PREFIX + uuid4().hex


class WebchatProvider(MessagingProvider):
    """Proveedor del canal webchat: normaliza lo entrante y publica lo saliente."""

    async def parse_webhook(self, raw_payload: dict[str, Any]) -> NormalizedMessage:
        """Normaliza un frame del widget que ya paso por el socket.

        El endpoint WebSocket es quien arma `raw_payload`: agrega el `session_id`
        de la conexion (no el que mando el cliente) y el `external_message_id`
        que genero el servidor.

        Args:
            raw_payload: Frame del widget mas los campos que agrega el endpoint.

        Returns:
            Mensaje normalizado del canal `webchat`.

        Raises:
            IgnoredWebhookError: Si el frame no es un mensaje (`typing`,
                `read_receipt`, `ping`) o si no trae texto.
            ValueError: Si falta el `session_id` de la conexion.
        """
        tipo = str(raw_payload.get("type") or "message")
        if tipo in _FRAMES_SIN_MENSAJE:
            raise IgnoredWebhookError(f"Frame de webchat sin mensaje: {tipo}")

        session_id = str(raw_payload.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("Frame de webchat sin session_id de la conexion")

        texto = (raw_payload.get("text") or "").strip()
        if not texto:
            raise IgnoredWebhookError("Frame de webchat sin texto")

        if raw_payload.get("media_url"):
            # Ver el docstring del modulo: sin endpoint de subida, un media_url
            # del visitante es una URL arbitraria que despues se descargaria.
            logger.info("Adjunto de webchat descartado (sin endpoint de subida)")

        return NormalizedMessage(
            channel=ChannelEnum.webchat,
            sender_identifier=session_id,
            text=texto,
            media_url=None,
            media_type=MessageTypeEnum.text,
            timestamp=datetime.now(timezone.utc),
            external_message_id=str(raw_payload.get("external_message_id") or nuevo_id_externo()),
            raw_payload=raw_payload,
        )

    async def validate_signature(self, payload: bytes, signature: str, secret: str) -> bool:
        """Rechaza siempre: webchat no entra por el endpoint HTTP de webhooks.

        Args:
            payload: Sin uso.
            signature: Sin uso.
            secret: Sin uso.

        Returns:
            False, siempre. Ver el docstring del modulo.
        """
        return False

    async def send_message(
        self, to: str, content: MessageContent, channel_config: dict[str, Any]
    ) -> str:
        """Publica el mensaje en el canal Redis de la sesion.

        Que no haya nadie suscrito no es un error: el visitante cerro la pestana.
        El mensaje ya quedo persistido por `deliver_message()` y se le entrega al
        reconectar con `last_message_id`.

        Args:
            to: Id de sesion en claro del destinatario.
            content: Contenido a enviar.
            channel_config: Debe traer `client_id`.

        Returns:
            El `external_message_id` generado para este mensaje.
        """
        from app.services.dedup import get_redis

        external_id = nuevo_id_externo()
        frame: dict[str, Any] = {
            "type": "message",
            "message_id": external_id,
            "text": content.text or "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if content.media_url:
            frame["media_url"] = content.media_url
            frame["media_type"] = content.media_type or "image"
        if content.caption:
            frame["caption"] = content.caption
        if content.buttons:
            frame["buttons"] = content.buttons

        canal = canal_de_sesion(channel_config["client_id"], to)
        suscriptores = await get_redis().publish(canal, json.dumps(frame))
        if not suscriptores:
            logger.info(
                "Webchat sin suscriptores para la sesion; el mensaje se entrega al reconectar"
            )
        return external_id

    async def send_template(
        self, to: str, template: TemplateMessage, channel_config: dict[str, Any]
    ) -> str:
        """Webchat no tiene templates preaprobados.

        Args:
            to: Sin uso.
            template: Sin uso.
            channel_config: Sin uso.

        Raises:
            TemplateNotSupportedError: Siempre.
        """
        raise TemplateNotSupportedError("Webchat no soporta templates preaprobados")

    def get_channel_constraints(self) -> ChannelConstraints:
        """Restricciones del canal webchat.

        Returns:
            Sin ventana de sesion y sin templates: el agente puede escribir en
            cualquier momento.
        """
        return ChannelConstraints(
            max_text_length=MAX_TEXT_LENGTH,
            supported_media_types=["image", "document"],
            session_window_hours=None,
            requires_template_outside_window=False,
            max_buttons=8,
            max_list_items=0,
        )
