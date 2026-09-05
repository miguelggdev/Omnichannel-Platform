# Sprint 9 — Canales Adicionales (Fase 2)

## Objetivo

Agregar Telegram, Webchat (WebSocket) y Email como canales de comunicación, además de transcripción de audio con Whisper API. Todos los canales deben implementar la misma `MessagingProvider` ABC y ser intercambiables en el flujo del grafo LangGraph.

> **Nota:** Instagram DM y Facebook Messenger fueron promovidos a canales MVP y se implementan en Sprint 4 junto a WhatsApp (YCloud). Ver `specs/sprint-04-webhooks.md` para su especificación completa.

## Prerequisitos

- Sprint 8 completado (MVP operativo en producción).
- `MessagingProvider` ABC implementada en Sprint 4 con los 5 métodos obligatorios: `parse_webhook`, `validate_signature`, `send_message`, `send_template`, `get_channel_constraints`.
- `YCloudProvider` (WhatsApp) y `MetaProvider` (Instagram + Facebook) funcionales como referencia de implementación.
- `ProviderFactory` existente (Sprint 4) con registro de `YCloudProvider` y `MetaProvider`.
- Cuenta de bot de Telegram con token de BotFather.
- Cuenta de SendGrid o Mailgun para email inbound parse (o servidor IMAP propio).
- Whisper API key (OpenAI) para transcripción de audio.

## Archivos a Crear/Modificar

| Archivo | Acción | Descripción |
|---|---|---|
| `app/services/messaging/telegram.py` | Crear | TelegramProvider — implementación completa |
| `app/services/messaging/webchat.py` | Crear | WebchatProvider — WebSocket bidireccional |
| `app/services/messaging/email_provider.py` | Crear | EmailProvider — SMTP/IMAP |
| `app/services/messaging/factory.py` | Modificar | Registrar nuevos proveedores (Telegram, Webchat, Email) |
| `app/api/v1/webhooks.py` | Modificar | Agregar endpoints de webhook para Telegram y Email |
| `app/api/v1/webchat.py` | Crear | WebSocket endpoint para webchat |
| `app/tasks/audio_transcription.py` | Crear | Task Celery para Whisper API |
| `app/schemas/webchat.py` | Crear | Schemas Pydantic para mensajes WebSocket |
| `tests/unit/test_telegram_provider.py` | Crear | Tests unitarios TelegramProvider |
| `tests/unit/test_webchat_provider.py` | Crear | Tests unitarios WebchatProvider |
| `tests/unit/test_email_provider.py` | Crear | Tests unitarios EmailProvider |
| `tests/unit/test_audio_transcription.py` | Crear | Tests de transcripción |
| `tests/integration/test_multichannel.py` | Crear | Test de flujo multichannel |

> **Nota:** `app/services/messaging/meta.py` y `tests/unit/test_meta_provider.py` se crean en Sprint 4 (MVP).

## Tareas Detalladas

### 1. TelegramProvider — `app/services/messaging/telegram.py`

**1.1 Estructura de la clase**

```python
from app.services.messaging.base import MessagingProvider, ChannelConstraints, NormalizedMessage
from app.core.config import settings
import httpx

class TelegramProvider(MessagingProvider):
    """Proveedor de mensajería para Telegram Bot API."""

    BASE_URL = "https://api.telegram.org/bot{token}"

    def __init__(self, provider_config: dict):
        self.token = provider_config["bot_token"]
        self.api_url = self.BASE_URL.format(token=self.token)

    async def parse_webhook(self, payload: dict, headers: dict) -> NormalizedMessage:
        ...

    async def validate_signature(self, payload: bytes, headers: dict, secret: str) -> bool:
        ...

    async def send_message(self, recipient_id: str, content: str, **kwargs) -> dict:
        ...

    async def send_template(self, recipient_id: str, template_name: str, params: dict) -> dict:
        ...

    def get_channel_constraints(self) -> ChannelConstraints:
        ...
```

**1.2 `parse_webhook` — Parsear Update de Telegram**

- Extraer del Update de Telegram:
  - `update.message.text` → contenido de texto
  - `update.message.photo` → última foto (la de mayor resolución, `photo[-1].file_id`)
  - `update.message.voice` / `update.message.audio` → audio (obtener file_path via `getFile`)
  - `update.message.video` → video
  - `update.message.document` → documento
  - `update.message.location` → latitud/longitud
  - `update.message.contact` → contacto compartido
- `sender_id` = `update.message.from.id` (string)
- `message_id` = `update.message.message_id` (string)
- `timestamp` = `update.message.date` (Unix timestamp)
- Para archivos: usar `getFile` API para obtener URL de descarga: `https://api.telegram.org/file/bot{token}/{file_path}`

**1.3 `validate_signature` — Verificación de webhook**

Telegram no firma webhooks con HMAC estándar. La verificación se hace de dos maneras:
- **IP whitelist**: Verificar que el request viene de IPs de Telegram (149.154.160.0/20, 91.108.4.0/22).
- **Secret token**: Si se configuró un `secret_token` al registrar el webhook (`setWebhook`), Telegram envía el header `X-Telegram-Bot-Api-Secret-Token`. Comparar con el secret almacenado.

```python
async def validate_signature(self, payload: bytes, headers: dict, secret: str) -> bool:
    telegram_secret = headers.get("x-telegram-bot-api-secret-token", "")
    return hmac.compare_digest(telegram_secret, secret)
```

**1.4 `send_message` — Enviar mensajes**

```python
async def send_message(self, recipient_id: str, content: str, **kwargs) -> dict:
    message_type = kwargs.get("type", "text")

    async with httpx.AsyncClient() as client:
        if message_type == "text":
            response = await client.post(
                f"{self.api_url}/sendMessage",
                json={
                    "chat_id": recipient_id,
                    "text": content,
                    "parse_mode": "HTML",  # Soporta HTML básico
                }
            )
        elif message_type == "image":
            response = await client.post(
                f"{self.api_url}/sendPhoto",
                json={"chat_id": recipient_id, "photo": kwargs["media_url"], "caption": content}
            )
        elif message_type == "document":
            response = await client.post(
                f"{self.api_url}/sendDocument",
                json={"chat_id": recipient_id, "document": kwargs["media_url"], "caption": content}
            )
        # ... más tipos

    result = response.json()
    if not result.get("ok"):
        raise ProviderError(f"Telegram API error: {result.get('description')}")

    return {"provider_message_id": str(result["result"]["message_id"])}
```

**1.5 Botones inline y reply keyboards**

```python
async def send_message_with_buttons(self, recipient_id: str, content: str, buttons: list) -> dict:
    """Enviar mensaje con botones inline."""
    inline_keyboard = [[{"text": btn["title"], "callback_data": btn["id"]}] for btn in buttons]

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{self.api_url}/sendMessage",
            json={
                "chat_id": recipient_id,
                "text": content,
                "reply_markup": {"inline_keyboard": inline_keyboard},
            }
        )
    return {"provider_message_id": str(response.json()["result"]["message_id"])}
```

**1.6 `get_channel_constraints`**

```python
def get_channel_constraints(self) -> ChannelConstraints:
    return ChannelConstraints(
        messaging_window=None,  # Sin restricción de ventana
        max_message_length=4096,
        supports_templates=False,
        supports_buttons=True,  # Inline keyboards
        supports_media=True,
        supports_location=True,
        rate_limit_per_second=30,  # Telegram rate limit ~30 msg/s
    )
```

**1.7 Registro del webhook**

Agregar script/endpoint para registrar el webhook de Telegram:
```python
async def register_webhook(self, webhook_url: str, secret_token: str):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{self.api_url}/setWebhook",
            json={
                "url": webhook_url,
                "secret_token": secret_token,
                "allowed_updates": ["message", "callback_query"],
            }
        )
    return response.json()
```

### 2. WebchatProvider — `app/services/messaging/webchat.py`

> **Nota:** La sección de MetaProvider (Instagram DM + Facebook Messenger) fue movida a Sprint 4 como canal MVP.
> Ver `specs/sprint-04-webhooks.md` sección 4 para la especificación completa.

**2.1 WebSocket endpoint — `app/api/v1/webchat.py`**

```python
from fastapi import WebSocket, WebSocketDisconnect
from app.services.messaging.webchat import WebchatConnectionManager

manager = WebchatConnectionManager()

@router.websocket("/webchat/{channel_token}")
async def webchat_endpoint(websocket: WebSocket, channel_token: str):
    """
    WebSocket bidireccional para webchat.
    Autenticación por channel_token (no JWT de usuario).
    """
    # Validar channel_token → obtener channel_config y client_id
    channel_config = await validate_channel_token(channel_token)
    if not channel_config:
        await websocket.close(code=4001, reason="Invalid channel token")
        return

    # Generar o recuperar session_id
    session_id = websocket.query_params.get("session_id")
    last_message_id = websocket.query_params.get("last_message_id")

    await manager.connect(websocket, channel_config.client_id, session_id)

    try:
        # Si tiene last_message_id, enviar mensajes perdidos (reconexión)
        if last_message_id:
            missed_messages = await get_messages_after(session_id, last_message_id)
            for msg in missed_messages:
                await websocket.send_json(msg)

        while True:
            data = await websocket.receive_json()
            # Validar schema del mensaje
            message = WebchatMessage(**data)

            # Procesar como cualquier otro canal
            await process_incoming_message.delay(
                channel="webchat",
                client_id=str(channel_config.client_id),
                payload=data,
                channel_config_id=str(channel_config.id),
            )

    except WebSocketDisconnect:
        manager.disconnect(websocket, session_id)
    except Exception as e:
        logger.error(f"Webchat error: {e}")
        await websocket.close(code=4000, reason="Internal error")
        manager.disconnect(websocket, session_id)
```

**2.2 Connection Manager**

```python
class WebchatConnectionManager:
    """Gestionar conexiones WebSocket activas."""

    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}  # session_id → websocket

    async def connect(self, websocket: WebSocket, client_id: UUID, session_id: str = None):
        await websocket.accept()
        if not session_id:
            session_id = str(uuid4())
        self.active_connections[session_id] = websocket
        # Enviar session_id al cliente
        await websocket.send_json({"type": "connected", "session_id": session_id})

    def disconnect(self, websocket: WebSocket, session_id: str):
        self.active_connections.pop(session_id, None)

    async def send_to_session(self, session_id: str, message: dict):
        """Enviar mensaje a una sesión específica."""
        websocket = self.active_connections.get(session_id)
        if websocket:
            try:
                await websocket.send_json(message)
            except Exception:
                self.disconnect(websocket, session_id)
```

**2.3 WebchatProvider**

```python
class WebchatProvider(MessagingProvider):
    """Proveedor para webchat via WebSocket."""

    def __init__(self, provider_config: dict, connection_manager: WebchatConnectionManager):
        self.manager = connection_manager

    async def parse_webhook(self, payload: dict, headers: dict) -> NormalizedMessage:
        """En webchat, el payload viene del WebSocket, no de un webhook HTTP."""
        return NormalizedMessage(
            sender_id=payload["session_id"],
            message_id=payload.get("message_id", str(uuid4())),
            content=payload.get("text", ""),
            media_type=payload.get("media_type"),
            media_url=payload.get("media_url"),
            timestamp=datetime.utcnow().isoformat(),
            channel="webchat",
            raw_payload=payload,
        )

    async def validate_signature(self, payload: bytes, headers: dict, secret: str) -> bool:
        """Webchat no necesita validación de firma (WebSocket ya autenticado)."""
        return True

    async def send_message(self, recipient_id: str, content: str, **kwargs) -> dict:
        """Enviar mensaje al WebSocket del cliente."""
        message = {
            "type": "message",
            "message_id": str(uuid4()),
            "text": content,
            "timestamp": datetime.utcnow().isoformat(),
        }
        if kwargs.get("media_url"):
            message["media_url"] = kwargs["media_url"]
            message["media_type"] = kwargs.get("media_type", "image")

        await self.manager.send_to_session(recipient_id, message)
        return {"provider_message_id": message["message_id"]}

    def get_channel_constraints(self) -> ChannelConstraints:
        return ChannelConstraints(
            messaging_window=None,  # SIN restricción de ventana
            max_message_length=10000,
            supports_templates=False,
            supports_buttons=True,
            supports_media=True,
            supports_location=False,
            rate_limit_per_second=None,  # Sin rate limit del proveedor
        )
```

**2.4 Schema WebSocket**

```python
# app/schemas/webchat.py
from pydantic import BaseModel
from typing import Optional

class WebchatMessage(BaseModel):
    type: str = "message"  # "message", "typing", "read_receipt"
    session_id: str
    message_id: Optional[str] = None
    text: Optional[str] = None
    media_type: Optional[str] = None  # "image", "file"
    media_url: Optional[str] = None
    file_name: Optional[str] = None

class WebchatResponse(BaseModel):
    type: str  # "message", "connected", "typing", "error"
    message_id: Optional[str] = None
    text: Optional[str] = None
    timestamp: str
    session_id: Optional[str] = None
```

### 3. EmailProvider — `app/services/messaging/email_provider.py`

**3.1 Estructura**

```python
class EmailProvider(MessagingProvider):
    """Proveedor de email con soporte SMTP (envío) e inbound parse (recepción)."""

    def __init__(self, provider_config: dict):
        self.smtp_host = provider_config["smtp_host"]
        self.smtp_port = provider_config.get("smtp_port", 587)
        self.smtp_user = provider_config["smtp_user"]
        self.smtp_password = provider_config["smtp_password"]
        self.from_email = provider_config["from_email"]
        self.from_name = provider_config.get("from_name", "Soporte")
        self.inbound_method = provider_config.get("inbound_method", "webhook")  # "webhook" o "imap"
```

**3.2 Recepción — Inbound Parse (webhook de SendGrid/Mailgun)**

```python
async def parse_webhook(self, payload: dict, headers: dict) -> NormalizedMessage:
    """
    Parsear email entrante desde SendGrid/Mailgun inbound parse.
    SendGrid: multipart form con campos 'from', 'to', 'subject', 'text', 'html'
    Mailgun: similar con campos 'sender', 'recipient', 'subject', 'body-plain'
    """
    # Normalizar campos según proveedor de inbound parse
    sender_email = self._extract_sender_email(payload)
    subject = payload.get("subject", "")
    body = payload.get("text", payload.get("body-plain", ""))
    html_body = payload.get("html", payload.get("body-html", ""))
    message_id = payload.get("Message-Id", payload.get("message-id", str(uuid4())))

    # Threading: extraer In-Reply-To y References
    in_reply_to = payload.get("In-Reply-To", payload.get("in-reply-to", ""))
    references = payload.get("References", payload.get("references", ""))

    return NormalizedMessage(
        sender_id=sender_email,
        message_id=message_id,
        content=body,
        metadata={
            "subject": subject,
            "html_body": html_body,
            "in_reply_to": in_reply_to,
            "references": references,
        },
        timestamp=datetime.utcnow().isoformat(),
        channel="email",
        raw_payload=payload,
    )
```

**3.3 Threading de email**

```python
async def _find_existing_conversation(self, db, client_id: UUID, message: NormalizedMessage) -> Optional[Conversation]:
    """
    Buscar conversación existente por threading de email.
    Prioridad:
    1. In-Reply-To header → buscar mensaje con ese message_id
    2. References header → buscar cualquier mensaje referenciado
    3. Subject (sin Re:/Fwd:) + sender → última conversación en 7 días
    """
    in_reply_to = message.metadata.get("in_reply_to")
    if in_reply_to:
        # Buscar mensaje con provider_message_id == in_reply_to
        existing_msg = await db.execute(
            select(Message).where(
                Message.client_id == client_id,
                Message.provider_message_id == in_reply_to,
            )
        )
        msg = existing_msg.scalar_one_or_none()
        if msg:
            return await db.get(Conversation, msg.conversation_id)

    # Fallback: buscar por subject limpio
    clean_subject = re.sub(r"^(Re|Fwd|RE|FW):\s*", "", message.metadata.get("subject", ""))
    # ... buscar conversación reciente con mismo subject y sender
```

**3.4 Envío — SMTP con template HTML**

```python
async def send_message(self, recipient_id: str, content: str, **kwargs) -> dict:
    """Enviar email via SMTP."""
    import aiosmtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{self.from_name} <{self.from_email}>"
    msg["To"] = recipient_id
    msg["Subject"] = kwargs.get("subject", "Re: Tu consulta")

    # Threading headers
    if kwargs.get("in_reply_to"):
        msg["In-Reply-To"] = kwargs["in_reply_to"]
        msg["References"] = kwargs.get("references", kwargs["in_reply_to"])

    # Cuerpo: texto plano + HTML
    text_part = MIMEText(content, "plain", "utf-8")
    html_content = self._render_html_template(content, kwargs.get("template_data", {}))
    html_part = MIMEText(html_content, "html", "utf-8")

    msg.attach(text_part)
    msg.attach(html_part)

    # Generar Message-ID propio
    message_id = f"<{uuid4().hex}@{self.from_email.split('@')[1]}>"
    msg["Message-ID"] = message_id

    # Enviar
    await aiosmtplib.send(
        msg,
        hostname=self.smtp_host,
        port=self.smtp_port,
        username=self.smtp_user,
        password=self.smtp_password,
        use_tls=True,
    )

    return {"provider_message_id": message_id}
```

**3.5 Constraints**

```python
def get_channel_constraints(self) -> ChannelConstraints:
    return ChannelConstraints(
        messaging_window=None,  # Sin restricción
        max_message_length=50000,  # Emails pueden ser largos
        supports_templates=True,  # Templates HTML
        supports_buttons=False,  # No hay botones nativos en email
        supports_media=True,  # Attachments
        supports_location=False,
        rate_limit_per_second=10,  # Depende del proveedor SMTP
    )
```

### 4. Audio Transcription — `app/tasks/audio_transcription.py`

**4.1 Task Celery**

```python
# app/tasks/audio_transcription.py
import httpx
from app.core.celery_app import celery_app
from app.core.config import settings

@celery_app.task(queue="media", bind=True, max_retries=3)
async def transcribe_audio(self, message_id: str, media_url: str, provider: str):
    """
    Descargar audio y transcribir con Whisper API.
    1. Descargar media desde el proveedor (WhatsApp, Telegram, etc.)
    2. Enviar a Whisper API
    3. Actualizar message.content con la transcripción
    4. Mantener media_url original
    """
    try:
        # 1. Descargar audio
        audio_data = await download_media(media_url, provider)

        # 2. Transcribir con Whisper
        transcript = await whisper_transcribe(audio_data)

        # 3. Actualizar mensaje
        async with get_db_session() as db:
            message = await db.get(Message, message_id)
            if message:
                message.content = transcript
                message.metadata = {
                    **(message.metadata or {}),
                    "original_type": "audio",
                    "transcription_model": "whisper-1",
                    "transcription_language": transcript_language,
                }
                await db.commit()

        # 4. Continuar el pipeline: enviar a process_message
        from app.tasks.message_processing import process_transcribed_message
        process_transcribed_message.delay(message_id)

    except Exception as exc:
        self.retry(exc=exc, countdown=5 * (self.request.retries + 1))


async def download_media(media_url: str, provider: str) -> bytes:
    """Descargar media desde el proveedor."""
    async with httpx.AsyncClient() as client:
        if provider == "whatsapp":
            # WhatsApp requiere Authorization header
            response = await client.get(
                media_url,
                headers={"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"},
            )
        elif provider == "telegram":
            # Telegram: URL directa
            response = await client.get(media_url)
        else:
            response = await client.get(media_url)

        response.raise_for_status()
        return response.content


async def whisper_transcribe(audio_data: bytes) -> str:
    """Transcribir audio con OpenAI Whisper API."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            files={"file": ("audio.ogg", audio_data, "audio/ogg")},
            data={"model": "whisper-1", "language": "es"},  # Default español
            timeout=60.0,
        )
        response.raise_for_status()
        return response.json()["text"]
```

**4.2 Integración en el pipeline**

- En el handler de webhook, después de normalización:
  - Si `message.media_type == "audio"`: enviar a `transcribe_audio.delay()` en vez de `process_incoming_message.delay()`
  - `transcribe_audio` actualiza el mensaje y luego llama a `process_transcribed_message`
  - El resto del pipeline (intent routing, RAG, etc.) recibe texto normal

**4.3 Batch processing (optimización)**

```python
@celery_app.task(queue="media")
async def batch_transcribe(message_ids: list[str]):
    """
    Transcribir múltiples audios en batch.
    Útil para procesar audios acumulados durante mantenimiento.
    """
    for message_id in message_ids:
        message = await get_message(message_id)
        if message and message.media_url:
            await transcribe_audio.delay(
                message_id=str(message.id),
                media_url=message.media_url,
                provider=message.channel,
            )
```

### 5. Actualizar Factory — `app/services/messaging/factory.py`

Agregar los nuevos proveedores de Fase 2 al factory existente (que ya incluye `ycloud` y `meta` del Sprint 4):

```python
# Agregar a app/services/messaging/factory.py (existente del Sprint 4)
from app.services.messaging.telegram import TelegramProvider
from app.services.messaging.webchat import WebchatProvider
from app.services.messaging.email_provider import EmailProvider

# Agregar al dict _PROVIDERS:
_PROVIDERS.update({
    "telegram": TelegramProvider,
    "webchat": WebchatProvider,
    "email": EmailProvider,
})

# O alternativamente, refactorizar a clase ProviderFactory con registro dinámico:

class ProviderFactory:
    """Factory para crear instancias de MessagingProvider."""

    _providers: dict[str, type[MessagingProvider]] = {
        # Sprint 4 (MVP)
        "ycloud": YCloudProvider,
        "meta": MetaProvider,       # Instagram DM + Facebook Messenger
        # Sprint 9 (Fase 2)
        "telegram": TelegramProvider,
        "webchat": WebchatProvider,
        "email": EmailProvider,
    }

    @classmethod
    def register(cls, channel: str, provider_class: type[MessagingProvider]):
        """Registro dinámico de proveedores."""
        cls._providers[channel] = provider_class

    @classmethod
    def get_provider(cls, channel: str, provider_config: dict, **kwargs) -> MessagingProvider:
        """
        Obtener instancia de provider para un canal.
        provider_config viene de channel_configs.provider_config (JSONB).
        """
        provider_class = cls._providers.get(channel)
        if not provider_class:
            raise ValueError(f"Canal no soportado: {channel}")

        # WebchatProvider necesita el connection_manager
        if channel == "webchat":
            return provider_class(provider_config, kwargs.get("connection_manager"))

        return provider_class(provider_config)

    @classmethod
    def get_supported_channels(cls) -> list[str]:
        """Listar canales soportados."""
        return list(cls._providers.keys())
```

### 6. Endpoints de Webhook — Actualizar `app/api/v1/webhooks.py`

> **Nota:** Los endpoints de Meta (Instagram/Facebook) ya están definidos en Sprint 4.

```python
# Agregar a app/api/v1/webhooks.py

@router.post("/webhooks/telegram/{channel_config_id}")
async def telegram_webhook(
    channel_config_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Webhook para Telegram Bot API."""
    payload = await request.json()
    raw_body = await request.body()
    headers = dict(request.headers)

    channel_config = await get_channel_config(channel_config_id)
    provider = ProviderFactory.get_provider("telegram", channel_config.provider_config)

    if not await provider.validate_signature(raw_body, headers, channel_config.webhook_secret):
        raise HTTPException(403, "Invalid signature")

    message = await provider.parse_webhook(payload, headers)

    process_incoming_message.delay(
        channel="telegram",
        client_id=str(channel_config.client_id),
        payload=message.dict(),
        channel_config_id=str(channel_config_id),
    )

    return {"status": "ok"}

@router.post("/webhooks/email/{channel_config_id}")
async def email_inbound_webhook(
    channel_config_id: UUID,
    request: Request,
):
    """Webhook para email inbound parse (SendGrid/Mailgun)."""
    # SendGrid envía multipart form
    form = await request.form()
    payload = {key: form[key] for key in form}

    channel_config = await get_channel_config(channel_config_id)
    provider = ProviderFactory.get_provider("email", channel_config.provider_config)

    message = await provider.parse_webhook(payload, dict(request.headers))

    process_incoming_message.delay(
        channel="email",
        client_id=str(channel_config.client_id),
        payload=message.dict(),
        channel_config_id=str(channel_config_id),
    )

    return {"status": "ok"}
```

### 7. Tests

**7.1 Test TelegramProvider**

```python
# tests/unit/test_telegram_provider.py
import pytest
from app.services.messaging.telegram import TelegramProvider

class TestTelegramProvider:
    @pytest.fixture
    def provider(self):
        return TelegramProvider({"bot_token": "test-token"})

    async def test_parse_text_message(self, provider):
        payload = {
            "update_id": 123,
            "message": {
                "message_id": 456,
                "from": {"id": 789, "first_name": "Test"},
                "chat": {"id": 789},
                "date": 1700000000,
                "text": "Hola",
            }
        }
        result = await provider.parse_webhook(payload, {})
        assert result.sender_id == "789"
        assert result.content == "Hola"
        assert result.channel == "telegram"

    async def test_parse_photo_message(self, provider):
        """Verificar que se toma la foto de mayor resolución."""
        # ...

    async def test_validate_signature(self, provider):
        headers = {"x-telegram-bot-api-secret-token": "my-secret"}
        assert await provider.validate_signature(b"", headers, "my-secret")
        assert not await provider.validate_signature(b"", headers, "wrong-secret")

    async def test_channel_constraints(self, provider):
        constraints = provider.get_channel_constraints()
        assert constraints.messaging_window is None  # Sin ventana
        assert constraints.max_message_length == 4096
```

**7.2 Test multichannel**

```python
# tests/integration/test_multichannel.py
@pytest.mark.integration
class TestMultichannel:
    async def test_same_contact_across_channels(self, db, test_tenant):
        """Mismo teléfono en WhatsApp y Telegram → 1 contacto."""
        # Crear mensaje desde WhatsApp con teléfono +573001234567
        # Crear mensaje desde Telegram con teléfono +573001234567
        # Verificar: solo 1 contacto, 2 contact_identifiers, 2 conversaciones
        pass

    async def test_telegram_full_flow(self, async_client, test_tenant):
        """Mensaje Telegram → grafo completo → respuesta Telegram."""
        pass

    async def test_audio_transcription_flow(self, async_client, test_tenant):
        """Audio WhatsApp → Whisper → texto → grafo → respuesta."""
        pass
```

## Criterios de Aceptación

| # | Criterio | Verificación |
|---|---|---|
| 1 | Telegram: mensaje enviado y recibido | Enviar texto al bot → recibir respuesta del agente |
| 2 | Webchat: WebSocket bidireccional | Conectar WS → enviar mensaje → recibir respuesta |
| 3 | Email: threading correcto | Enviar email → respuesta como reply en mismo hilo |
| 4 | Audio → texto | Enviar audio en WhatsApp → transcripción → respuesta como texto |
| 5 | Contact unification cross-canal | Mismo teléfono en 2+ canales → 1 contacto |
| 6 | Reconexión webchat | WebSocket se desconecta → reconecta → recibe mensajes perdidos |
| 7 | Factory registra todos los canales | `ProviderFactory.get_supported_channels()` devuelve los 5 proveedores (ycloud, meta, telegram, webchat, email) |
| 8 | Cada provider pasa la misma suite de tests base | ABC contract test para los 5 métodos |

> **Nota:** Los criterios de Instagram DM y Facebook Messenger están en Sprint 4.

## Notas Técnicas

- **ABC de 5 métodos**: Todo provider DEBE implementar `parse_webhook`, `validate_signature`, `send_message`, `send_template`, `get_channel_constraints`. Si un canal no soporta templates, `send_template` debe lanzar `TemplateNotSupportedError`.
- **Ventanas de mensajes**: WhatsApp, Instagram y Facebook (ventana de 24h) ya están implementados en Sprint 4. Telegram y Webchat NO tienen ventana. Email NO tiene ventana. La lógica de ventana se maneja en `get_channel_constraints()` y se verifica ANTES de enviar en el nodo `respond` del grafo.
- **Webchat NO tiene restricciones de ventana**: Esto es una ventaja clave — el agente puede iniciar conversación en cualquier momento.
- **Email threading es CRÍTICO**: Un email sin `In-Reply-To` correcto creará una conversación nueva en el mail client del usuario. Siempre incluir `In-Reply-To` y `References` en las respuestas.
- **Audio batch**: Considerar un endpoint admin para re-procesar audios fallidos en batch.
- **Rate limits**: Telegram tiene 30 msg/s por bot. WhatsApp y Meta (Sprint 4) tienen sus propios rate limits. Implementar throttling en el envío.

## Dependencias

| Dependencia | Versión | Propósito |
|---|---|---|
| httpx | >=0.27.0 | HTTP client async para APIs de proveedores |
| aiosmtplib | >=3.0.0 | Envío de email async via SMTP |
| websockets | >=12.0 | Soporte WebSocket (incluido en FastAPI/Starlette) |
| openai | >=1.12.0 | Whisper API para transcripción |
| python-multipart | >=0.0.9 | Parsing de forms multipart (email inbound) |
