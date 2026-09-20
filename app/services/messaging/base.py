"""MessagingProvider ABC — desacopla la app de cualquier proveedor de mensajeria.

Cada canal/proveedor (YCloud, Meta, ...) implementa esta interfaz. El resto de la
aplicacion (endpoint de webhooks, worker, nodo `respond` del grafo en Sprint 6)
nunca interactua directamente con la API de un proveedor concreto.

Contrato: `specs/sprint-04-webhooks.md` §1.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.schemas.message import NormalizedMessage


class TemplateNotSupportedError(NotImplementedError):
    """El canal no tiene el concepto de template preaprobado.

    Solo WhatsApp (HSM) y Facebook (button template) lo tienen. Los demas
    canales implementan `send_template()` para cumplir la ABC y lanzan esto, en
    vez de fingir un envio: quien lo llame tiene que saber que el canal no lo
    soporta.
    """


@dataclass
class ChannelConstraints:
    """Restricciones del canal de mensajeria.

    Attributes:
        max_text_length: Longitud maxima del texto de un mensaje.
        supported_media_types: Tipos de media que el canal acepta.
        session_window_hours: Ventana de sesion en horas para responder sin
            template (None si el canal no tiene ventana).
        requires_template_outside_window: Si hace falta un template aprobado
            para escribir fuera de la ventana de sesion.
        max_buttons: Maximo de botones en un mensaje interactivo.
        max_list_items: Maximo de elementos en una lista interactiva.
    """

    max_text_length: int
    supported_media_types: list[str]
    session_window_hours: int | None
    requires_template_outside_window: bool
    max_buttons: int
    max_list_items: int


@dataclass
class MessageContent:
    """Contenido de un mensaje saliente.

    Attributes:
        text: Cuerpo de texto del mensaje.
        media_url: URL del recurso multimedia a enviar.
        media_type: Tipo de media (image, audio, video, document).
        buttons: Botones/quick replies, como lista de dicts con `title`/`id`.
        caption: Texto que acompana a un media_url.
    """

    text: str | None = None
    media_url: str | None = None
    media_type: str | None = None
    buttons: list[dict[str, Any]] | None = None
    caption: str | None = None


@dataclass
class TemplateMessage:
    """Mensaje de template preaprobado (WhatsApp HSM, Facebook button template).

    Attributes:
        template_name: Nombre del template registrado ante el proveedor.
        language: Codigo de idioma del template (e.g. "es").
        components: Componentes del template (header, body, buttons) con sus
            parametros, en el formato que espera cada proveedor.
    """

    template_name: str
    language: str
    components: list[dict[str, Any]]


class MessagingProvider(ABC):
    """Interfaz abstracta para proveedores de mensajeria.

    Patron: cada canal/proveedor implementa esta interfaz. La aplicacion nunca
    interactua directamente con APIs de proveedores.
    """

    @abstractmethod
    async def parse_webhook(self, raw_payload: dict[str, Any]) -> NormalizedMessage:
        """Parsea el payload crudo del webhook y lo normaliza.

        Cada proveedor tiene un formato distinto; esta funcion los unifica en
        un `NormalizedMessage` comun.

        Args:
            raw_payload: Payload JSON del webhook, ya deserializado.

        Returns:
            Mensaje normalizado, independiente del proveedor de origen.
        """
        ...

    @abstractmethod
    async def validate_signature(self, payload: bytes, signature: str, secret: str) -> bool:
        """Valida la firma del webhook para autenticar el origen.

        Args:
            payload: Cuerpo crudo (bytes) del request, sin parsear.
            signature: Valor del header de firma que envio el proveedor.
            secret: Secreto configurado para ese proveedor/tenant.

        Returns:
            True si la firma es valida.
        """
        ...

    @abstractmethod
    async def send_message(
        self, to: str, content: MessageContent, channel_config: dict[str, Any]
    ) -> str:
        """Envia un mensaje al destinatario.

        Args:
            to: Identificador del destinatario segun el canal.
            content: Contenido del mensaje a enviar.
            channel_config: Credenciales/config del canal para este tenant.

        Returns:
            `external_message_id` asignado por el proveedor al mensaje enviado.
        """
        ...

    @abstractmethod
    async def send_template(
        self, to: str, template: TemplateMessage, channel_config: dict[str, Any]
    ) -> str:
        """Envia un mensaje de template, necesario fuera de la ventana de sesion.

        Args:
            to: Identificador del destinatario segun el canal.
            template: Template preaprobado a enviar.
            channel_config: Credenciales/config del canal para este tenant.

        Returns:
            `external_message_id` asignado por el proveedor al mensaje enviado.
        """
        ...

    @abstractmethod
    def get_channel_constraints(self) -> ChannelConstraints:
        """Retorna las restricciones del canal.

        Returns:
            Restricciones (longitud de texto, ventana de sesion, etc.).
        """
        ...
