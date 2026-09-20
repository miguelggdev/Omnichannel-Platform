"""WebchatProvider — el widget de chat de la pagina del cliente, por WebSocket.

A diferencia de YCloud, Meta o Telegram no hay un servidor externo al que llamar ni
que nos llame: el "proveedor" es un navegador conectado por WebSocket a la API. De
ahi tres diferencias con el resto de providers:

- **La entrada no llega por un webhook HTTP.** La recibe `app/api/v1/webchat.py`,
  que arma un payload propio (ya validado y con el visitante autenticado) y lo pasa
  por `parse_webhook()` para obtener el mismo `NormalizedMessage` que cualquier otro
  canal. Por eso `validate_signature()` devuelve **siempre** `False`: el endpoint
  generico `POST /api/v1/webhooks/webchat/...` existe (la factory lo registra) y no
  puede aceptar nada. La spec original devolvia `True` "porque el WebSocket ya esta
  autenticado", lo que habria dejado a cualquiera inyectar mensajes como cualquier
  visitante por HTTP.
- **La salida cruza procesos.** La respuesta de la IA la genera un worker de Celery y
  el WebSocket vive en un proceso de la API; no comparten memoria (la spec proponia
  un diccionario en memoria, que solo funciona con un unico proceso). `send_message()`
  publica en un canal de Redis por visitante y la conexion del visitante, suscrita a
  ese canal, lo reenvia por el WebSocket.
- **El destino puede no estar conectado.** Publicar sin suscriptores no es un fallo:
  `deliver_message()` guarda igualmente el mensaje y el visitante lo recupera al
  reconectar (`app/services/webchat_history.py`).

Credenciales: ninguna. El tenant sale de `channel_config["client_id"]`
(`DEFAULT_CLIENT_ID`, ADR-030) porque forma parte del nombre del canal de Redis.
"""

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from app.schemas.message import ChannelEnum, NormalizedMessage
from app.services.dedup import get_redis
from app.services.messaging.base import (
    ChannelConstraints,
    MessageContent,
    MessagingProvider,
    TemplateMessage,
    TemplateNotSupportedError,
)

#: Longitud maxima de un mensaje saliente (la de una respuesta larga de la IA).
MAX_TEXT_LENGTH = 10_000

#: Botones que un mensaje puede llevar.
MAX_BUTTONS = 8


def canal_de_salida(client_id: UUID | str, visitor_id: str) -> str:
    """Nombre del canal de Redis por el que se entrega a un visitante.

    Lleva el tenant: dos tenants no comparten canal aunque un `visitor_id` coincidiera.

    Args:
        client_id: Tenant del canal.
        visitor_id: Visitante destinatario.

    Returns:
        El nombre del canal.
    """
    return f"webchat:out:{client_id}:{visitor_id}"


class WebchatProvider(MessagingProvider):
    """Implementacion de `MessagingProvider` para el Webchat por WebSocket."""

    async def parse_webhook(self, raw_payload: dict[str, Any]) -> NormalizedMessage:
        """Normaliza un mensaje que el endpoint del WebSocket ya valido.

        No es un payload de un tercero: lo arma `app/api/v1/webchat.py` tras
        autenticar la sesion y validar el frame.

        Args:
            raw_payload: `{"visitor_id", "message_id", "text", "name"?, "button"?}`.

        Returns:
            Mensaje normalizado. El id externo antepone el visitante al id que
            eligio el cliente: dos visitantes que mandaran el mismo `message_id` no
            pueden hacer que la deduplicacion descarte el mensaje del otro.

        Raises:
            ValueError: Si falta el visitante o el id de mensaje.
        """
        visitor_id = raw_payload.get("visitor_id")
        message_id = raw_payload.get("message_id")
        if not visitor_id or not message_id:
            raise ValueError("Webchat: falta visitor_id o message_id")

        boton = raw_payload.get("button")
        interactive = (
            {"type": "button_reply", "id": boton["id"], "title": boton["title"]} if boton else None
        )
        return NormalizedMessage(
            channel=ChannelEnum.webchat,
            sender_identifier=str(visitor_id),
            sender_name=raw_payload.get("name"),
            text=boton["title"] if boton else raw_payload.get("text"),
            timestamp=datetime.now(timezone.utc),
            external_message_id=f"{visitor_id}:{message_id}",
            # Compacto: acaba en `messages.metadata` y en la cola de Celery.
            raw_payload={"visitor_id": visitor_id, "message_id": message_id},
            interactive_response=interactive,
        )

    async def validate_signature(self, payload: bytes, signature: str, secret: str) -> bool:
        """Rechaza siempre: el Webchat no recibe webhooks HTTP.

        Args:
            payload: Sin uso.
            signature: Sin uso.
            secret: Sin uso.

        Returns:
            `False`, siempre. La autenticacion del visitante es la sesion firmada del
            WebSocket (`webchat_session.py`), no una firma de webhook.
        """
        return False

    async def send_message(
        self, to: str, content: MessageContent, channel_config: dict[str, Any]
    ) -> str:
        """Publica el mensaje en el canal de Redis del visitante.

        Args:
            to: `visitor_id` del destinatario.
            content: Texto (y botones opcionales). El Webchat no envia media.
            channel_config: Debe traer `client_id`.

        Returns:
            El `message_id` generado, que el cliente usa para deduplicar y para
            pedir lo que se perdio al reconectar.

        Raises:
            ValueError: Si el contenido lleva media, que el Webchat no soporta.
        """
        if content.media_url:
            raise ValueError("Webchat no soporta el envio de media")

        message_id = uuid4().hex
        frame: dict[str, Any] = {
            "type": "message",
            "message_id": message_id,
            "text": content.text or "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        botones = [
            {"id": str(b["id"]), "title": str(b["title"])}
            for b in (content.buttons or [])[:MAX_BUTTONS]
        ]
        if botones:
            frame["buttons"] = botones

        # El resultado (numero de suscriptores) no importa: sin nadie conectado el
        # mensaje igualmente queda guardado y se recupera al reconectar.
        await get_redis().publish(
            canal_de_salida(channel_config["client_id"], to), json.dumps(frame)
        )
        return message_id

    async def send_template(
        self, to: str, template: TemplateMessage, channel_config: dict[str, Any]
    ) -> str:
        """El Webchat no tiene templates preaprobados.

        Args:
            to: Sin uso.
            template: Sin uso.
            channel_config: Sin uso.

        Raises:
            TemplateNotSupportedError: Siempre.
        """
        raise TemplateNotSupportedError("Webchat no soporta templates preaprobados")

    def get_channel_constraints(self) -> ChannelConstraints:
        """Restricciones del Webchat.

        Returns:
            Sin ventana de sesion y solo texto (el media exigiria una subida de
            archivos que este canal aun no tiene).
        """
        return ChannelConstraints(
            max_text_length=MAX_TEXT_LENGTH,
            supported_media_types=[],
            session_window_hours=None,
            requires_template_outside_window=False,
            max_buttons=MAX_BUTTONS,
            max_list_items=0,
        )
