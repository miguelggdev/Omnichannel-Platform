# Sprint 4 — Webhook Receiver & MessagingProvider

## Objetivo
Recepcion idempotente de mensajes entrantes desde cualquier canal, abstraccion de proveedor de mensajeria con patron ABC para desacoplamiento, y procesamiento asincrono via Celery. Al finalizar este sprint, mensajes de WhatsApp (via YCloud), Facebook Messenger e Instagram DM (via Meta Graph API) llegan, se deduplican, se normalizan y se almacenan correctamente.

## Prerequisitos
- Sprint 3 completado: FastAPI core, auth, middleware, modelos SQLAlchemy
- Celery workers funcionando con sus 5 colas (verificar con `celery inspect active_queues`)
- Redis accesible para cache de deduplicacion
- Credenciales de YCloud configuradas en .env
- App de Meta configurada con permisos de Instagram Messaging API y Facebook Messenger
- Page Access Token y App Secret de Meta configurados en .env

## Archivos a Crear
```
app/
  services/
    messaging/
      __init__.py
      base.py                    # MessagingProvider ABC + tipos base
      ycloud.py                  # YCloudProvider implementation
      meta.py                    # MetaProvider — Instagram DM + Facebook Messenger
      factory.py                 # Provider factory
  api/
    v1/
      webhooks.py                # Endpoint publico de webhooks (YCloud + Meta)
  schemas/
    message.py                   # NormalizedMessage, ChannelConstraints
  tasks/
    webhook_processor.py         # Worker de procesamiento
    __init__.py                  # Registro de tasks
  services/
    contact_resolver.py          # Resolver/crear contactos por identifier
tests/
  unit/
    test_webhook_dedup.py
    test_messaging_provider.py
    test_meta_provider.py        # Tests unitarios MetaProvider
    test_contact_resolver.py
  integration/
    test_webhook_flow.py
  fixtures/
    ycloud_payloads.py           # Payloads de ejemplo de YCloud
    meta_payloads.py             # Payloads de ejemplo de Meta (Instagram + Facebook)
```

## Tareas Detalladas

### 1. MessagingProvider ABC (`app/services/messaging/base.py`)

Definir la interfaz abstracta que desacopla la aplicacion de cualquier proveedor de mensajeria especifico:

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class ChannelConstraints:
    """Restricciones del canal de mensajeria."""
    max_text_length: int              # e.g., 4096 para WhatsApp
    supported_media_types: list[str]  # e.g., ["image", "audio", "video", "document"]
    session_window_hours: int | None  # e.g., 24 para WhatsApp (None = sin limite)
    requires_template_outside_window: bool
    max_buttons: int                  # Para mensajes interactivos
    max_list_items: int               # Para listas interactivas

@dataclass
class MessageContent:
    """Contenido de mensaje para envio."""
    text: str | None = None
    media_url: str | None = None
    media_type: str | None = None
    buttons: list[dict] | None = None
    caption: str | None = None

@dataclass
class TemplateMessage:
    """Mensaje de template (WhatsApp HSM)."""
    template_name: str
    language: str
    components: list[dict]  # header, body, buttons con parametros

class MessagingProvider(ABC):
    """
    Interfaz abstracta para proveedores de mensajeria.

    Patron: cada canal/proveedor implementa esta interfaz.
    La aplicacion NUNCA interactua directamente con APIs de proveedores.
    """

    @abstractmethod
    async def parse_webhook(self, raw_payload: dict) -> "NormalizedMessage":
        """
        Parsea el payload crudo del webhook y lo normaliza.
        Cada proveedor tiene formato diferente; esta funcion los unifica.
        """
        ...

    @abstractmethod
    async def validate_signature(self, payload: bytes, signature: str, secret: str) -> bool:
        """
        Valida la firma del webhook para autenticar el origen.
        Tipicamente HMAC-SHA256 con el secret del tenant.
        """
        ...

    @abstractmethod
    async def send_message(self, to: str, content: MessageContent, channel_config: dict) -> str:
        """
        Envia un mensaje al destinatario.
        Retorna el external_message_id del mensaje enviado.
        """
        ...

    @abstractmethod
    async def send_template(self, to: str, template: TemplateMessage, channel_config: dict) -> str:
        """
        Envia un mensaje de template (necesario fuera de la ventana de sesion en WhatsApp).
        Retorna el external_message_id.
        """
        ...

    @abstractmethod
    def get_channel_constraints(self) -> ChannelConstraints:
        """Retorna las restricciones del canal."""
        ...
```

### 2. NormalizedMessage Schema (`app/schemas/message.py`)

```python
from pydantic import BaseModel
from datetime import datetime
from enum import Enum
from uuid import UUID

class ChannelEnum(str, Enum):
    whatsapp = "whatsapp"
    telegram = "telegram"
    instagram = "instagram"
    webchat = "webchat"
    email = "email"
    phone = "phone"
    facebook = "facebook"

class MessageTypeEnum(str, Enum):
    text = "text"
    image = "image"
    audio = "audio"
    video = "video"
    document = "document"
    location = "location"
    template = "template"
    interactive = "interactive"

class NormalizedMessage(BaseModel):
    """
    Mensaje normalizado independiente del proveedor.
    Todos los webhooks entrantes se convierten a este formato.
    """
    channel: ChannelEnum
    sender_identifier: str        # telefono, email, username, etc.
    text: str | None = None
    media_url: str | None = None
    media_type: MessageTypeEnum | None = None
    timestamp: datetime
    external_message_id: str      # ID unico del proveedor
    raw_payload: dict             # Payload original para debugging

    # Campos opcionales para tipos especificos
    location: dict | None = None  # {"latitude": ..., "longitude": ...}
    interactive_response: dict | None = None  # Respuesta a botones/listas
```

### 3. YCloudProvider (`app/services/messaging/ycloud.py`)

```python
import hmac
import hashlib
import httpx

class YCloudProvider(MessagingProvider):
    """
    Implementacion para YCloud (WhatsApp Business API provider).
    Documentacion: https://docs.ycloud.com/
    """

    BASE_URL = "https://api.ycloud.com/v2"

    async def parse_webhook(self, raw_payload: dict) -> NormalizedMessage:
        """
        Extrae campos del formato de webhook de YCloud.

        Estructura esperada del payload de YCloud:
        {
            "id": "msg_xxx",
            "type": "whatsapp.inbound_message.received",
            "whatsappInboundMessage": {
                "id": "wamid.xxx",
                "from": "+5491155667788",
                "timestamp": "2024-01-01T00:00:00Z",
                "type": "text",
                "text": {"body": "Hola, necesito ayuda"},
                "image": {"url": "...", "caption": "..."},
                ...
            }
        }
        """
        msg = raw_payload.get("whatsappInboundMessage", {})

        text = None
        media_url = None
        media_type = None

        msg_type = msg.get("type", "text")

        if msg_type == "text":
            text = msg.get("text", {}).get("body")
        elif msg_type in ("image", "audio", "video", "document"):
            media_data = msg.get(msg_type, {})
            media_url = media_data.get("url") or media_data.get("link")
            media_type = MessageTypeEnum(msg_type)
            text = media_data.get("caption")
        elif msg_type == "location":
            # Manejar location como caso especial
            pass

        return NormalizedMessage(
            channel=ChannelEnum.whatsapp,
            sender_identifier=msg.get("from", ""),
            text=text,
            media_url=media_url,
            media_type=media_type,
            timestamp=datetime.fromisoformat(msg.get("timestamp", "")),
            external_message_id=msg.get("id", raw_payload.get("id", "")),
            raw_payload=raw_payload,
        )

    async def validate_signature(self, payload: bytes, signature: str, secret: str) -> bool:
        """
        Valida HMAC-SHA256 del webhook de YCloud.
        El header de firma es 'X-Ycloud-Signature'.
        """
        expected = hmac.new(
            secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def send_message(self, to: str, content: MessageContent, channel_config: dict) -> str:
        """
        Envia mensaje via YCloud WhatsApp API.
        POST https://api.ycloud.com/v2/whatsapp/messages
        """
        api_key = channel_config["api_key"]

        payload = {
            "from": channel_config["phone_number_id"],
            "to": to,
        }

        if content.text and not content.media_url:
            payload["type"] = "text"
            payload["text"] = {"body": content.text}
        elif content.media_url:
            media_type = content.media_type or "image"
            payload["type"] = media_type
            payload[media_type] = {
                "link": content.media_url,
                "caption": content.caption,
            }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.BASE_URL}/whatsapp/messages",
                json=payload,
                headers={
                    "X-API-Key": api_key,
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("whatsappMessage", {}).get("id", "")

    async def send_template(self, to: str, template: TemplateMessage, channel_config: dict) -> str:
        """
        Envia template aprobado de WhatsApp via YCloud.
        Necesario cuando la ventana de 24h ha expirado.
        """
        api_key = channel_config["api_key"]

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
                f"{self.BASE_URL}/whatsapp/messages",
                json=payload,
                headers={"X-API-Key": api_key},
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("whatsappMessage", {}).get("id", "")

    def get_channel_constraints(self) -> ChannelConstraints:
        return ChannelConstraints(
            max_text_length=4096,
            supported_media_types=["image", "audio", "video", "document"],
            session_window_hours=24,
            requires_template_outside_window=True,
            max_buttons=3,
            max_list_items=10,
        )
```

### 4. MetaProvider (`app/services/messaging/meta.py`)

Proveedor unificado para Instagram DM y Facebook Messenger via Meta Graph API. Ambos canales comparten la misma API pero tienen restricciones diferentes.

```python
import hmac
import hashlib
import httpx
from enum import Enum
from datetime import timedelta

class MetaChannel(str, Enum):
    """Sub-canal de Meta: Instagram o Facebook Messenger."""
    INSTAGRAM = "instagram"
    FACEBOOK_MESSENGER = "facebook"

class MetaProvider(MessagingProvider):
    """
    Implementacion para Instagram DM y Facebook Messenger via Meta Graph API.
    Documentacion: https://developers.facebook.com/docs/messenger-platform/
    Documentacion IG: https://developers.facebook.com/docs/instagram-messaging/
    """

    GRAPH_API_VERSION = "v18.0"
    BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

    def __init__(self, provider_config: dict):
        self.page_access_token = provider_config["page_access_token"]
        self.app_secret = provider_config["app_secret"]
        self.channel = MetaChannel(provider_config.get("channel", "facebook"))

    async def parse_webhook(self, raw_payload: dict) -> NormalizedMessage:
        """
        Parsea el payload de webhook de Meta (Instagram o Facebook Messenger).

        Estructura esperada del payload de Meta:
        {
            "object": "instagram" | "page",
            "entry": [{
                "id": "<PAGE_OR_IG_ID>",
                "time": 1700000000,
                "messaging": [{
                    "sender": {"id": "<SENDER_PSID>"},
                    "recipient": {"id": "<PAGE_ID>"},
                    "timestamp": 1700000000,
                    "message": {
                        "mid": "m_xxx",
                        "text": "Hola",
                        "attachments": [{"type": "image", "payload": {"url": "..."}}]
                    }
                }]
            }]
        }
        """
        entry = raw_payload.get("entry", [{}])[0]
        messaging = entry.get("messaging", [{}])[0]
        sender_id = messaging.get("sender", {}).get("id", "")
        message = messaging.get("message", {})

        text = message.get("text")
        media_url = None
        media_type = None

        # Extraer attachments si existen
        attachments = message.get("attachments", [])
        if attachments:
            attachment = attachments[0]
            att_type = attachment.get("type", "")
            if att_type in ("image", "audio", "video", "file"):
                media_url = attachment.get("payload", {}).get("url")
                media_type = MessageTypeEnum(att_type if att_type != "file" else "document")

        # Detectar quick_reply
        quick_reply = message.get("quick_reply")
        interactive_response = None
        if quick_reply:
            interactive_response = {"type": "quick_reply", "payload": quick_reply.get("payload")}

        # Determinar canal
        channel = ChannelEnum.instagram if self.channel == MetaChannel.INSTAGRAM else ChannelEnum.facebook

        return NormalizedMessage(
            channel=channel,
            sender_identifier=sender_id,
            text=text,
            media_url=media_url,
            media_type=media_type,
            timestamp=datetime.fromtimestamp(
                messaging.get("timestamp", 0) / 1000, tz=timezone.utc
            ),
            external_message_id=message.get("mid", ""),
            raw_payload=raw_payload,
            interactive_response=interactive_response,
        )

    async def validate_signature(self, payload: bytes, signature: str, secret: str) -> bool:
        """
        Valida x-hub-signature-256 de Meta.
        El header de firma es 'x-hub-signature-256' con formato 'sha256=<hex_digest>'.
        """
        if not signature.startswith("sha256="):
            return False
        expected = hmac.new(
            secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(signature[7:], expected)

    async def send_message(self, to: str, content: MessageContent, channel_config: dict) -> str:
        """
        Envia mensaje via Meta Send API (funciona para Instagram y Facebook Messenger).
        POST https://graph.facebook.com/v18.0/me/messages
        """
        message_payload: dict = {"recipient": {"id": to}}

        if content.text and not content.media_url:
            message_payload["message"] = {"text": content.text}
        elif content.media_url:
            media_type = content.media_type or "image"
            message_payload["message"] = {
                "attachment": {
                    "type": media_type,
                    "payload": {"url": content.media_url, "is_reusable": True},
                }
            }
            if content.caption:
                # Meta no soporta caption en attachments, enviar como mensaje separado
                pass

        if content.buttons:
            # Quick replies (soportado por ambos canales)
            message_payload["message"]["quick_replies"] = [
                {"content_type": "text", "title": btn.get("title", ""), "payload": btn.get("id", "")}
                for btn in content.buttons
            ]

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.BASE_URL}/me/messages",
                params={"access_token": channel_config["page_access_token"]},
                json=message_payload,
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("message_id", "")

    async def send_template(self, to: str, template: TemplateMessage, channel_config: dict) -> str:
        """
        Envia mensaje de template.
        - Facebook Messenger: soporta templates de boton y genericos.
        - Instagram: NO soporta templates. Lanza TemplateNotSupportedError.
        """
        if self.channel == MetaChannel.INSTAGRAM:
            raise NotImplementedError(
                "Instagram no soporta templates. Usar send_message con quick_replies."
            )

        # Facebook Messenger — template de boton o generico
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
                f"{self.BASE_URL}/me/messages",
                params={"access_token": channel_config["page_access_token"]},
                json=message_payload,
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("message_id", "")

    def get_channel_constraints(self) -> ChannelConstraints:
        if self.channel == MetaChannel.INSTAGRAM:
            return ChannelConstraints(
                max_text_length=1000,               # Instagram: max 1000 chars
                supported_media_types=["image", "audio", "video"],
                session_window_hours=24,             # Ventana estricta de 24h
                requires_template_outside_window=False,  # No hay templates en IG
                max_buttons=13,                      # Quick replies max 13
                max_list_items=0,                    # No hay listas en IG
            )
        else:  # Facebook Messenger
            return ChannelConstraints(
                max_text_length=2000,               # Facebook: max 2000 chars
                supported_media_types=["image", "audio", "video", "document"],
                session_window_hours=24,             # Ventana de 24h (message_tags fuera)
                requires_template_outside_window=False,  # Usa message_tags, no templates HSM
                max_buttons=3,                       # Botones en templates
                max_list_items=4,                    # Elementos en template generico
            )
```

**Nota sobre ventanas de mensajes:**
- **Instagram DM:** Ventana estricta de 24h. No se puede enviar mensajes fuera de ventana. No hay templates.
- **Facebook Messenger:** Ventana de 24h para mensajes normales. Fuera de ventana, se pueden usar `message_tags` para casos especificos (CONFIRMED_EVENT_UPDATE, POST_PURCHASE_UPDATE, ACCOUNT_UPDATE).

### 5. Provider Factory (`app/services/messaging/factory.py`)

```python
from functools import lru_cache

_PROVIDERS: dict[str, type[MessagingProvider]] = {
    "ycloud": YCloudProvider,
    "meta": MetaProvider,       # Instagram DM + Facebook Messenger
    # Futuros proveedores:
    # "twilio": TwilioProvider,
}

def get_messaging_provider(provider_name: str, provider_config: dict | None = None) -> MessagingProvider:
    """
    Factory para obtener el provider adecuado.

    El nombre del provider se recibe de la URL del webhook:
    POST /api/v1/webhooks/{provider}/{channel}

    Para MetaProvider, el channel (instagram/facebook) se pasa en provider_config
    para diferenciar el sub-canal.
    """
    provider_class = _PROVIDERS.get(provider_name)
    if not provider_class:
        raise ValueError(f"Provider '{provider_name}' no registrado. Disponibles: {list(_PROVIDERS.keys())}")

    if provider_name == "meta" and provider_config:
        return provider_class(provider_config)

    return provider_class()
```

### 6. Webhook Endpoint (`app/api/v1/webhooks.py`)

```python
from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import JSONResponse
import redis.asyncio as aioredis

router = APIRouter()

DEDUP_TTL_SECONDS = 86400  # 24 horas

@router.post("/webhooks/{provider}/{channel}")
async def receive_webhook(
    provider: str,
    channel: str,
    request: Request,
):
    """
    Endpoint publico para recibir webhooks de proveedores de mensajeria.

    IMPORTANTE:
    - Este endpoint NO usa TenantContextMiddleware (no tiene JWT)
    - La autenticacion es por firma del webhook (HMAC)
    - Debe responder 200 en < 100ms (procesamiento es asincrono)
    - El client_id se resuelve por configuracion del channel
    """
    raw_body = await request.body()
    payload = await request.json()

    # 1. Obtener el provider
    try:
        messaging_provider = get_messaging_provider(provider)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Provider '{provider}' no soportado")

    # 2. Validar firma del webhook
    # Cada provider usa un header diferente para la firma:
    # - YCloud: X-Ycloud-Signature
    # - Meta: x-hub-signature-256
    if provider == "meta":
        signature = request.headers.get("x-hub-signature-256", "")
        webhook_secret = settings.META_APP_SECRET
    else:
        signature = request.headers.get("X-Ycloud-Signature", "")
        webhook_secret = settings.YCLOUD_WEBHOOK_SECRET

    if not await messaging_provider.validate_signature(raw_body, signature, webhook_secret):
        raise HTTPException(status_code=401, detail="Firma de webhook invalida")

    # 3. Parsear a NormalizedMessage
    try:
        normalized = await messaging_provider.parse_webhook(payload)
    except Exception as e:
        logger.error(f"Error parseando webhook: {e}", extra={"payload": payload})
        # Retornar 200 para evitar reintentos del proveedor
        return JSONResponse(status_code=200, content={"status": "parse_error"})

    # 4. Deduplicacion — Check Redis primero (rapido)
    dedup_key = f"webhook_dedup:{normalized.channel}:{normalized.external_message_id}"
    redis_client = get_redis()

    already_seen = await redis_client.set(dedup_key, "1", nx=True, ex=DEDUP_TTL_SECONDS)
    if not already_seen:
        # Ya procesado — retornar 200 silenciosamente (idempotente)
        logger.info(f"Webhook duplicado ignorado: {normalized.external_message_id}")
        return JSONResponse(status_code=200, content={"status": "duplicate"})

    # 5. Encolar en Celery para procesamiento asincrono
    from app.tasks.webhook_processor import process_incoming_message
    process_incoming_message.delay(
        provider=provider,
        channel=channel,
        normalized_message=normalized.model_dump(mode="json"),
    )

    # 6. Responder 200 inmediatamente (< 100ms)
    return JSONResponse(status_code=200, content={"status": "queued"})
```

### 7. Webhook Verification Endpoint

Algunos proveedores requieren un GET para verificar la URL del webhook:

```python
@router.get("/webhooks/{provider}/{channel}")
async def verify_webhook(
    provider: str,
    channel: str,
    request: Request,
):
    """
    Verificacion de webhook (GET).

    - YCloud: envía un challenge simple como query param.
    - Meta (Instagram/Facebook): envía hub.mode, hub.verify_token, hub.challenge.
      Se debe verificar que hub.verify_token coincida con el token configurado
      y responder con hub.challenge como texto plano.
    """
    if provider == "meta":
        # Meta Webhook Verification
        mode = request.query_params.get("hub.mode")
        token = request.query_params.get("hub.verify_token")
        challenge = request.query_params.get("hub.challenge")

        if mode == "subscribe" and token == settings.META_WEBHOOK_VERIFY_TOKEN:
            return Response(content=challenge, media_type="text/plain")
        raise HTTPException(status_code=403, detail="Verificacion de webhook fallida")

    # Para YCloud y similares
    challenge = request.query_params.get("challenge")
    if challenge:
        return Response(content=challenge, media_type="text/plain")
    return JSONResponse(status_code=200, content={"status": "ok"})
```

### 8. Worker: process_incoming_message (`app/tasks/webhook_processor.py`)

```python
from celery import shared_task
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)

@shared_task(
    name="app.tasks.webhook_process_incoming",
    bind=True,
    max_retries=3,
    default_retry_delay=5,  # 5 segundos base
    retry_backoff=True,      # Exponencial: 5s, 25s, 125s
    retry_backoff_max=300,   # Max 5 minutos
    acks_late=True,
    queue="webhooks",
)
def process_incoming_message(self, provider: str, channel: str, normalized_message: dict):
    """
    Procesa un mensaje entrante normalizado.

    Flujo:
    1. Deserializar NormalizedMessage
    2. Resolver client_id por configuracion del canal
    3. Resolver o crear contacto
    4. Resolver o crear conversacion
    5. Guardar mensaje en DB
    6. Guardar en webhook_dedup (persistencia para auditoria)
    7. Encolar procesamiento AI
    """
    try:
        # Esto se ejecuta de manera sincrona dentro de Celery
        # Usar asyncio.run() o sync_to_async para las operaciones async
        import asyncio
        asyncio.run(_process_message(provider, channel, normalized_message))

    except Exception as exc:
        logger.error(f"Error procesando mensaje: {exc}", exc_info=True)

        if self.request.retries < self.max_retries:
            # Reintentar con backoff exponencial
            raise self.retry(exc=exc)
        else:
            # Max retries alcanzado — enviar a Dead Letter Queue
            logger.critical(
                f"Mensaje enviado a DLQ despues de {self.max_retries} intentos",
                extra={"message": normalized_message},
            )
            _send_to_dlq(normalized_message)


async def _process_message(provider: str, channel: str, message_data: dict):
    """Logica asincrona de procesamiento."""
    normalized = NormalizedMessage(**message_data)

    # 1. Resolver client_id
    # En MVP, se puede resolver por la configuracion global del provider
    # En produccion, se resuelve por channel_configs (Fase 2) o por URL path
    client_id = await _resolve_client_id(provider, channel, normalized.sender_identifier)

    # 2. Resolver o crear contacto
    contact = await _resolve_contact(client_id, normalized.channel, normalized.sender_identifier)

    # 3. Resolver o crear conversacion
    conversation = await _resolve_conversation(client_id, contact.id, normalized.channel)

    # 4. Guardar mensaje
    async with tenant_session(client_id) as session:
        message = Message(
            client_id=client_id,
            conversation_id=conversation.id,
            contact_id=contact.id,
            direction="inbound",
            message_type=normalized.media_type or "text",
            content=normalized.text,
            media_url=normalized.media_url,
            external_message_id=normalized.external_message_id,
            metadata=normalized.raw_payload,
        )
        session.add(message)

        # 5. Actualizar last_message_at de la conversacion
        conversation.last_message_at = normalized.timestamp
        session.add(conversation)

        # 6. Registrar en webhook_dedup (persistencia)
        dedup = WebhookDedup(
            client_id=client_id,
            channel=normalized.channel,
            external_message_id=normalized.external_message_id,
            processed=True,
        )
        session.add(dedup)

        await session.commit()

    # 7. Encolar procesamiento AI
    from app.tasks.ai_processor import process_ai_response
    process_ai_response.delay(
        client_id=str(client_id),
        conversation_id=str(conversation.id),
        contact_id=str(contact.id),
        channel=channel,
        message_data=message_data,
    )
```

### 9. Contact Resolver (`app/services/contact_resolver.py`)

```python
async def resolve_contact(
    client_id: UUID,
    channel: ChannelEnum,
    identifier_value: str,
) -> Contact:
    """
    Busca un contacto existente por (channel, identifier_value) o crea uno nuevo.

    Logica:
    1. Buscar en contact_identifiers por (client_id, channel, identifier_descifrado)
    2. Si existe → retornar el contacto vinculado
    3. Si no existe → crear nuevo contacto + identifier
    4. Si el contacto encontrado tiene merged_into_id → seguir la cadena de merge

    NOTA: El identifier_value se almacena cifrado con pgcrypto.
    """
    async with tenant_session(client_id) as session:
        # Buscar identifier existente
        # NOTA: La busqueda en identifier cifrado requiere cifrar el valor de busqueda
        # y comparar, o usar una columna de hash adicional para busqueda
        stmt = select(ContactIdentifier).where(
            ContactIdentifier.client_id == client_id,
            ContactIdentifier.channel == channel,
            # Comparacion con valor cifrado — depende de la implementacion de cifrado
        )
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            # Obtener contacto
            contact = await session.get(Contact, existing.contact_id)

            # Seguir cadena de merge
            while contact and contact.merged_into_id:
                contact = await session.get(Contact, contact.merged_into_id)

            return contact

        # Crear nuevo contacto + identifier
        contact = Contact(
            client_id=client_id,
            display_name=identifier_value,  # Nombre provisional
        )
        session.add(contact)
        await session.flush()  # Para obtener contact.id

        identifier = ContactIdentifier(
            client_id=client_id,
            contact_id=contact.id,
            channel=channel,
            identifier_value=encrypt_identifier(identifier_value),
            is_primary=True,
        )
        session.add(identifier)
        await session.commit()

        return contact
```

### 10. Conversation Resolver

```python
async def resolve_conversation(
    client_id: UUID,
    contact_id: UUID,
    channel: ChannelEnum,
) -> Conversation:
    """
    Busca una conversacion activa para el contacto en el canal, o crea una nueva.

    Una conversacion se considera "activa" si su status NO es 'resolved' ni 'archived'.
    Si hay multiples activas, usa la mas reciente.
    """
    async with tenant_session(client_id) as session:
        stmt = (
            select(Conversation)
            .where(
                Conversation.client_id == client_id,
                Conversation.contact_id == contact_id,
                Conversation.channel == channel,
                Conversation.status.not_in(["resolved", "archived"]),
            )
            .order_by(Conversation.last_message_at.desc().nullslast())
            .limit(1)
        )
        result = await session.execute(stmt)
        conversation = result.scalar_one_or_none()

        if conversation:
            return conversation

        # Crear nueva conversacion
        conversation = Conversation(
            client_id=client_id,
            contact_id=contact_id,
            channel=channel,
            status="bot_active",  # Inicia con el bot
            started_at=datetime.now(timezone.utc),
        )
        session.add(conversation)
        await session.commit()

        return conversation
```

### 11. Dead Letter Queue

```python
async def _send_to_dlq(message_data: dict):
    """
    Envia un mensaje fallido a la Dead Letter Queue en Redis.
    Los mensajes en DLQ se pueden revisar y reprocesar manualmente.
    """
    redis_client = get_redis()
    dlq_key = "dlq:webhook_messages"
    await redis_client.lpush(dlq_key, json.dumps({
        "message": message_data,
        "failed_at": datetime.now(timezone.utc).isoformat(),
        "retries_exhausted": True,
    }))
    # Opcional: alertar via notificacion
```

### 12. Tests

#### test_webhook_dedup.py

```python
@pytest.mark.asyncio
async def test_duplicate_webhook_ignored(client):
    """Enviar el mismo mensaje 2 veces: solo se procesa 1."""
    payload = make_ycloud_payload(external_id="wamid.123")

    # Primer envio
    resp1 = await client.post(
        "/api/v1/webhooks/ycloud/whatsapp",
        json=payload,
        headers={"X-Ycloud-Signature": compute_signature(payload)},
    )
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "queued"

    # Segundo envio (duplicado)
    resp2 = await client.post(
        "/api/v1/webhooks/ycloud/whatsapp",
        json=payload,
        headers={"X-Ycloud-Signature": compute_signature(payload)},
    )
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "duplicate"

@pytest.mark.asyncio
async def test_invalid_signature_rejected(client):
    """Firma invalida retorna 401."""
    payload = make_ycloud_payload()
    resp = await client.post(
        "/api/v1/webhooks/ycloud/whatsapp",
        json=payload,
        headers={"X-Ycloud-Signature": "invalid_signature"},
    )
    assert resp.status_code == 401
```

#### test_messaging_provider.py

```python
@pytest.mark.asyncio
async def test_ycloud_parse_text_message():
    """Payload de texto de YCloud se normaliza correctamente."""
    provider = YCloudProvider()
    payload = {
        "id": "evt_123",
        "type": "whatsapp.inbound_message.received",
        "whatsappInboundMessage": {
            "id": "wamid.456",
            "from": "+5491155667788",
            "timestamp": "2024-01-15T10:30:00Z",
            "type": "text",
            "text": {"body": "Hola, necesito una cita"},
        },
    }

    normalized = await provider.parse_webhook(payload)

    assert normalized.channel == ChannelEnum.whatsapp
    assert normalized.sender_identifier == "+5491155667788"
    assert normalized.text == "Hola, necesito una cita"
    assert normalized.external_message_id == "wamid.456"
    assert normalized.media_url is None

@pytest.mark.asyncio
async def test_ycloud_parse_image_message():
    """Payload de imagen de YCloud se normaliza con media_url."""
    ...

@pytest.mark.asyncio
async def test_ycloud_validate_signature_valid():
    """Firma HMAC valida pasa la validacion."""
    ...

@pytest.mark.asyncio
async def test_ycloud_validate_signature_invalid():
    """Firma HMAC invalida falla la validacion."""
    ...
```

#### test_contact_resolver.py

```python
@pytest.mark.asyncio
async def test_same_phone_two_messages_same_contact():
    """Dos mensajes del mismo telefono resuelven al mismo contacto."""
    contact1 = await resolve_contact(client_id, ChannelEnum.whatsapp, "+5491155667788")
    contact2 = await resolve_contact(client_id, ChannelEnum.whatsapp, "+5491155667788")
    assert contact1.id == contact2.id

@pytest.mark.asyncio
async def test_different_phone_different_contacts():
    """Dos telefonos diferentes crean contactos diferentes."""
    contact1 = await resolve_contact(client_id, ChannelEnum.whatsapp, "+5491155667788")
    contact2 = await resolve_contact(client_id, ChannelEnum.whatsapp, "+5491199887766")
    assert contact1.id != contact2.id

@pytest.mark.asyncio
async def test_merged_contact_resolves_to_target():
    """Un contacto con merged_into_id resuelve al contacto target."""
    ...
```

#### test_meta_provider.py

```python
@pytest.mark.asyncio
async def test_meta_parse_instagram_text_message():
    """Payload de texto de Instagram DM se normaliza correctamente."""
    provider = MetaProvider({
        "page_access_token": "test_token",
        "app_secret": "test_secret",
        "channel": "instagram",
    })
    payload = {
        "object": "instagram",
        "entry": [{
            "id": "ig_123",
            "time": 1700000000,
            "messaging": [{
                "sender": {"id": "ig_user_456"},
                "recipient": {"id": "ig_page_789"},
                "timestamp": 1700000000000,
                "message": {
                    "mid": "m_abc123",
                    "text": "Hola, quiero informacion",
                },
            }],
        }],
    }

    normalized = await provider.parse_webhook(payload)

    assert normalized.channel == ChannelEnum.instagram
    assert normalized.sender_identifier == "ig_user_456"
    assert normalized.text == "Hola, quiero informacion"
    assert normalized.external_message_id == "m_abc123"
    assert normalized.media_url is None

@pytest.mark.asyncio
async def test_meta_parse_facebook_text_message():
    """Payload de texto de Facebook Messenger se normaliza correctamente."""
    provider = MetaProvider({
        "page_access_token": "test_token",
        "app_secret": "test_secret",
        "channel": "facebook",
    })
    payload = {
        "object": "page",
        "entry": [{
            "id": "page_123",
            "time": 1700000000,
            "messaging": [{
                "sender": {"id": "fb_user_456"},
                "recipient": {"id": "page_789"},
                "timestamp": 1700000000000,
                "message": {
                    "mid": "m_def456",
                    "text": "Necesito ayuda con mi pedido",
                },
            }],
        }],
    }

    normalized = await provider.parse_webhook(payload)

    assert normalized.channel == ChannelEnum.facebook
    assert normalized.sender_identifier == "fb_user_456"
    assert normalized.text == "Necesito ayuda con mi pedido"
    assert normalized.external_message_id == "m_def456"

@pytest.mark.asyncio
async def test_meta_parse_image_attachment():
    """Payload con imagen de Meta se normaliza con media_url."""
    provider = MetaProvider({
        "page_access_token": "test_token",
        "app_secret": "test_secret",
        "channel": "instagram",
    })
    payload = {
        "object": "instagram",
        "entry": [{
            "id": "ig_123",
            "time": 1700000000,
            "messaging": [{
                "sender": {"id": "ig_user_456"},
                "recipient": {"id": "ig_page_789"},
                "timestamp": 1700000000000,
                "message": {
                    "mid": "m_img789",
                    "attachments": [{
                        "type": "image",
                        "payload": {"url": "https://scontent.xx.fbcdn.net/image.jpg"},
                    }],
                },
            }],
        }],
    }

    normalized = await provider.parse_webhook(payload)
    assert normalized.media_url == "https://scontent.xx.fbcdn.net/image.jpg"
    assert normalized.media_type == MessageTypeEnum.image

@pytest.mark.asyncio
async def test_meta_validate_signature_valid():
    """Firma x-hub-signature-256 valida pasa la validacion."""
    provider = MetaProvider({
        "page_access_token": "test_token",
        "app_secret": "test_secret",
        "channel": "facebook",
    })
    payload = b'{"test": "data"}'
    secret = "my_app_secret"
    valid_sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    assert await provider.validate_signature(payload, valid_sig, secret)

@pytest.mark.asyncio
async def test_meta_validate_signature_invalid():
    """Firma invalida falla la validacion."""
    provider = MetaProvider({
        "page_access_token": "test_token",
        "app_secret": "test_secret",
        "channel": "facebook",
    })
    assert not await provider.validate_signature(b"data", "sha256=invalid", "secret")
    assert not await provider.validate_signature(b"data", "invalid_format", "secret")

@pytest.mark.asyncio
async def test_meta_instagram_constraints():
    """Constraints de Instagram: 1000 chars, 24h window, sin templates."""
    provider = MetaProvider({
        "page_access_token": "t",
        "app_secret": "s",
        "channel": "instagram",
    })
    constraints = provider.get_channel_constraints()
    assert constraints.max_text_length == 1000
    assert constraints.session_window_hours == 24
    assert constraints.requires_template_outside_window is False

@pytest.mark.asyncio
async def test_meta_facebook_constraints():
    """Constraints de Facebook: 2000 chars, 24h window, con templates."""
    provider = MetaProvider({
        "page_access_token": "t",
        "app_secret": "s",
        "channel": "facebook",
    })
    constraints = provider.get_channel_constraints()
    assert constraints.max_text_length == 2000
    assert constraints.session_window_hours == 24
    assert constraints.max_buttons == 3

@pytest.mark.asyncio
async def test_meta_instagram_send_template_raises():
    """Instagram no soporta templates — debe lanzar NotImplementedError."""
    provider = MetaProvider({
        "page_access_token": "t",
        "app_secret": "s",
        "channel": "instagram",
    })
    with pytest.raises(NotImplementedError):
        await provider.send_template("user_123", TemplateMessage(...), {})
```

#### test_webhook_flow.py (integracion)

```python
@pytest.mark.asyncio
async def test_full_webhook_to_db_flow_ycloud():
    """
    Test de integracion completo (YCloud/WhatsApp):
    1. Enviar webhook
    2. Verificar que se creo contacto
    3. Verificar que se creo conversacion (status: bot_active)
    4. Verificar que se guardo mensaje en DB
    5. Verificar registro en webhook_dedup
    """
    ...

@pytest.mark.asyncio
async def test_full_webhook_to_db_flow_meta_instagram():
    """
    Test de integracion completo (Meta/Instagram):
    1. Enviar webhook con firma x-hub-signature-256
    2. Verificar normalizacion a NormalizedMessage con channel=instagram
    3. Verificar contacto creado con sender_identifier del PSID
    4. Verificar conversacion y mensaje en DB
    """
    ...

@pytest.mark.asyncio
async def test_full_webhook_to_db_flow_meta_facebook():
    """
    Test de integracion completo (Meta/Facebook Messenger):
    Similar a Instagram pero con channel=facebook y constraints diferentes.
    """
    ...

@pytest.mark.asyncio
async def test_meta_webhook_verification_get():
    """
    GET /webhooks/meta/{channel} responde al challenge de Meta.
    hub.mode=subscribe + hub.verify_token correcto → retorna hub.challenge.
    """
    ...
```

## Criterios de Aceptacion

### YCloud (WhatsApp)
- [ ] Webhook de YCloud recibido, normalizado y almacenado como NormalizedMessage
- [ ] Firma HMAC-SHA256 de YCloud validada correctamente (header `X-Ycloud-Signature`)
- [ ] Segundo envio del mismo external_message_id es ignorado (responde 200 con status "duplicate")
- [ ] Firma invalida retorna HTTP 401

### Meta (Instagram DM + Facebook Messenger)
- [ ] Webhook de Instagram DM recibido y normalizado con `channel=instagram`
- [ ] Webhook de Facebook Messenger recibido y normalizado con `channel=facebook`
- [ ] Firma `x-hub-signature-256` de Meta validada correctamente
- [ ] Webhook verification GET responde al challenge de Meta (`hub.mode=subscribe`)
- [ ] Instagram: `get_channel_constraints()` retorna max 1000 chars, 24h window, sin templates
- [ ] Facebook: `get_channel_constraints()` retorna max 2000 chars, 24h window, con templates
- [ ] Instagram: `send_template()` lanza `NotImplementedError`
- [ ] Attachments de imagen/audio/video parseados correctamente en ambos canales

### Comunes (todos los proveedores)
- [ ] Worker Celery procesa y guarda en DB correctamente (message, contact, conversation)
- [ ] Contact unification funciona: mismo identificador en 2 mensajes crea 1 solo contacto con 1 identifier
- [ ] Conversacion nueva se crea con status "bot_active"
- [ ] Mensaje existente en conversacion activa se vincula a la conversacion existente
- [ ] Retry con backoff exponencial funciona (5s, 25s, 125s)
- [ ] Dead Letter Queue recibe mensajes que fallaron despues de 3 intentos
- [ ] El endpoint de webhook responde 200 en menos de 100ms (procesamiento es asincrono via Celery)
- [ ] Factory resuelve correctamente: `ycloud` → YCloudProvider, `meta` → MetaProvider(instagram/facebook)

## Notas Tecnicas

### Webhook SIN TenantContextMiddleware
El endpoint de webhook NO usa `TenantContextMiddleware` porque:
- Los webhooks son publicos (no tienen JWT)
- La autenticacion es por firma HMAC del proveedor
- El `client_id` se resuelve por la configuracion del canal, no por JWT

En el futuro (Fase 2, tabla `channel_configs`), el `client_id` se resolvera por la configuracion del canal especifico. En el MVP, se puede resolver por configuracion global o por la URL del webhook.

### Meta Graph API — Consideraciones
- **App Review**: La app de Meta necesita pasar App Review para permisos de `instagram_messaging` y `pages_messaging`. Durante desarrollo, se puede usar en modo test con usuarios de prueba.
- **Page Access Token**: Los tokens de pagina tienen expiración. Usar tokens de larga duración (60 dias) y renovarlos periodicamente. En produccion, considerar tokens que nunca expiran (System User tokens).
- **Rate Limits**: Meta tiene rate limiting por pagina (~200 llamadas/hora para envio). Implementar throttling en el worker de envio.
- **Webhook Fields**: Al registrar el webhook en Meta, suscribirse a `messages`, `messaging_postbacks`, `messaging_optins` para Messenger, y `messages` para Instagram.
- **PSID vs User ID**: El `sender.id` es un Page-Scoped ID (PSID) unico por pagina, no el ID real del usuario de Facebook/Instagram.

### Deduplicacion en dos niveles
1. **Redis** (rapido, TTL 24h): primera linea de defensa, O(1)
2. **PostgreSQL** (permanente, tabla webhook_dedup): para auditoria y como backup si Redis se reinicia

El flujo es: check Redis → si no existe, insertar en Redis → encolar → el worker inserta en webhook_dedup.

### Celery Retry con Backoff
Los delays de retry son exponenciales: 5s, 25s, 125s. Esto evita saturar servicios externos cuando hay problemas temporales. Despues de 3 intentos fallidos, el mensaje va a DLQ para revision manual.

### Cifrado de Identifiers
La busqueda en `contact_identifiers` con valores cifrados puede ser costosa. Alternativas:
- Almacenar un hash (SHA-256) del identifier en una columna adicional para busqueda O(1)
- Usar cifrado determinista (no recomendado para datos sensibles)
- La implementacion preferida: hash_column para busqueda + valor cifrado para lectura

## Variables de Entorno Nuevas

```env
# YCloud (existente)
YCLOUD_API_KEY=your_ycloud_api_key
YCLOUD_WEBHOOK_SECRET=your_ycloud_webhook_secret

# Meta (Facebook + Instagram) — NUEVAS
META_APP_SECRET=your_meta_app_secret
META_PAGE_ACCESS_TOKEN=your_meta_page_access_token
META_WEBHOOK_VERIFY_TOKEN=your_custom_verify_token
```

## Dependencias para Sprint 5
- `NormalizedMessage` schema disponible e importable
- `MessagingProvider` ABC disponible para envio de respuestas (usado por el nodo `respond` del grafo)
- `MetaProvider` funcional para envio de respuestas por Instagram DM y Facebook Messenger
- Contact resolver funcional para vincular mensajes a contactos
- Celery queues operativas (especialmente `webhooks` y `ai_inference`)
- Conversacion creada con status `bot_active` lista para ser procesada por el grafo de agentes
- Factory resuelve 2 proveedores: YCloud (WhatsApp) y Meta (Instagram + Facebook)
