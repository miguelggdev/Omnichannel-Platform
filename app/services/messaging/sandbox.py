"""SandboxProvider — el canal de las conversaciones de prueba del sandbox (ADR-078).

No hay a quien enviarle nada: quien escribe es el administrador probando su
configuracion desde `POST /api/v1/sandbox/messages`, y la respuesta vuelve en
esa misma peticion. `send_message()` no toca la red ni Redis; lo que el agente
"envia" queda en `messages` (lo guarda `deliver_message()`), como en cualquier
otro canal.

Un canal propio, y no `webchat`, porque el webchat exige que `DEFAULT_CLIENT_ID`
este configurado y publica en un canal de Redis de **ese** tenant: una prueba del
sandbox no debe depender de la configuracion de otro ni dejar rastro en ella.
Tampoco recibe webhooks: `validate_signature()` devuelve siempre `False`, de modo
que el endpoint generico `POST /api/v1/webhooks/sandbox/...` que la factory
registra no puede aceptar nada.
"""

from typing import Any
from uuid import uuid4

from app.schemas.message import NormalizedMessage
from app.services.messaging.base import (
    ChannelConstraints,
    MessageContent,
    MessagingProvider,
    TemplateMessage,
    TemplateNotSupportedError,
)

MAX_TEXT_LENGTH = 10_000


class SandboxProvider(MessagingProvider):
    """Proveedor de mensajeria que no entrega nada fuera del proceso."""

    async def parse_webhook(self, raw_payload: dict[str, Any]) -> NormalizedMessage:
        """El sandbox no recibe webhooks.

        Args:
            raw_payload: Sin uso.

        Raises:
            NotImplementedError: Siempre.
        """
        raise NotImplementedError("El canal sandbox no recibe webhooks")

    async def validate_signature(self, payload: bytes, signature: str, secret: str) -> bool:
        """Rechaza todo: no hay un tercero que firme nada.

        Args:
            payload: Sin uso.
            signature: Sin uso.
            secret: Sin uso.

        Returns:
            `False`, siempre.
        """
        return False

    async def send_message(
        self, to: str, content: MessageContent, channel_config: dict[str, Any]
    ) -> str:
        """No envia nada; devuelve un id para que el mensaje quede registrado.

        Args:
            to: Sin uso.
            content: Sin uso.
            channel_config: Sin uso.

        Returns:
            Un id externo generado.
        """
        return uuid4().hex

    async def send_template(
        self, to: str, template: TemplateMessage, channel_config: dict[str, Any]
    ) -> str:
        """El sandbox no tiene templates.

        Args:
            to: Sin uso.
            template: Sin uso.
            channel_config: Sin uso.

        Raises:
            TemplateNotSupportedError: Siempre.
        """
        raise TemplateNotSupportedError("El canal sandbox no soporta templates")

    def get_channel_constraints(self) -> ChannelConstraints:
        """Sin ventana de sesion y solo texto.

        Returns:
            Las restricciones del canal.
        """
        return ChannelConstraints(
            max_text_length=MAX_TEXT_LENGTH,
            supported_media_types=[],
            session_window_hours=None,
            requires_template_outside_window=False,
            max_buttons=0,
            max_list_items=0,
        )
