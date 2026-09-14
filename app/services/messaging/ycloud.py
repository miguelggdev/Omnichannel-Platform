"""YCloudProvider — WhatsApp Business API via YCloud.

Documentacion: https://docs.ycloud.com/

Contrato: `specs/sprint-04-webhooks.md` §3.
"""

import hashlib
import hmac
from datetime import datetime
from typing import Any

import httpx

from app.core.config import get_settings
from app.schemas.message import ChannelEnum, MessageTypeEnum, NormalizedMessage
from app.services.messaging.base import (
    ChannelConstraints,
    MessageContent,
    MessagingProvider,
    TemplateMessage,
)

# Tipos de media que YCloud reporta en el webhook con su propia clave anidada.
_MEDIA_TYPES = ("image", "audio", "video", "document")


class YCloudProvider(MessagingProvider):
    """Implementacion de `MessagingProvider` para YCloud (WhatsApp).

    No requiere config en el constructor: la firma se valida con el secreto que
    pasa el endpoint (`settings.YCLOUD_WEBHOOK_SECRET`) y el envio usa la API key
    del `channel_config` que reciba en cada llamada.
    """

    async def parse_webhook(self, raw_payload: dict[str, Any]) -> NormalizedMessage:
        """Normaliza el payload de un webhook entrante de YCloud.

        Estructura esperada (`whatsappInboundMessage`): ver
        `tests/fixtures/meta_payloads.py::YCLOUD_TEXT` / `YCLOUD_IMAGE`.

        Args:
            raw_payload: Payload JSON del webhook, ya deserializado.

        Returns:
            Mensaje normalizado con `channel=whatsapp`.
        """
        msg = raw_payload.get("whatsappInboundMessage", {})
        msg_type = msg.get("type", "text")

        text: str | None = None
        media_url: str | None = None
        media_type: MessageTypeEnum | None = None
        location: dict[str, Any] | None = None

        if msg_type == "text":
            text = msg.get("text", {}).get("body")
        elif msg_type in _MEDIA_TYPES:
            media_data = msg.get(msg_type, {})
            media_url = media_data.get("url") or media_data.get("link")
            media_type = MessageTypeEnum(msg_type)
            text = media_data.get("caption")
        elif msg_type == "location":
            loc = msg.get("location", {})
            if loc:
                location = {
                    "latitude": loc.get("latitude"),
                    "longitude": loc.get("longitude"),
                }

        return NormalizedMessage(
            channel=ChannelEnum.whatsapp,
            sender_identifier=msg.get("from", ""),
            text=text,
            media_url=media_url,
            media_type=media_type,
            timestamp=datetime.fromisoformat(msg.get("timestamp", "")),
            external_message_id=msg.get("id", raw_payload.get("id", "")),
            raw_payload=raw_payload,
            location=location,
        )

    async def validate_signature(self, payload: bytes, signature: str, secret: str) -> bool:
        """Valida el HMAC-SHA256 del header `X-Ycloud-Signature`.

        YCloud manda el digest hex pelado, sin prefijo (a diferencia de Meta,
        que antepone `sha256=`).

        Args:
            payload: Cuerpo crudo del request.
            signature: Valor del header `X-Ycloud-Signature`.
            secret: `YCLOUD_WEBHOOK_SECRET` configurado.

        Returns:
            True si la firma coincide.
        """
        if not signature:
            return False
        expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def send_message(
        self, to: str, content: MessageContent, channel_config: dict[str, Any]
    ) -> str:
        """Envia un mensaje de texto o media via la API de YCloud.

        Args:
            to: Numero de telefono destino en formato E.164 sin `+`.
            content: Contenido a enviar.
            channel_config: Debe traer `api_key` y `phone_number_id`.

        Returns:
            `id` del mensaje de WhatsApp asignado por YCloud.
        """
        payload: dict[str, Any] = {
            "from": channel_config["phone_number_id"],
            "to": to,
        }

        if content.media_url:
            media_type = content.media_type or "image"
            payload["type"] = media_type
            payload[media_type] = {"link": content.media_url, "caption": content.caption}
        else:
            payload["type"] = "text"
            payload["text"] = {"body": content.text or ""}

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{get_settings().YCLOUD_BASE_URL}/whatsapp/messages",
                json=payload,
                headers={
                    "X-API-Key": channel_config["api_key"],
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            return str(data.get("whatsappMessage", {}).get("id", ""))

    async def send_template(
        self, to: str, template: TemplateMessage, channel_config: dict[str, Any]
    ) -> str:
        """Envia un template aprobado de WhatsApp (HSM), fuera de la ventana de 24h.

        Args:
            to: Numero de telefono destino en formato E.164 sin `+`.
            template: Template preaprobado a enviar.
            channel_config: Debe traer `api_key` y `phone_number_id`.

        Returns:
            `id` del mensaje de WhatsApp asignado por YCloud.
        """
        payload = {
            "from": channel_config["phone_number_id"],
            "to": to,
            "type": "template",
            "template": {
                "name": template.template_name,
                "language": {"code": template.language},
                "components": template.components,
            },
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{get_settings().YCLOUD_BASE_URL}/whatsapp/messages",
                json=payload,
                headers={"X-API-Key": channel_config["api_key"]},
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            return str(data.get("whatsappMessage", {}).get("id", ""))

    def get_channel_constraints(self) -> ChannelConstraints:
        """Restricciones de WhatsApp Business API.

        Returns:
            4096 caracteres, ventana de sesion de 24h con template obligatorio
            fuera de ella.
        """
        return ChannelConstraints(
            max_text_length=4096,
            supported_media_types=["image", "audio", "video", "document"],
            session_window_hours=24,
            requires_template_outside_window=True,
            max_buttons=3,
            max_list_items=10,
        )
