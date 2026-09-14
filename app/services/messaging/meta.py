"""MetaProvider — Instagram DM y Facebook Messenger via Meta Graph API.

Documentacion Messenger: https://developers.facebook.com/docs/messenger-platform/
Documentacion Instagram: https://developers.facebook.com/docs/instagram-messaging/

Ambos canales comparten la misma Graph API y el mismo formato de webhook
(`entry[].messaging[]`); lo unico que cambia es el `object` de nivel superior
(`"instagram"` vs `"page"`) y las restricciones de cada canal. Por eso un solo
`MetaProvider` cubre los dos, diferenciados por `provider_config["channel"]`.

Contrato: `specs/sprint-04-webhooks.md` §4.
"""

import hashlib
import hmac
from datetime import datetime, timezone
from enum import StrEnum
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

# Tipos de attachment que Meta reporta; "file" se normaliza a "document".
_ATTACHMENT_TYPES = ("image", "audio", "video", "file")


class MetaChannel(StrEnum):
    """Sub-canal de Meta: Instagram Direct o Facebook Messenger."""

    INSTAGRAM = "instagram"
    FACEBOOK_MESSENGER = "facebook"


class MetaProvider(MessagingProvider):
    """Implementacion de `MessagingProvider` para Instagram DM y Facebook Messenger.

    El endpoint de webhooks (`app/api/v1/webhooks.py::_resolve_provider`) solo
    pasa `{"channel": channel}` al construirlo — el `page_access_token` y el
    `app_secret` de `self` quedan vacios en ese camino a proposito. La firma se
    valida con el secreto que llega como parametro en `validate_signature()`
    (`settings.META_APP_SECRET`), y el envio recibe sus credenciales en
    `channel_config` en cada llamada; ninguno de los dos depende de `self`.
    """

    GRAPH_API_VERSION_DEFAULT = "v19.0"

    def __init__(self, provider_config: dict[str, Any]) -> None:
        """Inicializa el provider para un sub-canal de Meta.

        Args:
            provider_config: Config del canal. `channel` es obligatorio
                ("instagram" o "facebook"); `page_access_token` y `app_secret`
                son opcionales aqui porque se pueden resolver mas tarde por
                llamada (ver docstring de la clase).
        """
        self.page_access_token = provider_config.get("page_access_token", "")
        self.app_secret = provider_config.get("app_secret", "")
        self.channel = MetaChannel(provider_config.get("channel", "facebook"))

    @property
    def _base_url(self) -> str:
        """URL base de la Graph API, con la version configurada en Settings."""
        version = get_settings().META_GRAPH_API_VERSION or self.GRAPH_API_VERSION_DEFAULT
        return f"https://graph.facebook.com/{version}"

    async def parse_webhook(self, raw_payload: dict[str, Any]) -> NormalizedMessage:
        """Normaliza el payload de un webhook entrante de Meta.

        Estructura esperada: ver `tests/fixtures/meta_payloads.py`
        (`INSTAGRAM_TEXT`, `INSTAGRAM_IMAGE`, `INSTAGRAM_QUICK_REPLY`,
        `FACEBOOK_TEXT`).

        Args:
            raw_payload: Payload JSON del webhook, ya deserializado.

        Returns:
            Mensaje normalizado con `channel=instagram` o `channel=facebook`
            segun `self.channel`.
        """
        entry = raw_payload.get("entry", [{}])[0]
        messaging = entry.get("messaging", [{}])[0]
        sender_id = messaging.get("sender", {}).get("id", "")
        message = messaging.get("message", {})

        text = message.get("text")
        media_url: str | None = None
        media_type: MessageTypeEnum | None = None

        attachments = message.get("attachments", [])
        if attachments:
            attachment = attachments[0]
            att_type = attachment.get("type", "")
            if att_type in _ATTACHMENT_TYPES:
                media_url = attachment.get("payload", {}).get("url")
                media_type = MessageTypeEnum(att_type if att_type != "file" else "document")

        quick_reply = message.get("quick_reply")
        interactive_response: dict[str, Any] | None = None
        if quick_reply:
            interactive_response = {"type": "quick_reply", "payload": quick_reply.get("payload")}

        channel = (
            ChannelEnum.instagram if self.channel == MetaChannel.INSTAGRAM else ChannelEnum.facebook
        )

        return NormalizedMessage(
            channel=channel,
            sender_identifier=sender_id,
            text=text,
            media_url=media_url,
            media_type=media_type,
            timestamp=datetime.fromtimestamp(messaging.get("timestamp", 0) / 1000, tz=timezone.utc),
            external_message_id=message.get("mid", ""),
            raw_payload=raw_payload,
            interactive_response=interactive_response,
        )

    async def validate_signature(self, payload: bytes, signature: str, secret: str) -> bool:
        """Valida el header `x-hub-signature-256` de Meta.

        El formato es `sha256=<hex_digest>`; sin ese prefijo la firma se
        rechaza directamente (a diferencia de YCloud, que no lo usa).

        Args:
            payload: Cuerpo crudo del request.
            signature: Valor del header `x-hub-signature-256`.
            secret: `META_APP_SECRET` configurado.

        Returns:
            True si la firma coincide.
        """
        if not signature.startswith("sha256="):
            return False
        expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature[len("sha256=") :], expected)

    async def send_message(
        self, to: str, content: MessageContent, channel_config: dict[str, Any]
    ) -> str:
        """Envia un mensaje via la Send API de Meta (Instagram y Messenger).

        Args:
            to: PSID (Page-Scoped ID) del destinatario.
            content: Contenido a enviar.
            channel_config: Debe traer `page_access_token`.

        Returns:
            `message_id` asignado por Meta.
        """
        message_payload: dict[str, Any] = {"recipient": {"id": to}}

        if content.media_url:
            media_type = content.media_type or "image"
            message_payload["message"] = {
                "attachment": {
                    "type": media_type,
                    "payload": {"url": content.media_url, "is_reusable": True},
                }
            }
        else:
            message_payload["message"] = {"text": content.text or ""}

        if content.buttons:
            message_payload["message"]["quick_replies"] = [
                {
                    "content_type": "text",
                    "title": btn.get("title", ""),
                    "payload": btn.get("id", ""),
                }
                for btn in content.buttons
            ]

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._base_url}/me/messages",
                params={"access_token": channel_config["page_access_token"]},
                json=message_payload,
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            return str(data.get("message_id", ""))

    async def send_template(
        self, to: str, template: TemplateMessage, channel_config: dict[str, Any]
    ) -> str:
        """Envia un template de boton (solo Facebook Messenger).

        Args:
            to: PSID (Page-Scoped ID) del destinatario.
            template: Template a enviar.
            channel_config: Debe traer `page_access_token`.

        Returns:
            `message_id` asignado por Meta.

        Raises:
            NotImplementedError: Instagram no soporta templates; hay que usar
                `send_message()` con quick replies.
        """
        if self.channel == MetaChannel.INSTAGRAM:
            raise NotImplementedError(
                "Instagram no soporta templates. Usar send_message con quick_replies."
            )

        message_payload = {
            "recipient": {"id": to},
            "message": {
                "attachment": {
                    "type": "template",
                    "payload": {
                        "template_type": "button",
                        "text": template.template_name,
                        "buttons": template.components,
                    },
                }
            },
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._base_url}/me/messages",
                params={"access_token": channel_config["page_access_token"]},
                json=message_payload,
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            return str(data.get("message_id", ""))

    def get_channel_constraints(self) -> ChannelConstraints:
        """Restricciones del sub-canal actual.

        Returns:
            Instagram: 1000 caracteres, sin templates, sin listas.
            Facebook Messenger: 2000 caracteres, con templates de boton.
        """
        if self.channel == MetaChannel.INSTAGRAM:
            return ChannelConstraints(
                max_text_length=1000,
                supported_media_types=["image", "audio", "video"],
                session_window_hours=24,
                requires_template_outside_window=False,
                max_buttons=13,
                max_list_items=0,
            )

        return ChannelConstraints(
            max_text_length=2000,
            supported_media_types=["image", "audio", "video", "document"],
            session_window_hours=24,
            requires_template_outside_window=False,
            max_buttons=3,
            max_list_items=4,
        )
