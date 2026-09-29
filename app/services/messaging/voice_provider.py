"""TwilioVoiceProvider — el canal de voz (llamadas telefonicas) por Twilio.

Contrato: `specs/sprint-13-advanced-modules.md` §1, adaptado al ABC real
(`app/services/messaging/base.py`), que no es el del spec: aqui
`parse_webhook()` recibe un dict, `validate_signature()` una firma ya extraida
y `send_message()` un `MessageContent`.

Como encaja una llamada en un canal de mensajeria
--------------------------------------------------
Igual que el Webchat (ADR-059), la voz no es un webhook por mensaje:

- **Entrada.** Twilio abre un WebSocket (Media Streams) con el audio de la
  llamada; `app/api/v1/voice_ws.py` corta el audio en frases y las transcribe.
  Cada frase se normaliza con `parse_webhook()`, con los nombres de campo de
  Twilio (`CallSid`, `From`, `SpeechResult`...), y se encola en el mismo
  `process_incoming_message` que un WhatsApp. El resto de la plataforma
  (contactos, conversaciones, grafo, CRM) no sabe que fue una llamada.
- **Salida.** La respuesta la genera un worker de Celery y el WebSocket vive
  en un proceso de la API: `send_message()` la publica en un canal de Redis
  por llamante y la conexion de la llamada, suscrita, la sintetiza y la manda
  a Twilio. Sin la llamada activa no hay a quien decirselo (ver
  `send_message()`).

Solo Twilio: el spec pide ademas Vonage como backend alternativo; el usuario
eligio Twilio y un segundo proveedor seria otra clase registrada en la factory,
no un `if backend ==` en cada metodo.

El numero del llamante no unifica contactos
-------------------------------------------
`NormalizedMessage.verified_phone` queda vacio: el caller ID de una llamada se
puede falsificar, y usarlo para unir el contacto con el de WhatsApp del mismo
numero le daria a cualquiera el historial de otro con solo cambiar su caller
ID. El identificador del contacto en este canal es el numero, en el canal
`voice`, separado del de WhatsApp.
"""

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode
from uuid import UUID, uuid4

import httpx

from app.core.encryption import blind_index
from app.schemas.message import ChannelEnum, NormalizedMessage
from app.services.dedup import get_redis
from app.services.messaging.base import (
    ChannelConstraints,
    IgnoredWebhookError,
    MessageContent,
    MessagingProvider,
    TemplateMessage,
    TemplateNotSupportedError,
)

logger = logging.getLogger(__name__)

#: Texto maximo de una respuesta por voz; el TTS la parte en trozos.
MAX_TEXT_LENGTH = 4000

#: Timeout de las llamadas a la API REST de Twilio.
TWILIO_TIMEOUT_SECONDS = 10.0


#: Claves que solo hacen falta para llamar hacia fuera.
CREDENCIALES_SALIENTES: tuple[str, ...] = (
    "account_sid",
    "auth_token",
    "phone_number",
    "api_base_url",
)


def faltan_credenciales_salientes(channel_config: dict[str, Any]) -> list[str]:
    """Dice que credenciales REST faltan para iniciar una llamada saliente.

    Args:
        channel_config: Configuracion del canal de voz.

    Returns:
        Las claves ausentes o vacias; lista vacia si estan todas.
    """
    return [clave for clave in CREDENCIALES_SALIENTES if not channel_config.get(clave)]


class TwilioAPIError(RuntimeError):
    """La API REST de Twilio rechazo o no contesto una operacion."""


def canal_de_salida(client_id: UUID | str, telefono: str) -> str:
    """Canal de Redis por el que se entregan las respuestas a un llamante.

    Lleva el indice ciego del numero, no el numero: los nombres de canal
    aparecen en `PUBSUB CHANNELS`, en `MONITOR` y en los logs de Redis.

    Args:
        client_id: Tenant.
        telefono: Numero del llamante, en E.164.

    Returns:
        El nombre del canal.
    """
    return f"voice:out:{client_id}:{blind_index(telefono, client_id)}"


def twilio_signature(url: str, params: list[tuple[str, str]], auth_token: str) -> str:
    """Calcula la firma `X-Twilio-Signature` de un webhook.

    Algoritmo documentado por Twilio: la URL completa (con query string)
    seguida de cada parametro POST, ordenados por nombre, como `nombreValor`;
    HMAC-SHA1 con el auth token, en base64.

    Args:
        url: URL publica exacta a la que Twilio hizo el request.
        params: Parametros del cuerpo, en pares (un nombre puede repetirse).
        auth_token: `TWILIO_AUTH_TOKEN`.

    Returns:
        La firma esperada.
    """
    datos = url + "".join(f"{clave}{valor}" for clave, valor in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), datos.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def numero_del_contacto(payload: dict[str, Any]) -> str:
    """Numero del cliente en una llamada, sea entrante o saliente.

    Args:
        payload: Campos de Twilio (`From`, `To`, `Direction`).

    Returns:
        `From` en una llamada entrante; `To` en una saliente (`outbound-api`,
        `outbound-dial`), donde `From` es el numero de la plataforma.
    """
    direccion = str(payload.get("Direction") or "inbound")
    campo = "To" if direccion.startswith("outbound") else "From"
    return str(payload.get(campo) or "").strip()


class TwilioVoiceProvider(MessagingProvider):
    """Implementacion de `MessagingProvider` para llamadas por Twilio.

    Attributes:
        url: URL publica del request que se esta validando. Solo la tienen los
            endpoints de voz; sin ella, `validate_signature()` rechaza.
    """

    def __init__(self, provider_config: dict[str, Any] | None = None) -> None:
        """Crea el provider.

        Args:
            provider_config: `{"url": ...}` para validar un webhook. La factory
                lo construye con `{"channel": ...}`, que no trae URL.
        """
        self.url: str | None = (provider_config or {}).get("url")

    async def parse_webhook(self, raw_payload: dict[str, Any]) -> NormalizedMessage:
        """Normaliza una frase de la llamada ya transcrita.

        Args:
            raw_payload: Campos con los nombres de Twilio: `CallSid`, `From`,
                `To`, `Direction`, `SpeechResult` o `Digits`, `Confidence` y
                `UtteranceIndex` (numero de frase dentro de la llamada, propio
                de la plataforma).

        Returns:
            El mensaje normalizado. El id externo es `CallSid:UtteranceIndex`,
            unico por frase, para que la deduplicacion no descarte la segunda
            frase de una misma llamada.

        Raises:
            IgnoredWebhookError: Si no trae texto (un status callback, por
                ejemplo): no hay nada que responder.
            ValueError: Si falta `CallSid` o el numero del cliente.
        """
        call_sid = str(raw_payload.get("CallSid") or "").strip()
        telefono = numero_del_contacto(raw_payload)
        if not call_sid or not telefono:
            raise ValueError("Twilio: falta CallSid o el numero del cliente")

        texto = str(raw_payload.get("SpeechResult") or raw_payload.get("Digits") or "").strip()
        if not texto:
            raise IgnoredWebhookError("Evento de llamada sin voz ni digitos")

        indice = str(raw_payload.get("UtteranceIndex") or "0")
        return NormalizedMessage(
            channel=ChannelEnum.voice,
            sender_identifier=telefono,
            text=texto,
            timestamp=datetime.now(timezone.utc),
            external_message_id=f"{call_sid}:{indice}",
            # Compacto: acaba en `messages.metadata` y en la cola de Celery.
            raw_payload={
                "call_sid": call_sid,
                "utterance_index": indice,
                "direction": raw_payload.get("Direction") or "inbound",
                "confidence": raw_payload.get("Confidence"),
            },
        )

    async def validate_signature(self, payload: bytes, signature: str, secret: str) -> bool:
        """Valida `X-Twilio-Signature` sobre un cuerpo `x-www-form-urlencoded`.

        Args:
            payload: Cuerpo crudo del request.
            signature: Valor de `X-Twilio-Signature`.
            secret: `TWILIO_AUTH_TOKEN`.

        Returns:
            True si la firma coincide. False sin URL (el endpoint generico de
            webhooks no la conoce), sin secreto configurado o sin firma.
        """
        if not self.url or not secret or not signature:
            return False
        try:
            params = parse_qsl(payload.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            return False
        esperada = twilio_signature(self.url, params, secret)
        return hmac.compare_digest(esperada.encode("ascii"), signature.encode("utf-8"))

    async def send_message(
        self, to: str, content: MessageContent, channel_config: dict[str, Any]
    ) -> str:
        """Publica la respuesta para que la llamada activa la diga.

        Si el cliente ya colgo no hay nadie suscrito y la respuesta no se dice;
        queda igual en el historial de la conversacion (como en el Webchat), y
        se registra un warning. No se lanza error: reintentar no la entregaria,
        y el worker escalaria a un humano una conversacion que simplemente
        termino.

        Args:
            to: Numero del cliente (E.164).
            content: Texto a decir. La voz no envia media ni botones.
            channel_config: Debe traer `client_id`.

        Returns:
            Id generado para el mensaje.

        Raises:
            ValueError: Si el contenido lleva media.
        """
        if content.media_url:
            raise ValueError("El canal de voz no envia media")

        message_id = uuid4().hex
        frame = {"type": "say", "message_id": message_id, "text": content.text or ""}
        oyentes = await get_redis().publish(
            canal_de_salida(channel_config["client_id"], to), json.dumps(frame)
        )
        if not oyentes:
            logger.warning("Respuesta de voz %s sin llamada activa que la diga", message_id)
        return message_id

    async def send_template(
        self, to: str, template: TemplateMessage, channel_config: dict[str, Any]
    ) -> str:
        """La voz no tiene templates preaprobados.

        Args:
            to: Sin uso.
            template: Sin uso.
            channel_config: Sin uso.

        Raises:
            TemplateNotSupportedError: Siempre.
        """
        raise TemplateNotSupportedError("El canal de voz no soporta templates preaprobados")

    def get_channel_constraints(self) -> ChannelConstraints:
        """Restricciones del canal de voz.

        Returns:
            Solo texto (que se sintetiza), sin ventana de sesion ni botones.
        """
        return ChannelConstraints(
            max_text_length=MAX_TEXT_LENGTH,
            supported_media_types=[],
            session_window_hours=None,
            requires_template_outside_window=False,
            max_buttons=0,
            max_list_items=0,
        )

    async def start_call(
        self,
        to: str,
        channel_config: dict[str, Any],
        answer_url: str,
        status_callback_url: str,
    ) -> str:
        """Inicia una llamada saliente con la API REST de Twilio.

        Cuando el cliente contesta, Twilio pide el TwiML a `answer_url` (el
        mismo endpoint que las entrantes, que distingue por `Direction`).

        Args:
            to: Numero a llamar, E.164.
            channel_config: `account_sid`, `auth_token`, `phone_number` y
                `api_base_url`.
            answer_url: URL publica del TwiML de la llamada.
            status_callback_url: URL publica del status callback.

        Returns:
            El `CallSid` de la llamada creada.

        Raises:
            TwilioAPIError: Si Twilio la rechaza o no contesta.
        """
        # Las credenciales REST se validan aqui y no en `get_channel_config()`:
        # responder en una llamada ya en curso no las necesita, y exigirlas para
        # todo el canal dejaba mudo un despliegue de solo entrada. El endpoint
        # las comprueba antes con `faltan_credenciales_salientes()` para
        # responder 503; esto es la red de seguridad de la propia clase.
        faltantes = faltan_credenciales_salientes(channel_config)
        if faltantes:
            raise TwilioAPIError(f"Faltan credenciales para llamar hacia fuera: {faltantes}")
        sid = channel_config["account_sid"]
        url = f"{channel_config['api_base_url'].rstrip('/')}/2010-04-01/Accounts/{sid}/Calls.json"
        datos = [
            ("To", to),
            ("From", channel_config["phone_number"]),
            ("Url", answer_url),
            ("StatusCallback", status_callback_url),
            ("StatusCallbackEvent", "initiated"),
            ("StatusCallbackEvent", "answered"),
            ("StatusCallbackEvent", "completed"),
        ]
        try:
            async with httpx.AsyncClient(timeout=TWILIO_TIMEOUT_SECONDS) as cliente:
                # `content` y no `data`: `StatusCallbackEvent` se repite, y un
                # dict se quedaria solo con el ultimo valor.
                respuesta = await cliente.post(
                    url,
                    content=urlencode(datos),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    auth=(sid, channel_config["auth_token"]),
                )
        except httpx.HTTPError as exc:
            raise TwilioAPIError(f"Twilio no respondio: {type(exc).__name__}") from exc

        if respuesta.status_code != 201:
            # El cuerpo de error de Twilio es `{code, message, more_info}`: no
            # trae credenciales, y el codigo es lo que hace falta para operar.
            try:
                detalle = respuesta.json()
            except ValueError:
                detalle = {}
            raise TwilioAPIError(
                f"Twilio rechazo la llamada ({respuesta.status_code}, "
                f"codigo {detalle.get('code')}): {detalle.get('message')}"
            )
        return str(respuesta.json()["sid"])
