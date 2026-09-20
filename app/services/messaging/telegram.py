"""TelegramProvider — Telegram Bot API como canal de clientes.

Documentacion: https://core.telegram.org/bots/api

Desviaciones deliberadas sobre `specs/sprint-09-channels.md` §1, todas por lo
que el codigo real ya hace o por lo que romperia en produccion:

- **Firma de la ABC real.** El spec usa `parse_webhook(payload, headers)` y
  `send_message(recipient_id, content, **kwargs) -> dict`; la ABC de Sprint 4 es
  `parse_webhook(raw_payload)` y `send_message(to, content, channel_config) -> str`.
- **Sin `getFile` dentro de `parse_webhook`.** El spec resuelve la URL de un
  archivo con una llamada a la API en el camino del webhook, que tiene que
  responder en <100 ms (CLAUDE.md, regla 4). Aqui `media_url` lleva un
  identificador `telegram-file:<file_id>` y quien necesite el binario (la
  transcripcion de audio, por ejemplo) lo resuelve con `get_file_url()`.
- **Sin `parse_mode: HTML`.** El texto lo genera un LLM: un `<` o un `&` sin
  escapar hace que Telegram responda 400 "can't parse entities" y el mensaje se
  pierde. Se envia como texto plano.
- **Troceo propio.** Telegram rechaza mas de 4096 caracteres, y nada aguas arriba
  consulta `get_channel_constraints()`: el troceo lo hace el provider.
- **`update_id` como id externo.** `message_id` solo es unico dentro de un chat;
  la clave de deduplicacion es global por canal, asi que dos usuarios distintos
  con el mismo `message_id` se descartarian entre si.
- **Sin lista blanca de IPs.** El mecanismo de autenticidad que documenta
  Telegram es el `secret_token` de `setWebhook`; una lista de IPs detras de
  Cloudflare y Traefik depende de reenviar bien la IP de origen y falla en
  silencio.
- **Solo chats privados.** En un grupo, responder al `from.id` escribiria un DM al
  usuario, que Telegram rechaza si nunca inicio la conversacion.

El token del bot viaja en el path de la URL (`/bot<TOKEN>/metodo`) sin otra
alternativa: por eso los errores de red se reescriben sin la URL y
`app/core/telemetry.py` la redacta en los spans.
"""

import hmac
from datetime import datetime, timezone
from typing import Any

import httpx

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

#: Prefijo de `media_url` para un archivo de Telegram aun sin resolver.
TELEGRAM_FILE_PREFIX = "telegram-file:"

#: Limites de la Bot API.
MAX_TEXT_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024

_TIMEOUT_SECONDS = 30.0

# Tipo de mensaje de Telegram -> (clave del payload, tipo normalizado).
_MEDIA_KEYS: dict[str, MessageTypeEnum] = {
    "voice": MessageTypeEnum.audio,
    "audio": MessageTypeEnum.audio,
    "video": MessageTypeEnum.video,
    "video_note": MessageTypeEnum.video,
    "document": MessageTypeEnum.document,
}

# Tipo normalizado -> metodo de la Bot API que lo envia y su campo de archivo.
_SEND_METHODS: dict[str, tuple[str, str]] = {
    "image": ("sendPhoto", "photo"),
    "audio": ("sendAudio", "audio"),
    "video": ("sendVideo", "video"),
    "document": ("sendDocument", "document"),
}


class TelegramAPIError(RuntimeError):
    """La Bot API rechazo la peticion, o no se pudo hablar con ella.

    Attributes:
        error_code: Codigo HTTP/de Telegram, si lo hubo.
        retry_after: Segundos que Telegram pide esperar (429), si los indico.
    """

    def __init__(
        self, message: str, error_code: int | None = None, retry_after: int | None = None
    ) -> None:
        """Guarda el motivo y los metadatos que sirven para decidir un reintento.

        Args:
            message: Descripcion del error, sin el token del bot.
            error_code: Codigo devuelto por Telegram.
            retry_after: Segundos de espera pedidos por Telegram.
        """
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after


def _sin_token(texto: str, token: str) -> str:
    """Quita el token del bot de un texto (mensajes de error, URLs).

    Args:
        texto: Texto que puede contener el token.
        token: Token del bot.

    Returns:
        El texto con el token reemplazado por `***`.
    """
    return texto.replace(token, "***") if token else texto


def partir_texto(texto: str, limite: int = MAX_TEXT_LENGTH) -> list[str]:
    """Parte un texto largo en trozos de como mucho `limite` caracteres.

    Corta donde menos molesta: primero en un salto de linea, luego en un espacio,
    y solo si no hay ninguno, en seco.

    Args:
        texto: Texto a enviar.
        limite: Longitud maxima de cada trozo.

    Returns:
        Lista de trozos no vacios; una lista vacia si el texto esta vacio.
    """
    texto = texto.strip()
    trozos: list[str] = []
    while len(texto) > limite:
        ventana = texto[:limite]
        corte = ventana.rfind("\n")
        if corte < limite // 2:
            corte = ventana.rfind(" ")
        if corte < limite // 2:
            corte = limite
        trozos.append(texto[:corte].rstrip())
        texto = texto[corte:].lstrip()
    if texto:
        trozos.append(texto)
    return trozos


def _nombre(perfil: dict[str, Any]) -> str | None:
    """Nombre publico del remitente, o su `@usuario` si no tiene nombre.

    Args:
        perfil: Objeto `from` de Telegram.

    Returns:
        El nombre para mostrar en la bandeja, o `None`.
    """
    completo = " ".join(p for p in (perfil.get("first_name"), perfil.get("last_name")) if p).strip()
    if completo:
        return completo
    usuario = perfil.get("username")
    return f"@{usuario}" if usuario else None


class TelegramProvider(MessagingProvider):
    """Implementacion de `MessagingProvider` para Telegram (Bot API).

    No necesita config en el constructor: el secreto del webhook llega desde el
    endpoint y el token del bot, por llamada, en `channel_config["bot_token"]`
    (mismo criterio que `YCloudProvider`).
    """

    # ─── Recepcion ──────────────────────────────────────────────────────────

    async def parse_webhook(self, raw_payload: dict[str, Any]) -> NormalizedMessage:
        """Normaliza un `Update` de Telegram.

        Args:
            raw_payload: `Update` ya deserializado.

        Returns:
            Mensaje normalizado con `channel=telegram`.

        Raises:
            ValueError: Si el update no es un mensaje o un boton de un chat
                privado, o si el tipo de mensaje no esta soportado. El endpoint
                lo trata como `parse_error` (200, sin reintento del proveedor):
                reintentar un update que nunca se va a poder procesar es ruido.
        """
        update_id = raw_payload.get("update_id")
        if update_id is None:
            raise ValueError("Update de Telegram sin `update_id`")

        if "callback_query" in raw_payload:
            return self._parse_callback(raw_payload, str(update_id))

        mensaje = raw_payload.get("message")
        if not isinstance(mensaje, dict):
            raise IgnoredWebhookError(
                "Update de Telegram sin `message` ni `callback_query` "
                f"(claves: {sorted(k for k in raw_payload if k != 'update_id')})"
            )

        chat = mensaje.get("chat") or {}
        perfil = mensaje.get("from") or {}
        self._exigir_chat_privado_de_una_persona(chat, perfil)

        texto: str | None = mensaje.get("text")
        media_url: str | None = None
        media_type: MessageTypeEnum | None = None
        location: dict[str, Any] | None = None

        if mensaje.get("photo"):
            # `photo` trae la misma imagen en varios tamanos, de menor a mayor.
            media_url = f"{TELEGRAM_FILE_PREFIX}{mensaje['photo'][-1]['file_id']}"
            media_type = MessageTypeEnum.image
            texto = mensaje.get("caption")
        elif any(clave in mensaje for clave in _MEDIA_KEYS):
            clave = next(c for c in _MEDIA_KEYS if c in mensaje)
            media_url = f"{TELEGRAM_FILE_PREFIX}{mensaje[clave]['file_id']}"
            media_type = _MEDIA_KEYS[clave]
            texto = mensaje.get("caption")
        elif "location" in mensaje:
            ubicacion = mensaje["location"]
            location = {
                "latitude": ubicacion.get("latitude"),
                "longitude": ubicacion.get("longitude"),
            }
        elif "contact" in mensaje:
            contacto = mensaje["contact"]
            nombre = " ".join(
                p for p in (contacto.get("first_name"), contacto.get("last_name")) if p
            )
            texto = f"Contacto compartido: {nombre} {contacto.get('phone_number', '')}".strip()
        elif texto is None:
            tipos = sorted(k for k in mensaje if k not in ("message_id", "from", "chat", "date"))
            raise IgnoredWebhookError(f"Tipo de mensaje de Telegram no soportado: {tipos}")

        return NormalizedMessage(
            channel=ChannelEnum.telegram,
            sender_identifier=str(chat["id"]),
            sender_name=_nombre(perfil),
            text=texto,
            media_url=media_url,
            media_type=media_type,
            timestamp=datetime.fromtimestamp(mensaje["date"], tz=timezone.utc),
            external_message_id=str(update_id),
            raw_payload=raw_payload,
            location=location,
        )

    def _parse_callback(self, raw_payload: dict[str, Any], update_id: str) -> NormalizedMessage:
        """Normaliza el toque de un boton inline.

        Args:
            raw_payload: `Update` con `callback_query`.
            update_id: Id del update, que hace de id externo.

        Returns:
            Mensaje con el `data` del boton como texto y como `interactive_response`.

        Raises:
            ValueError: Si el boton no viene de un chat privado.
        """
        callback = raw_payload["callback_query"]
        perfil = callback.get("from") or {}
        chat = (callback.get("message") or {}).get("chat") or {}
        self._exigir_chat_privado_de_una_persona(chat, perfil)

        data = callback.get("data") or ""
        return NormalizedMessage(
            channel=ChannelEnum.telegram,
            sender_identifier=str(chat["id"]),
            sender_name=_nombre(perfil),
            text=data,
            # Un callback no trae fecha propia.
            timestamp=datetime.now(timezone.utc),
            external_message_id=update_id,
            raw_payload=raw_payload,
            interactive_response={
                "type": "button_reply",
                "id": data,
                "callback_query_id": callback.get("id"),
            },
        )

    @staticmethod
    def _exigir_chat_privado_de_una_persona(chat: dict[str, Any], perfil: dict[str, Any]) -> None:
        """Descarta grupos, canales y otros bots.

        Args:
            chat: Objeto `chat` del update.
            perfil: Objeto `from` del update.

        Raises:
            ValueError: Si el chat no es privado, no tiene id o lo envia un bot.
        """
        if chat.get("type") != "private" or chat.get("id") is None:
            raise IgnoredWebhookError(
                f"Telegram: solo se atienden chats privados (tipo={chat.get('type')})"
            )
        if perfil.get("is_bot"):
            raise IgnoredWebhookError(
                "Telegram: mensaje de otro bot, se descarta para evitar bucles"
            )

    async def validate_signature(self, payload: bytes, signature: str, secret: str) -> bool:
        """Compara el `secret_token` que Telegram devuelve en cada update.

        Telegram no firma con HMAC: devuelve tal cual el `secret_token` que se
        registro en `setWebhook`, en el header `X-Telegram-Bot-Api-Secret-Token`.

        Args:
            payload: Cuerpo crudo (no participa: no hay firma sobre el cuerpo).
            signature: Valor del header.
            secret: `TELEGRAM_WEBHOOK_SECRET`.

        Returns:
            True si coincide. Un secreto vacio (sin configurar) nunca valida.
        """
        if not secret or not signature:
            return False
        return hmac.compare_digest(signature.encode("utf-8"), secret.encode("utf-8"))

    # ─── Envio ──────────────────────────────────────────────────────────────

    async def send_message(
        self, to: str, content: MessageContent, channel_config: dict[str, Any]
    ) -> str:
        """Envia texto o media por la Bot API, troceando lo que no cabe.

        Args:
            to: `chat_id` del destinatario.
            content: Contenido a enviar. Los botones se envian como teclado
                inline y van en el ultimo mensaje.
            channel_config: Debe traer `bot_token`.

        Returns:
            `message_id` del ultimo mensaje enviado.

        Raises:
            TelegramAPIError: Si Telegram rechaza la peticion o no responde.
        """
        token = channel_config["bot_token"]
        teclado = self._teclado(content.buttons)
        ultimo_id = ""

        if content.media_url:
            metodo, campo = _SEND_METHODS.get(content.media_type or "image", _SEND_METHODS["image"])
            pie = content.caption or content.text or ""
            # El pie de un media admite menos que un mensaje: si no cabe, el
            # media va sin pie y el texto sale despues como mensaje normal.
            cabe = len(pie) <= MAX_CAPTION_LENGTH
            cuerpo: dict[str, Any] = {"chat_id": to, campo: content.media_url}
            if cabe and pie:
                cuerpo["caption"] = pie
            if cabe and teclado:
                cuerpo["reply_markup"] = teclado
            ultimo_id = await self._llamar(token, metodo, cuerpo)
            if not cabe:
                return await self._enviar_texto(token, to, pie, teclado) or ultimo_id
            return ultimo_id

        return await self._enviar_texto(token, to, content.text or "", teclado)

    async def _enviar_texto(
        self, token: str, to: str, texto: str, teclado: dict[str, Any] | None
    ) -> str:
        """Envia un texto, partido en trozos de 4096, con el teclado al final.

        Args:
            token: Token del bot.
            to: `chat_id` destino.
            texto: Texto completo.
            teclado: `reply_markup` para el ultimo trozo, o `None`.

        Returns:
            `message_id` del ultimo trozo, o cadena vacia si no habia texto.
        """
        trozos = partir_texto(texto)
        ultimo_id = ""
        for i, trozo in enumerate(trozos):
            cuerpo: dict[str, Any] = {"chat_id": to, "text": trozo}
            if teclado and i == len(trozos) - 1:
                cuerpo["reply_markup"] = teclado
            ultimo_id = await self._llamar(token, "sendMessage", cuerpo)
        return ultimo_id

    @staticmethod
    def _teclado(botones: list[dict[str, Any]] | None) -> dict[str, Any] | None:
        """Arma el teclado inline: un boton por fila.

        Args:
            botones: Lista de dicts con `title` e `id`.

        Returns:
            `reply_markup` de Telegram, o `None` si no hay botones.
        """
        if not botones:
            return None
        return {
            "inline_keyboard": [
                [{"text": b["title"], "callback_data": str(b["id"])[:64]}] for b in botones
            ]
        }

    async def send_template(
        self, to: str, template: TemplateMessage, channel_config: dict[str, Any]
    ) -> str:
        """Telegram no tiene templates preaprobados.

        Args:
            to: Sin uso.
            template: Sin uso.
            channel_config: Sin uso.

        Raises:
            TemplateNotSupportedError: Siempre.
        """
        raise TemplateNotSupportedError("Telegram no soporta templates preaprobados")

    def get_channel_constraints(self) -> ChannelConstraints:
        """Restricciones de Telegram.

        Returns:
            4096 caracteres, sin ventana de sesion y sin listas interactivas.
        """
        return ChannelConstraints(
            max_text_length=MAX_TEXT_LENGTH,
            supported_media_types=["image", "audio", "video", "document"],
            session_window_hours=None,
            requires_template_outside_window=False,
            max_buttons=8,
            max_list_items=0,
        )

    # ─── Utilidades de la Bot API ───────────────────────────────────────────

    async def register_webhook(
        self,
        webhook_url: str,
        secret_token: str,
        channel_config: dict[str, Any],
        drop_pending_updates: bool = False,
    ) -> dict[str, Any]:
        """Registra el webhook del bot en Telegram (`setWebhook`).

        Se ejecuta a mano al configurar un entorno; no forma parte del flujo de
        mensajes.

        Args:
            webhook_url: URL publica de `POST /api/v1/webhooks/telegram/telegram`.
            secret_token: Igual a `TELEGRAM_WEBHOOK_SECRET` (1-256 caracteres
                `A-Za-z0-9_-`).
            channel_config: Debe traer `bot_token`.
            drop_pending_updates: Descartar lo acumulado mientras no habia webhook.

        Returns:
            La respuesta de Telegram (`ok`, `result`, `description`).

        Raises:
            TelegramAPIError: Si Telegram rechaza el registro.
        """
        return await self._llamar_raw(
            channel_config["bot_token"],
            "setWebhook",
            {
                "url": webhook_url,
                "secret_token": secret_token,
                # Sin esto Telegram manda tambien ediciones, altas en grupos, etc.
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": drop_pending_updates,
            },
        )

    async def get_file_url(self, file_id: str, channel_config: dict[str, Any]) -> str:
        """Resuelve un `file_id` a la URL de descarga (`getFile`).

        Para quien necesite el binario de un `telegram-file:<file_id>` (la
        transcripcion de audio). **La URL lleva el token del bot**: no se loguea
        ni se guarda.

        Args:
            file_id: Identificador del archivo, sin el prefijo `telegram-file:`.
            channel_config: Debe traer `bot_token`.

        Returns:
            URL de descarga, valida al menos una hora.

        Raises:
            TelegramAPIError: Si el archivo no existe o pesa mas de 20 MB.
        """
        token = channel_config["bot_token"]
        respuesta = await self._llamar_raw(token, "getFile", {"file_id": file_id})
        ruta = (respuesta.get("result") or {}).get("file_path")
        if not ruta:
            raise TelegramAPIError("Telegram no devolvio `file_path` (archivo demasiado grande?)")
        return f"{get_settings().TELEGRAM_API_BASE_URL}/file/bot{token}/{ruta}"

    @staticmethod
    def file_id_de(media_url: str | None) -> str | None:
        """Extrae el `file_id` de un `media_url` de Telegram.

        Args:
            media_url: Valor de `NormalizedMessage.media_url`.

        Returns:
            El `file_id`, o `None` si no es un archivo de Telegram.
        """
        if media_url and media_url.startswith(TELEGRAM_FILE_PREFIX):
            return media_url[len(TELEGRAM_FILE_PREFIX) :]
        return None

    async def _llamar(self, token: str, metodo: str, cuerpo: dict[str, Any]) -> str:
        """Llama a un metodo de envio y devuelve el `message_id`.

        Args:
            token: Token del bot.
            metodo: Metodo de la Bot API.
            cuerpo: JSON de la peticion.

        Returns:
            `message_id` del mensaje enviado, como texto.
        """
        respuesta = await self._llamar_raw(token, metodo, cuerpo)
        return str((respuesta.get("result") or {}).get("message_id", ""))

    async def _llamar_raw(self, token: str, metodo: str, cuerpo: dict[str, Any]) -> dict[str, Any]:
        """Llama a la Bot API y devuelve la respuesta ya validada.

        Args:
            token: Token del bot.
            metodo: Metodo de la Bot API.
            cuerpo: JSON de la peticion.

        Returns:
            El JSON de la respuesta (con `ok: true`). `result` no siempre es un
            dict: `setWebhook` devuelve `true`.

        Raises:
            TelegramAPIError: Si Telegram responde `ok: false`, la respuesta no es
                JSON o falla la red. Ningun mensaje incluye el token.
        """
        url = f"{get_settings().TELEGRAM_API_BASE_URL}/bot{token}/{metodo}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as cliente:
                respuesta = await cliente.post(url, json=cuerpo)
        except httpx.HTTPError as exc:
            # `str(exc)` de httpx incluye la URL, y la URL lleva el token.
            raise TelegramAPIError(
                f"Fallo de red hablando con Telegram ({type(exc).__name__}): "
                f"{_sin_token(str(exc), token)}"
            ) from None

        try:
            datos = respuesta.json()
        except ValueError:
            raise TelegramAPIError(
                f"Telegram respondio {respuesta.status_code} sin JSON en {metodo}",
                error_code=respuesta.status_code,
            ) from None

        if not datos.get("ok"):
            raise TelegramAPIError(
                f"Telegram rechazo {metodo}: {_sin_token(str(datos.get('description')), token)}",
                error_code=datos.get("error_code", respuesta.status_code),
                retry_after=(datos.get("parameters") or {}).get("retry_after"),
            )
        return dict(datos)
