# Sprint 11 — Webhooks Salientes & CSAT (Fase 2)

## Objetivo

Implementar un sistema de webhooks salientes para que tenants puedan integrar la plataforma con sistemas externos (CRM, ERP, etc.) mediante eventos, y un sistema automático de encuestas CSAT (Customer Satisfaction Score) post-resolución de conversaciones.

## Prerequisitos

- Sprint 10 completado (templates y sentimiento funcionales).
- Tablas `tenant_webhooks`, `outgoing_webhook_logs` y `satisfaction_surveys` definidas en el esquema de Fase 2. Si no existen, crear migraciones.
- Celery con cola `notifications` configurada.
- Dashboard de Grafana operativo (Sprint 8) para agregar panel CSAT.
- Todos los canales operativos (Sprint 9) para enviar encuestas por el canal correcto.

## Archivos a Crear/Modificar

| Archivo | Acción | Descripción |
|---|---|---|
| `app/api/v1/webhooks_config.py` | Crear | CRUD de configuración de webhooks salientes |
| `app/services/webhook_dispatcher.py` | Crear | Event dispatcher con HMAC y retry |
| `app/services/csat.py` | Crear | Servicio de encuestas CSAT |
| `app/tasks/outgoing_webhooks.py` | Crear | Tasks Celery para envío de webhooks |
| `app/tasks/csat_tasks.py` | Crear | Tasks Celery para CSAT |
| `app/models/tenant_webhook.py` | Crear | Modelo SQLAlchemy para tenant_webhooks |
| `app/models/outgoing_webhook_log.py` | Crear | Modelo para logs de webhooks |
| `app/models/satisfaction_survey.py` | Crear | Modelo para encuestas CSAT |
| `app/schemas/webhook_config.py` | Crear | Schemas Pydantic para webhooks |
| `app/schemas/csat.py` | Crear | Schemas Pydantic para CSAT |
| `app/api/v1/admin.py` | Modificar | Agregar endpoint de resumen CSAT |
| `app/core/events.py` | Crear | Sistema de eventos internos |
| `grafana/dashboards/csat.json` | Crear | Dashboard CSAT en Grafana |
| `migrations/versions/xxx_webhooks.py` | Crear | Migración para tablas de webhooks |
| `migrations/versions/xxx_csat.py` | Crear | Migración para tabla satisfaction_surveys |
| `tests/unit/test_webhook_dispatcher.py` | Crear | Tests del dispatcher |
| `tests/unit/test_csat.py` | Crear | Tests del servicio CSAT |
| `tests/integration/test_webhook_flow.py` | Crear | Test de flujo completo |

## Tareas Detalladas

### 1. Modelos

**1.1 Modelo tenant_webhooks — `app/models/tenant_webhook.py`**

```python
# app/models/tenant_webhook.py
from sqlalchemy import Column, String, Boolean, DateTime
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY
from app.models.base import Base, TimestampMixin
import uuid

class TenantWebhook(Base, TimestampMixin):
    __tablename__ = "tenant_webhooks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    url = Column(String(2048), nullable=False)  # URL destino
    secret = Column(String(256), nullable=False)  # HMAC secret
    events = Column(ARRAY(String), nullable=False)  # Lista de eventos suscritos
    is_active = Column(Boolean, default=True, nullable=False)
    description = Column(String(500))
    headers = Column(JSONB, default={})  # Headers custom adicionales
    consecutive_failures = Column(Integer, default=0)
    last_triggered_at = Column(DateTime)
    last_success_at = Column(DateTime)
    last_failure_at = Column(DateTime)
    disabled_reason = Column(String(200))  # "auto_disabled_failures" si se desactivó automáticamente
```

**1.2 Modelo outgoing_webhook_logs — `app/models/outgoing_webhook_log.py`**

```python
# app/models/outgoing_webhook_log.py
class OutgoingWebhookLog(Base):
    __tablename__ = "outgoing_webhook_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    webhook_id = Column(UUID(as_uuid=True), ForeignKey("tenant_webhooks.id"), nullable=False, index=True)
    client_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    event = Column(String(100), nullable=False)
    payload = Column(JSONB, nullable=False)
    status = Column(String(20), nullable=False)  # "success", "failed", "pending", "retrying"
    response_code = Column(Integer)  # HTTP status code de la respuesta
    response_body = Column(Text)  # Primeros 1000 chars del response body
    duration_ms = Column(Integer)  # Duración del request en ms
    attempt = Column(Integer, default=1)  # Número de intento (1, 2, 3)
    error = Column(Text)  # Descripción del error si falló
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
```

**1.3 Modelo satisfaction_surveys — `app/models/satisfaction_survey.py`**

```python
# app/models/satisfaction_survey.py
class SatisfactionSurvey(Base, TimestampMixin):
    __tablename__ = "satisfaction_surveys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False, unique=True)
    contact_id = Column(UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False)
    channel = Column(String(50), nullable=False)
    rating = Column(Integer)  # 1-5, NULL si no respondió
    comment = Column(Text)  # Comentario opcional
    sent_at = Column(DateTime, nullable=False)
    responded_at = Column(DateTime)
    survey_message_id = Column(String(200))  # provider_message_id del mensaje de encuesta
    status = Column(String(20), default="sent")  # "sent", "responded", "expired"
```

**1.4 Migraciones**

```sql
-- tenant_webhooks
CREATE TABLE tenant_webhooks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL REFERENCES clients(id),
    url VARCHAR(2048) NOT NULL,
    secret VARCHAR(256) NOT NULL,
    events TEXT[] NOT NULL,
    is_active BOOLEAN DEFAULT TRUE NOT NULL,
    description VARCHAR(500),
    headers JSONB DEFAULT '{}',
    consecutive_failures INTEGER DEFAULT 0,
    last_triggered_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_failure_at TIMESTAMPTZ,
    disabled_reason VARCHAR(200),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE tenant_webhooks ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_webhooks_isolation ON tenant_webhooks
    USING (client_id = current_setting('app.current_client_id')::UUID);

CREATE INDEX idx_tenant_webhooks_active_events ON tenant_webhooks (client_id, is_active) WHERE is_active = TRUE;

-- outgoing_webhook_logs
CREATE TABLE outgoing_webhook_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    webhook_id UUID NOT NULL REFERENCES tenant_webhooks(id) ON DELETE CASCADE,
    client_id UUID NOT NULL,
    event VARCHAR(100) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(20) NOT NULL,
    response_code INTEGER,
    response_body TEXT,
    duration_ms INTEGER,
    attempt INTEGER DEFAULT 1,
    error TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

ALTER TABLE outgoing_webhook_logs ENABLE ROW LEVEL SECURITY;
CREATE POLICY webhook_logs_isolation ON outgoing_webhook_logs
    USING (client_id = current_setting('app.current_client_id')::UUID);

CREATE INDEX idx_webhook_logs_webhook_status ON outgoing_webhook_logs (webhook_id, status);
CREATE INDEX idx_webhook_logs_created_at ON outgoing_webhook_logs (created_at);

-- Particionar por fecha si el volumen lo justifica (opcional)

-- satisfaction_surveys
CREATE TABLE satisfaction_surveys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL REFERENCES clients(id),
    conversation_id UUID NOT NULL REFERENCES conversations(id) UNIQUE,
    contact_id UUID NOT NULL REFERENCES contacts(id),
    channel VARCHAR(50) NOT NULL,
    rating INTEGER CHECK (rating >= 1 AND rating <= 5),
    comment TEXT,
    sent_at TIMESTAMPTZ NOT NULL,
    responded_at TIMESTAMPTZ,
    survey_message_id VARCHAR(200),
    status VARCHAR(20) DEFAULT 'sent',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE satisfaction_surveys ENABLE ROW LEVEL SECURITY;
CREATE POLICY csat_isolation ON satisfaction_surveys
    USING (client_id = current_setting('app.current_client_id')::UUID);

CREATE INDEX idx_csat_client_date ON satisfaction_surveys (client_id, sent_at);
```

### 2. CRUD tenant_webhooks — `app/api/v1/webhooks_config.py`

```python
# app/api/v1/webhooks_config.py
from fastapi import APIRouter, Depends, HTTPException
from uuid import UUID
import secrets

router = APIRouter(prefix="/api/v1/webhooks/outgoing", tags=["Outgoing Webhooks"])

SUPPORTED_EVENTS = [
    "message.received",
    "message.sent",
    "conversation.created",
    "conversation.resolved",
    "contact.created",
    "contact.updated",
    "appointment.created",
]

@router.post("", status_code=201)
async def create_outgoing_webhook(
    data: WebhookCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "super_admin"])),
):
    """
    Configurar un nuevo webhook saliente.
    Si no se proporciona secret, se genera uno automáticamente.
    """
    # Validar que los eventos son soportados
    invalid_events = set(data.events) - set(SUPPORTED_EVENTS)
    if invalid_events:
        raise HTTPException(400, f"Eventos no soportados: {invalid_events}")

    # Validar URL (HTTPS obligatorio en producción)
    if not data.url.startswith("https://") and settings.ENVIRONMENT == "production":
        raise HTTPException(400, "La URL del webhook debe usar HTTPS en producción")

    webhook = TenantWebhook(
        client_id=current_user.client_id,
        url=data.url,
        secret=data.secret or secrets.token_hex(32),
        events=data.events,
        description=data.description,
        headers=data.headers or {},
    )
    db.add(webhook)
    await db.commit()
    await db.refresh(webhook)
    return webhook

@router.get("")
async def list_outgoing_webhooks(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "super_admin"])),
):
    """Listar configuraciones de webhooks del tenant."""
    result = await db.execute(
        select(TenantWebhook)
        .where(TenantWebhook.client_id == current_user.client_id)
        .order_by(TenantWebhook.created_at.desc())
    )
    return result.scalars().all()

@router.put("/{webhook_id}")
async def update_outgoing_webhook(
    webhook_id: UUID,
    data: WebhookUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "super_admin"])),
):
    """Actualizar configuración de webhook. Puede reactivar un webhook desactivado automáticamente."""
    webhook = await db.get(TenantWebhook, webhook_id)
    if not webhook:
        raise HTTPException(404, "Webhook no encontrado")

    for field, value in data.dict(exclude_unset=True).items():
        setattr(webhook, field, value)

    # Si se reactiva manualmente, resetear contadores de fallos
    if data.is_active:
        webhook.consecutive_failures = 0
        webhook.disabled_reason = None

    await db.commit()
    return webhook

@router.delete("/{webhook_id}", status_code=204)
async def delete_outgoing_webhook(
    webhook_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "super_admin"])),
):
    """Eliminar configuración de webhook y sus logs."""
    webhook = await db.get(TenantWebhook, webhook_id)
    if not webhook:
        raise HTTPException(404, "Webhook no encontrado")
    await db.delete(webhook)
    await db.commit()

@router.get("/{webhook_id}/logs")
async def get_webhook_logs(
    webhook_id: UUID,
    limit: int = 50,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "super_admin"])),
):
    """Obtener logs de un webhook específico."""
    query = (
        select(OutgoingWebhookLog)
        .where(OutgoingWebhookLog.webhook_id == webhook_id)
    )
    if status:
        query = query.where(OutgoingWebhookLog.status == status)
    query = query.order_by(OutgoingWebhookLog.created_at.desc()).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()

@router.post("/{webhook_id}/test")
async def test_webhook(
    webhook_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "super_admin"])),
):
    """Enviar un evento de prueba al webhook para verificar configuración."""
    webhook = await db.get(TenantWebhook, webhook_id)
    if not webhook:
        raise HTTPException(404, "Webhook no encontrado")

    test_payload = {
        "event": "webhook.test",
        "timestamp": datetime.utcnow().isoformat(),
        "data": {"message": "Este es un evento de prueba."},
    }

    from app.services.webhook_dispatcher import WebhookDispatcher
    dispatcher = WebhookDispatcher()
    result = await dispatcher.send_webhook(webhook, test_payload)

    return {
        "success": result["success"],
        "status_code": result.get("status_code"),
        "response_time_ms": result.get("duration_ms"),
        "error": result.get("error"),
    }
```

### 3. Event Dispatcher — `app/services/webhook_dispatcher.py`

**3.1 Sistema de eventos internos — `app/core/events.py`**

```python
# app/core/events.py
from typing import Callable, Any
from loguru import logger

class EventEmitter:
    """Sistema de eventos internos para disparar webhooks salientes."""

    _handlers: dict[str, list[Callable]] = {}

    @classmethod
    def on(cls, event: str, handler: Callable):
        """Registrar handler para un evento."""
        cls._handlers.setdefault(event, []).append(handler)

    @classmethod
    async def emit(cls, event: str, client_id: str, data: dict):
        """
        Emitir un evento. Los handlers se ejecutan de forma no bloqueante.
        El handler principal es dispatch_outgoing_webhooks que busca webhooks suscritos.
        """
        logger.info(f"Evento emitido: {event}", extra={"client_id": client_id})
        for handler in cls._handlers.get(event, []):
            try:
                await handler(event, client_id, data)
            except Exception as e:
                logger.error(f"Error en handler de evento {event}: {e}")

# Registrar el dispatcher como handler global
from app.tasks.outgoing_webhooks import dispatch_outgoing_webhooks

for event_name in SUPPORTED_EVENTS:
    EventEmitter.on(event_name, lambda e, c, d: dispatch_outgoing_webhooks.delay(e, c, d))
```

**3.2 Integración de eventos en el código existente**

Agregar emisión de eventos en los puntos clave del pipeline:

```python
# En el handler de mensajes entrantes (después de guardar en DB)
await EventEmitter.emit("message.received", str(client_id), {
    "message_id": str(message.id),
    "conversation_id": str(message.conversation_id),
    "contact_id": str(message.contact_id),
    "channel": message.channel,
    "content": message.content,  # Cuidado: no enviar contenido cifrado sin descifrar
    "direction": "incoming",
    "timestamp": message.created_at.isoformat(),
})

# En el handler de mensajes salientes (después de enviar)
await EventEmitter.emit("message.sent", str(client_id), {
    "message_id": str(message.id),
    "conversation_id": str(message.conversation_id),
    "channel": message.channel,
    "content": message.content,
    "direction": "outgoing",
    "timestamp": message.created_at.isoformat(),
})

# Al crear conversación
await EventEmitter.emit("conversation.created", str(client_id), {
    "conversation_id": str(conversation.id),
    "contact_id": str(conversation.contact_id),
    "channel": conversation.channel,
})

# Al resolver conversación
await EventEmitter.emit("conversation.resolved", str(client_id), {
    "conversation_id": str(conversation.id),
    "contact_id": str(conversation.contact_id),
    "resolved_by": str(resolver_user_id),
    "resolution_time_seconds": resolution_time,
})

# Al crear contacto
await EventEmitter.emit("contact.created", str(client_id), {
    "contact_id": str(contact.id),
    "channel": contact.primary_channel,
})

# Al actualizar contacto
await EventEmitter.emit("contact.updated", str(client_id), {
    "contact_id": str(contact.id),
    "updated_fields": list(updated_fields),
})

# Al crear cita
await EventEmitter.emit("appointment.created", str(client_id), {
    "appointment_id": str(appointment.id),
    "contact_id": str(appointment.contact_id),
    "scheduled_at": appointment.scheduled_at.isoformat(),
})
```

**3.3 Dispatcher — `app/services/webhook_dispatcher.py`**

```python
# app/services/webhook_dispatcher.py
import hashlib
import hmac
import json
import time
from datetime import datetime
from uuid import UUID
import httpx
from loguru import logger
from app.models.tenant_webhook import TenantWebhook
from app.models.outgoing_webhook_log import OutgoingWebhookLog

class WebhookDispatcher:
    """Dispatcher de webhooks salientes con HMAC y retry."""

    TIMEOUT = 10  # segundos

    def compute_signature(self, body: str, secret: str, timestamp: str) -> str:
        """
        Calcular HMAC-SHA256 sobre body + timestamp.
        El receptor verifica: HMAC(secret, timestamp + "." + body) == signature
        """
        message = f"{timestamp}.{body}"
        return hmac.new(
            secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def send_webhook(self, webhook: TenantWebhook, payload: dict) -> dict:
        """
        Enviar webhook con firma HMAC y headers estándar.
        Retorna resultado con status, duration, error.
        """
        body = json.dumps(payload, default=str, sort_keys=True)
        timestamp = str(int(time.time()))
        signature = self.compute_signature(body, webhook.secret, timestamp)

        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Signature": f"sha256={signature}",
            "X-Webhook-Event": payload.get("event", "unknown"),
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-ID": str(payload.get("webhook_delivery_id", "")),
            "User-Agent": "ConversationalAI-Webhook/1.0",
            **(webhook.headers or {}),
        }

        start_time = time.monotonic()

        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                response = await client.post(
                    webhook.url,
                    content=body,
                    headers=headers,
                )

            duration_ms = int((time.monotonic() - start_time) * 1000)

            if 200 <= response.status_code < 300:
                return {
                    "success": True,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                    "response_body": response.text[:1000],
                }
            else:
                return {
                    "success": False,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                    "error": f"HTTP {response.status_code}: {response.text[:500]}",
                    "response_body": response.text[:1000],
                }

        except httpx.TimeoutException:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            return {
                "success": False,
                "duration_ms": duration_ms,
                "error": "Timeout after 10 seconds",
            }
        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            return {
                "success": False,
                "duration_ms": duration_ms,
                "error": str(e),
            }
```

### 4. Tasks Celery — `app/tasks/outgoing_webhooks.py`

```python
# app/tasks/outgoing_webhooks.py
from app.core.celery_app import celery_app
from app.services.webhook_dispatcher import WebhookDispatcher
from uuid import uuid4

# Backoff exponencial: 5s, 30s, 300s
RETRY_DELAYS = [5, 30, 300]
MAX_CONSECUTIVE_FAILURES = 10

@celery_app.task(queue="notifications")
async def dispatch_outgoing_webhooks(event: str, client_id: str, data: dict):
    """
    Buscar webhooks suscritos a un evento y enviar a cada uno.
    Se ejecuta en cola 'notifications' para no bloquear el flujo principal.
    """
    async with get_db_session() as db:
        # Buscar webhooks activos del tenant que suscriban este evento
        webhooks = await db.execute(
            select(TenantWebhook).where(
                TenantWebhook.client_id == client_id,
                TenantWebhook.is_active == True,
                TenantWebhook.events.any(event),  # PostgreSQL ANY con ARRAY
            )
        )

        for webhook in webhooks.scalars():
            # Crear payload completo
            payload = {
                "event": event,
                "timestamp": datetime.utcnow().isoformat(),
                "webhook_delivery_id": str(uuid4()),
                "data": data,
            }

            # Enviar con retry
            send_webhook_with_retry.delay(
                webhook_id=str(webhook.id),
                client_id=client_id,
                payload=payload,
                attempt=1,
            )


@celery_app.task(queue="notifications", bind=True)
async def send_webhook_with_retry(
    self,
    webhook_id: str,
    client_id: str,
    payload: dict,
    attempt: int = 1,
):
    """
    Enviar webhook individual con lógica de retry.
    3 intentos: 5s, 30s, 300s (backoff exponencial).
    """
    dispatcher = WebhookDispatcher()

    async with get_db_session() as db:
        webhook = await db.get(TenantWebhook, webhook_id)
        if not webhook or not webhook.is_active:
            return  # Webhook eliminado o desactivado

        result = await dispatcher.send_webhook(webhook, payload)

        # Registrar log
        log = OutgoingWebhookLog(
            webhook_id=webhook.id,
            client_id=client_id,
            event=payload["event"],
            payload=payload,
            status="success" if result["success"] else "failed",
            response_code=result.get("status_code"),
            response_body=result.get("response_body"),
            duration_ms=result.get("duration_ms"),
            attempt=attempt,
            error=result.get("error"),
        )
        db.add(log)

        if result["success"]:
            # Éxito: resetear contador de fallos
            webhook.consecutive_failures = 0
            webhook.last_triggered_at = datetime.utcnow()
            webhook.last_success_at = datetime.utcnow()
            await db.commit()
            logger.info(
                f"Webhook enviado exitosamente",
                extra={"webhook_id": webhook_id, "event": payload["event"]},
            )
        else:
            # Fallo: incrementar contador
            webhook.consecutive_failures += 1
            webhook.last_triggered_at = datetime.utcnow()
            webhook.last_failure_at = datetime.utcnow()

            # Desactivación automática tras MAX_CONSECUTIVE_FAILURES
            if webhook.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                webhook.is_active = False
                webhook.disabled_reason = "auto_disabled_failures"
                await db.commit()
                logger.warning(
                    f"Webhook desactivado automáticamente por {MAX_CONSECUTIVE_FAILURES} fallos consecutivos",
                    extra={"webhook_id": webhook_id},
                )
                # TODO: Notificar admin del tenant
                return

            await db.commit()

            # Retry si no hemos agotado intentos
            if attempt < len(RETRY_DELAYS) + 1:
                delay = RETRY_DELAYS[attempt - 1]
                logger.info(
                    f"Webhook falló, reintentando en {delay}s (intento {attempt}/{len(RETRY_DELAYS) + 1})",
                    extra={"webhook_id": webhook_id},
                )
                send_webhook_with_retry.apply_async(
                    kwargs={
                        "webhook_id": webhook_id,
                        "client_id": client_id,
                        "payload": payload,
                        "attempt": attempt + 1,
                    },
                    countdown=delay,
                )
            else:
                logger.error(
                    f"Webhook falló después de {attempt} intentos",
                    extra={"webhook_id": webhook_id, "error": result.get("error")},
                )
```

### 5. Servicio CSAT — `app/services/csat.py`

```python
# app/services/csat.py
from uuid import UUID
from datetime import datetime, timedelta
from loguru import logger
from app.services.messaging.factory import ProviderFactory

class CSATService:
    """Servicio de encuestas CSAT post-resolución."""

    DEFAULT_DELAY_MINUTES = 5
    SURVEY_EXPIRY_HOURS = 48  # La encuesta expira después de 48h

    async def schedule_survey(
        self,
        db: AsyncSession,
        conversation_id: UUID,
        contact_id: UUID,
        client_id: UUID,
        channel: str,
        delay_minutes: int = None,
    ):
        """
        Programar encuesta CSAT para después de resolver una conversación.
        Solo se envía UNA vez por conversación resuelta.
        """
        # Verificar que no existe ya una encuesta para esta conversación
        existing = await db.execute(
            select(SatisfactionSurvey).where(
                SatisfactionSurvey.conversation_id == conversation_id
            )
        )
        if existing.scalar_one_or_none():
            logger.info(f"CSAT ya existe para conversación {conversation_id}, saltando")
            return

        delay = delay_minutes or self.DEFAULT_DELAY_MINUTES

        # Programar envío con delay
        from app.tasks.csat_tasks import send_csat_survey
        send_csat_survey.apply_async(
            kwargs={
                "conversation_id": str(conversation_id),
                "contact_id": str(contact_id),
                "client_id": str(client_id),
                "channel": channel,
            },
            countdown=delay * 60,  # Convertir minutos a segundos
        )

    def build_survey_message(self, channel: str, contact_name: str = "") -> dict:
        """
        Construir mensaje de encuesta adaptado al canal.
        Retorna el payload específico para cada canal.
        """
        greeting = f"Hola{(' ' + contact_name) if contact_name else ''}"
        base_text = (
            f"{greeting}, tu conversación ha sido resuelta. "
            "¿Cómo calificarías tu experiencia? (1-5 estrellas)"
        )

        if channel in ("whatsapp", "telegram"):
            # Mensaje interactivo con botones
            return {
                "type": "interactive_buttons",
                "text": base_text,
                "buttons": [
                    {"id": "csat_1", "title": "1 - Muy mala"},
                    {"id": "csat_2", "title": "2 - Mala"},
                    {"id": "csat_3", "title": "3 - Regular"},
                    {"id": "csat_4", "title": "4 - Buena"},
                    {"id": "csat_5", "title": "5 - Excelente"},
                ],
            }
        elif channel == "email":
            # Template HTML con enlaces
            return {
                "type": "email_template",
                "subject": "¿Cómo fue tu experiencia?",
                "text": base_text,
                "template_data": {
                    "contact_name": contact_name,
                    "rating_links": {
                        i: f"{{base_url}}/api/v1/csat/respond?survey_id={{survey_id}}&rating={i}"
                        for i in range(1, 6)
                    },
                },
            }
        elif channel == "webchat":
            # Widget inline
            return {
                "type": "csat_widget",
                "text": base_text,
                "min_rating": 1,
                "max_rating": 5,
            }
        else:
            # Fallback: texto simple
            return {
                "type": "text",
                "text": f"{base_text}\n\nResponde con un número del 1 al 5.",
            }

    async def process_response(
        self,
        db: AsyncSession,
        survey_id: UUID = None,
        conversation_id: UUID = None,
        rating: int = None,
        comment: str = None,
        raw_response: str = None,
    ):
        """
        Procesar respuesta a encuesta CSAT.
        Puede venir como callback de botón, link de email o mensaje de texto.
        """
        # Buscar encuesta
        if survey_id:
            survey = await db.get(SatisfactionSurvey, survey_id)
        elif conversation_id:
            result = await db.execute(
                select(SatisfactionSurvey).where(
                    SatisfactionSurvey.conversation_id == conversation_id
                )
            )
            survey = result.scalar_one_or_none()

        if not survey:
            logger.warning(f"Encuesta CSAT no encontrada")
            return None

        if survey.status == "responded":
            logger.info(f"Encuesta CSAT ya respondida para conversación {survey.conversation_id}")
            return survey

        # Extraer rating de raw_response si viene de texto libre
        if rating is None and raw_response:
            rating = self._extract_rating_from_text(raw_response)

        if rating and 1 <= rating <= 5:
            survey.rating = rating
            survey.comment = comment
            survey.responded_at = datetime.utcnow()
            survey.status = "responded"
            await db.commit()

            # Emitir evento
            from app.core.events import EventEmitter
            await EventEmitter.emit("csat.responded", str(survey.client_id), {
                "survey_id": str(survey.id),
                "conversation_id": str(survey.conversation_id),
                "rating": rating,
                "comment": comment,
            })

            return survey

        return None

    def _extract_rating_from_text(self, text: str) -> Optional[int]:
        """Extraer rating numérico de respuesta de texto."""
        # Buscar "csat_N" (de callback de botón)
        import re
        match = re.search(r"csat_(\d)", text)
        if match:
            return int(match.group(1))

        # Buscar número simple del 1 al 5
        match = re.search(r"\b([1-5])\b", text.strip())
        if match:
            return int(match.group(1))

        return None
```

### 6. Tasks CSAT — `app/tasks/csat_tasks.py`

```python
# app/tasks/csat_tasks.py
from app.core.celery_app import celery_app
from app.services.csat import CSATService

@celery_app.task(queue="notifications")
async def send_csat_survey(
    conversation_id: str,
    contact_id: str,
    client_id: str,
    channel: str,
):
    """
    Enviar encuesta CSAT al contacto.
    Se ejecuta con delay después de resolver la conversación.
    """
    csat_service = CSATService()

    async with get_db_session() as db:
        # Verificar que la conversación sigue resuelta (no se reabrió)
        conversation = await db.get(Conversation, conversation_id)
        if not conversation or conversation.status != "resolved":
            logger.info(f"Conversación {conversation_id} ya no está resuelta, cancelando CSAT")
            return

        # Verificar que no existe ya una encuesta
        existing = await db.execute(
            select(SatisfactionSurvey).where(
                SatisfactionSurvey.conversation_id == conversation_id
            )
        )
        if existing.scalar_one_or_none():
            return

        # Obtener datos del contacto para personalización
        contact = await db.get(Contact, contact_id)
        contact_name = contact.first_name if contact else ""

        # Construir mensaje de encuesta
        survey_message = csat_service.build_survey_message(channel, contact_name)

        # Obtener channel_config para enviar por el canal correcto
        channel_config = await get_active_channel_config(db, client_id, channel)
        if not channel_config:
            logger.error(f"No hay canal activo {channel} para tenant {client_id}")
            return

        # Obtener el identifier del contacto para este canal
        identifier = await get_contact_identifier(db, contact_id, channel)
        if not identifier:
            logger.error(f"Contacto {contact_id} no tiene identifier para canal {channel}")
            return

        # Enviar
        provider = ProviderFactory.get_provider(channel, channel_config.provider_config)
        result = await provider.send_message(
            recipient_id=identifier.identifier_value,
            content=survey_message["text"],
            **survey_message,
        )

        # Registrar encuesta
        survey = SatisfactionSurvey(
            client_id=client_id,
            conversation_id=conversation_id,
            contact_id=contact_id,
            channel=channel,
            sent_at=datetime.utcnow(),
            survey_message_id=result.get("provider_message_id"),
            status="sent",
        )
        db.add(survey)
        await db.commit()

        logger.info(
            f"Encuesta CSAT enviada",
            extra={"conversation_id": conversation_id, "channel": channel},
        )


@celery_app.task(queue="maintenance")
async def expire_old_surveys():
    """
    Marcar encuestas sin respuesta como expiradas.
    Ejecutar diariamente via Celery Beat.
    """
    async with get_db_session() as db:
        expiry_threshold = datetime.utcnow() - timedelta(hours=48)
        await db.execute(
            update(SatisfactionSurvey)
            .where(
                SatisfactionSurvey.status == "sent",
                SatisfactionSurvey.sent_at < expiry_threshold,
            )
            .values(status="expired")
        )
        await db.commit()
```

### 7. Integración CSAT con resolución de conversación

Agregar hook en el servicio de conversaciones:

```python
# En app/services/conversation_service.py (al resolver conversación)
async def resolve_conversation(self, db, conversation_id, resolved_by):
    conversation = await db.get(Conversation, conversation_id)
    conversation.status = "resolved"
    conversation.resolved_at = datetime.utcnow()
    conversation.resolved_by = resolved_by
    await db.commit()

    # Emitir evento (para webhooks salientes)
    await EventEmitter.emit("conversation.resolved", str(conversation.client_id), {...})

    # Programar encuesta CSAT
    csat_service = CSATService()
    await csat_service.schedule_survey(
        db=db,
        conversation_id=conversation.id,
        contact_id=conversation.contact_id,
        client_id=conversation.client_id,
        channel=conversation.channel,
    )
```

### 8. Endpoint CSAT para respuesta vía link (email)

```python
# app/api/v1/csat.py
@router.get("/csat/respond")
async def respond_csat_via_link(
    survey_id: UUID = Query(...),
    rating: int = Query(..., ge=1, le=5),
    db: AsyncSession = Depends(get_db),
):
    """
    Endpoint para responder CSAT via link en email.
    Redirige a una página de agradecimiento.
    """
    csat_service = CSATService()
    survey = await csat_service.process_response(db, survey_id=survey_id, rating=rating)

    if survey:
        return RedirectResponse(url="/csat/thank-you")
    raise HTTPException(404, "Encuesta no encontrada o ya respondida")
```

### 9. Métricas CSAT — Endpoint resumen

```python
# En app/api/v1/admin.py
@router.get("/admin/csat/summary")
async def csat_summary(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "super_admin"])),
):
    """Resumen de CSAT para el tenant."""
    since = datetime.utcnow() - timedelta(days=days)

    result = await db.execute(
        select(
            func.count(SatisfactionSurvey.id).label("total_surveys"),
            func.count(SatisfactionSurvey.rating).label("responses"),
            func.avg(SatisfactionSurvey.rating).label("avg_rating"),
            func.count(case((SatisfactionSurvey.rating >= 4, 1))).label("promoters"),  # 4-5
            func.count(case((SatisfactionSurvey.rating <= 2, 1))).label("detractors"),  # 1-2
        )
        .where(
            SatisfactionSurvey.client_id == current_user.client_id,
            SatisfactionSurvey.sent_at >= since,
        )
    )
    row = result.one()

    # Distribución de ratings
    distribution = await db.execute(
        select(
            SatisfactionSurvey.rating,
            func.count(SatisfactionSurvey.id).label("count"),
        )
        .where(
            SatisfactionSurvey.client_id == current_user.client_id,
            SatisfactionSurvey.sent_at >= since,
            SatisfactionSurvey.rating.isnot(None),
        )
        .group_by(SatisfactionSurvey.rating)
        .order_by(SatisfactionSurvey.rating)
    )

    # Tendencia semanal
    trend = await db.execute(
        select(
            func.date_trunc("week", SatisfactionSurvey.responded_at).label("week"),
            func.avg(SatisfactionSurvey.rating).label("avg_rating"),
            func.count(SatisfactionSurvey.id).label("count"),
        )
        .where(
            SatisfactionSurvey.client_id == current_user.client_id,
            SatisfactionSurvey.responded_at >= since,
            SatisfactionSurvey.rating.isnot(None),
        )
        .group_by("week")
        .order_by("week")
    )

    total = row.total_surveys or 0
    responses = row.responses or 0
    response_rate = (responses / total * 100) if total > 0 else 0

    return {
        "period_days": days,
        "total_surveys_sent": total,
        "total_responses": responses,
        "response_rate": round(response_rate, 1),
        "average_rating": round(float(row.avg_rating or 0), 2),
        "promoters": row.promoters or 0,
        "detractors": row.detractors or 0,
        "distribution": {r.rating: r.count for r in distribution},
        "weekly_trend": [
            {"week": t.week.isoformat(), "avg_rating": round(float(t.avg_rating), 2), "count": t.count}
            for t in trend
        ],
    }
```

### 10. Dashboard CSAT en Grafana

Agregar métricas Prometheus para CSAT:

```python
# En app/core/telemetry.py
csat_score = meter.create_histogram(
    name="csat_score",
    description="Puntuación CSAT",
)
csat_surveys_sent = meter.create_counter(
    name="csat_surveys_sent_total",
    description="Encuestas CSAT enviadas",
)
csat_responses = meter.create_counter(
    name="csat_responses_total",
    description="Respuestas CSAT recibidas",
)
```

Dashboard `grafana/dashboards/csat.json` con paneles:
- **CSAT Score Promedio** (stat panel, último valor)
- **Distribución de Ratings** (bar chart 1-5)
- **CSAT por Canal** (bar chart agrupado)
- **Tendencia Semanal** (time series)
- **Tasa de Respuesta** (gauge, porcentaje)

Alerta:
```yaml
# En prometheus/alerts.yml (agregar)
- alert: LowCSATScore
  expr: avg(csat_score) < 3.0
  for: 7d
  labels:
    severity: warning
  annotations:
    summary: "CSAT promedio < 3.0 en la última semana"
```

### 11. Schemas Pydantic — `app/schemas/webhook_config.py`

```python
# app/schemas/webhook_config.py
from pydantic import BaseModel, HttpUrl
from typing import Optional

class WebhookCreate(BaseModel):
    url: str  # HttpUrl en prod, str en dev para localhost
    events: list[str]
    secret: Optional[str] = None  # Auto-generado si no se proporciona
    description: Optional[str] = None
    headers: Optional[dict[str, str]] = None

class WebhookUpdate(BaseModel):
    url: Optional[str] = None
    events: Optional[list[str]] = None
    secret: Optional[str] = None
    description: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    is_active: Optional[bool] = None

class WebhookResponse(BaseModel):
    id: str
    url: str
    events: list[str]
    is_active: bool
    description: Optional[str]
    consecutive_failures: int
    last_triggered_at: Optional[str]
    last_success_at: Optional[str]
    last_failure_at: Optional[str]
    disabled_reason: Optional[str]
    created_at: str

    class Config:
        from_attributes = True
```

### 12. Tests

**12.1 Tests del dispatcher — `tests/unit/test_webhook_dispatcher.py`**

```python
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.services.webhook_dispatcher import WebhookDispatcher

class TestWebhookDispatcher:
    @pytest.fixture
    def dispatcher(self):
        return WebhookDispatcher()

    def test_compute_signature(self, dispatcher):
        """Verificar que la firma HMAC se calcula correctamente."""
        body = '{"event":"test"}'
        secret = "test-secret"
        timestamp = "1700000000"
        signature = dispatcher.compute_signature(body, secret, timestamp)
        assert len(signature) == 64  # SHA-256 hex
        # Verificar que es determinística
        assert signature == dispatcher.compute_signature(body, secret, timestamp)
        # Verificar que cambia con diferente secret
        assert signature != dispatcher.compute_signature(body, "other-secret", timestamp)

    @pytest.mark.asyncio
    async def test_send_webhook_success(self, dispatcher):
        """Envío exitoso retorna success=True."""
        webhook = MagicMock(url="https://example.com/webhook", secret="secret", headers={})
        payload = {"event": "test", "data": {}}

        with patch("httpx.AsyncClient") as mock_client:
            mock_response = MagicMock(status_code=200, text="OK")
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.post = AsyncMock(return_value=mock_response)

            result = await dispatcher.send_webhook(webhook, payload)

        assert result["success"] is True
        assert result["status_code"] == 200

    @pytest.mark.asyncio
    async def test_send_webhook_failure(self, dispatcher):
        """Fallo HTTP retorna success=False con error."""
        webhook = MagicMock(url="https://example.com/webhook", secret="secret", headers={})
        payload = {"event": "test", "data": {}}

        with patch("httpx.AsyncClient") as mock_client:
            mock_response = MagicMock(status_code=500, text="Internal Server Error")
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.post = AsyncMock(return_value=mock_response)

            result = await dispatcher.send_webhook(webhook, payload)

        assert result["success"] is False
        assert result["status_code"] == 500

    @pytest.mark.asyncio
    async def test_send_webhook_timeout(self, dispatcher):
        """Timeout retorna success=False."""
        webhook = MagicMock(url="https://example.com/webhook", secret="secret", headers={})
        payload = {"event": "test", "data": {}}

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.post = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))

            result = await dispatcher.send_webhook(webhook, payload)

        assert result["success"] is False
        assert "Timeout" in result["error"]

    @pytest.mark.asyncio
    async def test_signature_headers_present(self, dispatcher):
        """Verificar que los headers de firma están presentes en el request."""
        webhook = MagicMock(url="https://example.com/webhook", secret="secret", headers={})
        payload = {"event": "message.received", "data": {}}

        with patch("httpx.AsyncClient") as mock_client:
            mock_response = MagicMock(status_code=200, text="OK")
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.post = AsyncMock(return_value=mock_response)

            await dispatcher.send_webhook(webhook, payload)

            call_kwargs = mock_client.return_value.post.call_args
            headers = call_kwargs.kwargs["headers"]
            assert "X-Webhook-Signature" in headers
            assert headers["X-Webhook-Signature"].startswith("sha256=")
            assert "X-Webhook-Event" in headers
            assert "X-Webhook-Timestamp" in headers
```

**12.2 Tests CSAT — `tests/unit/test_csat.py`**

```python
import pytest
from app.services.csat import CSATService

class TestCSATService:
    @pytest.fixture
    def csat_service(self):
        return CSATService()

    def test_build_survey_message_whatsapp(self, csat_service):
        """WhatsApp: mensaje con botones interactivos."""
        message = csat_service.build_survey_message("whatsapp", "Juan")
        assert message["type"] == "interactive_buttons"
        assert len(message["buttons"]) == 5
        assert "Juan" in message["text"]

    def test_build_survey_message_email(self, csat_service):
        """Email: template HTML con links."""
        message = csat_service.build_survey_message("email", "María")
        assert message["type"] == "email_template"
        assert "subject" in message
        assert "rating_links" in message["template_data"]

    def test_build_survey_message_webchat(self, csat_service):
        """Webchat: widget inline."""
        message = csat_service.build_survey_message("webchat")
        assert message["type"] == "csat_widget"
        assert message["min_rating"] == 1
        assert message["max_rating"] == 5

    def test_extract_rating_from_button(self, csat_service):
        """Extraer rating de callback de botón."""
        assert csat_service._extract_rating_from_text("csat_4") == 4
        assert csat_service._extract_rating_from_text("csat_1") == 1

    def test_extract_rating_from_number(self, csat_service):
        """Extraer rating de respuesta numérica."""
        assert csat_service._extract_rating_from_text("5") == 5
        assert csat_service._extract_rating_from_text("3") == 3

    def test_extract_rating_invalid(self, csat_service):
        """No extraer rating de texto no numérico."""
        assert csat_service._extract_rating_from_text("excelente") is None
        assert csat_service._extract_rating_from_text("") is None

    @pytest.mark.asyncio
    async def test_survey_not_duplicated(self, csat_service, db, test_conversation_resolved):
        """Solo se envía UNA encuesta por conversación."""
        # Primera vez: programar encuesta
        await csat_service.schedule_survey(
            db, test_conversation_resolved.id,
            test_conversation_resolved.contact_id,
            test_conversation_resolved.client_id,
            "whatsapp",
        )
        # Segunda vez: no debería crear otra
        # (Verificar que no se duplica)
```

**12.3 Test de flujo integrado — `tests/integration/test_webhook_flow.py`**

```python
@pytest.mark.integration
class TestWebhookFlow:
    async def test_conversation_resolved_triggers_webhook(self, db, test_tenant, test_webhook):
        """Al resolver conversación → webhook saliente enviado."""
        # 1. Configurar webhook suscrito a conversation.resolved
        # 2. Resolver conversación
        # 3. Verificar que el webhook fue enviado (via mock o log)
        pass

    async def test_retry_on_failure(self, db, test_tenant, test_webhook):
        """Primer intento falla, segundo éxito."""
        # Mock: primera respuesta 500, segunda 200
        pass

    async def test_auto_disable_after_10_failures(self, db, test_tenant, test_webhook):
        """Webhook se desactiva tras 10 fallos consecutivos."""
        pass

    async def test_csat_sent_after_resolution(self, db, test_tenant, test_conversation):
        """CSAT enviado automáticamente después de resolver conversación."""
        pass
```

## Criterios de Aceptación

| # | Criterio | Verificación |
|---|---|---|
| 1 | Webhook saliente enviado con HMAC válido | POST al URL del webhook con header X-Webhook-Signature verificable |
| 2 | Retry con backoff funciona | Mock: 1er intento falla → reintenta a los 5s → éxito |
| 3 | Desactivación automática tras 10 fallos | 10 envíos fallidos → is_active = false, disabled_reason = "auto_disabled_failures" |
| 4 | Reactivación manual funciona | PUT /webhooks/outgoing/{id} con is_active=true → resetea contador |
| 5 | Endpoint de test envía evento de prueba | POST /webhooks/outgoing/{id}/test → evento recibido en URL |
| 6 | CSAT enviado automáticamente | Resolver conversación → 5 min después → encuesta enviada por canal correcto |
| 7 | CSAT solo una vez por conversación | Resolver misma conversación dos veces → solo 1 encuesta |
| 8 | CSAT registrado correctamente | Rating 1-5 almacenado en satisfaction_surveys |
| 9 | Resumen CSAT con métricas reales | GET /admin/csat/summary → avg_rating, distribución, tendencia |
| 10 | Dashboard CSAT en Grafana | Panel visible con datos reales |
| 11 | Webhooks NO bloquean flujo principal | Envío via Celery cola 'notifications', no síncrono |
| 12 | Logs de webhook consultables | GET /webhooks/outgoing/{id}/logs → historial de envíos |

## Notas Técnicas

- **HMAC-SHA256**: La firma se calcula sobre `timestamp + "." + body` (no solo body) para prevenir replay attacks. El receptor debe verificar que el timestamp no tiene más de 5 minutos de antigüedad.
- **Webhooks NO bloquean**: Todo el envío es via Celery (cola `notifications`). El flujo principal emite el evento y continúa. Si Celery está caído, los eventos se encolan en Redis y se procesan cuando se recupere.
- **CSAT delay**: El delay de 5 minutos (configurable por tenant) evita enviar la encuesta inmediatamente, dando tiempo al contacto para procesar la resolución. Si la conversación se reabre antes del envío, la encuesta se cancela.
- **Una encuesta por conversación**: Constraint UNIQUE en `conversation_id` garantiza que no se duplique a nivel de base de datos. La verificación en código es una protección adicional.
- **Desactivación automática**: 10 fallos consecutivos desactivan el webhook para proteger contra URLs muertas que generan tráfico inútil. El admin puede reactivar manualmente.
- **Eventos de CSAT como webhooks**: Los eventos `csat.responded` NO se envían como webhooks salientes por defecto (no están en SUPPORTED_EVENTS). Se pueden agregar si un tenant lo solicita.

## Dependencias

| Dependencia | Versión | Propósito |
|---|---|---|
| httpx | >=0.27.0 | HTTP client async para enviar webhooks |
| Celery | (existente) | Cola 'notifications' para envío async |
| Redis | (existente) | Cola de tareas Celery |
| Grafana | (existente) | Dashboard CSAT |
| Prometheus | (existente) | Métricas CSAT |
