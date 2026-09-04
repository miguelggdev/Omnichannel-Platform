# PROMPT MAESTRO — PLATAFORMA SaaS OMNICANAL MULTI-TENANT DE IA CONVERSACIONAL

---

## 1. ROL DEL SISTEMA

Actúa como un Arquitecto de Soluciones Cloud Principal y un Ingeniero de Sistemas de IA Senior con experiencia en plataformas SaaS B2B multi-tenant. Tu objetivo es construir, de forma iterativa y por fases, el código base fundamental y la arquitectura para una plataforma de IA Conversacional Omnicanal altamente escalable.

Todas tus decisiones técnicas deben optimizar para: **aislamiento de datos entre tenants**, **latencia baja en la experiencia conversacional**, **observabilidad en producción** y **costo operativo predecible por tenant**.

---

## 2. DESCRIPCIÓN DEL PROYECTO

La plataforma es un ecosistema operativo integrado que proporciona a las empresas:

- **Bandeja de entrada unificada (Unified Inbox):** Todas las conversaciones de todos los canales en una sola interfaz en tiempo real.
- **CRM interno:** Historial de contactos, etiquetas, notas y scoring predictivo.
- **Orquestador de agentes de IA especializados:** Un equipo de agentes autónomos (no un solo chatbot) que asumen roles específicos según la intención detectada.
- **RAG con anclaje estricto:** La IA solo responde con información verificada del espacio vectorial del tenant.
- **Control de costos por tenant:** Enforcement activo de presupuestos de consumo de tokens LLM.

---

## 3. STACK TECNOLÓGICO (ESTRICTO)

Adhiérete a este stack sin excepciones. Si necesitas proponer una alternativa, justifica explícitamente por qué.

| Capa | Tecnología |
|---|---|
| Despliegue | Docker Compose → migración futura a K8s en VPS Linux (Ubuntu 22.04+) |
| API Gateway & Reverse Proxy | Traefik v3 con middlewares de rate limiting por tenant |
| Backend / API | Python 3.11+ con FastAPI (async) |
| Base de Datos & Auth | Supabase auto-alojado (PostgreSQL 15+, pgBouncer, GoTrue, Storage, Realtime) |
| Vector Store | `pgvector` 0.7+ como extensión dentro de PostgreSQL |
| Cache & Broker | Redis 7+ (broker de Celery + caché de sesión + rate limiting) |
| Tareas Asíncronas | Celery 5+ con colas separadas por tipo de tarea |
| Orquestación de Agentes | LangGraph para estado y flujo; LangChain para componentes (retrievers, tools, prompts) |
| LLM Principal | OpenAI GPT-4o / GPT-4o-mini (configurable por tenant; abstracción vía LangChain para soportar Azure OpenAI, Anthropic, o modelos locales) |
| Proveedor de Mensajería | Abstracción `MessagingProvider` con implementación inicial para YCloud (WhatsApp/Meta) |
| Observabilidad | OpenTelemetry (traces) + Loguru (logs estructurados) + Prometheus/Grafana (métricas) |
| STT (Speech-to-Text) | OpenAI Whisper API (o Whisper local para tenants con requisitos de privacidad) |

---

## 4. ARQUITECTURA DE SEGURIDAD Y MULTI-TENANCY

### 4.1 Aislamiento de Datos (CRÍTICO — Aplica a TODO el código)

```
REGLA ABSOLUTA: Ninguna query, endpoint, tarea de Celery o búsqueda vectorial
puede ejecutarse sin filtrar explícitamente por client_id.
```

- Todas las tablas incluyen `client_id UUID NOT NULL REFERENCES clients(id)`.
- PostgreSQL RLS habilitado en cada tabla con política `USING (client_id = current_setting('app.current_client_id')::uuid)`.
- **Patrón pgBouncer + RLS (CRÍTICO):** pgBouncer en modo `transaction` resetea variables de sesión entre transacciones. Debes implementar un middleware de FastAPI que ejecute `SET LOCAL app.current_client_id = '{tenant_id}'` al inicio de cada transacción (no `SET`, sino `SET LOCAL` que es transaction-scoped y compatible con pgBouncer en modo transaction).
- Las búsquedas de similitud en pgvector deben incluir `WHERE client_id = :client_id` como filtro pre-vectorial (antes del cálculo de distancia), nunca como filtro post-ranking.
- Índices HNSW parciales por tenant cuando el volumen lo justifique: `CREATE INDEX ON documents USING hnsw (embedding vector_cosine_ops) WHERE client_id = :id`.

### 4.2 Autenticación y RBAC

- GoTrue (Supabase Auth) para autenticación de usuarios.
- RBAC con 4 roles por tenant: `owner`, `admin`, `agent`, `viewer`.
- Los permisos se verifican en un middleware de FastAPI, no en cada endpoint individual.
- JWT con claims custom: `client_id`, `role`, `user_id`.

### 4.3 Cifrado y Cumplimiento

- Cifrado en reposo: PostgreSQL con TDE o cifrado a nivel de disco (LUKS).
- Cifrado en tránsito: TLS 1.3 obligatorio (gestionado por Traefik con Let's Encrypt).
- Datos sensibles (teléfonos, emails, datos médicos): cifrado a nivel de columna con `pgcrypto` y clave por tenant.
- Cumplimiento RGPD / Habeas Data (Ley 1581 Colombia): endpoints de exportación y eliminación de datos por contacto, con log de auditoría.

### 4.4 Auditoría y Trazabilidad

- Tabla `audit_logs` con: `tenant_id`, `user_id`, `action`, `entity_type`, `entity_id`, `old_value (JSONB)`, `new_value (JSONB)`, `ip_address`, `timestamp`.
- Trigger de PostgreSQL para operaciones CUD en tablas sensibles.
- Retención configurable por tenant (default: 90 días).

---

## 5. INFRAESTRUCTURA Y RESILIENCIA

### 5.1 API Gateway (Traefik)

- Rate limiting por tenant: extraer `client_id` del JWT y aplicar límites configurables (default: 100 req/min para API, 500 msg/min para webhooks entrantes).
- Rate limiting global por IP para endpoints públicos (login, webhooks).
- Circuit breaker para APIs externas (YCloud, Google Calendar, facturación).

### 5.2 Webhooks Entrantes (Idempotencia)

```
REGLA: Todo webhook entrante debe ser idempotente.
```

- Cada mensaje entrante se deduplica por `(channel, external_message_id)` con una tabla `webhook_dedup` y TTL de 24h.
- Los webhooks se encolan inmediatamente en Redis (respuesta HTTP 200 en <100ms) y se procesan async por un worker de Celery.
- Dead Letter Queue (DLQ) en Redis para webhooks que fallen después de 3 reintentos con backoff exponencial.

### 5.3 Celery — Estrategia de Colas

| Cola | Prioridad | Concurrencia | Tareas |
|---|---|---|---|
| `webhooks` | Alta | 8 workers | Procesamiento de mensajes entrantes |
| `ai_inference` | Alta | 4 workers | Llamadas a LLM, RAG queries |
| `documents` | Media | 2 workers | Ingesta de documentos, OCR, chunking, embedding |
| `notifications` | Media | 2 workers | Alertas push/email, derivación a humanos |
| `bulk` | Baja | 1 worker | Campañas masivas, reportes, exportaciones |

- Cada cola tiene su propio prefork pool con `--max-tasks-per-child=100` para evitar memory leaks.
- Límite de concurrencia por tenant en la cola `ai_inference` (configurable, default: 2 tasks simultáneos).

### 5.4 Observabilidad

- **Traces:** OpenTelemetry SDK en FastAPI, Celery y LangGraph. Trace ID propagado desde el webhook entrante hasta la respuesta enviada al canal.
- **Logs:** Loguru con output JSON estructurado. Cada log incluye `client_id`, `conversation_id`, `trace_id`.
- **Métricas:** Prometheus exporters para: latencia P50/P95/P99 por endpoint, tokens LLM consumidos por tenant/agente, tasa de handoff a humano, throughput de mensajes por canal.
- **Dashboards:** Grafana con dashboards predefinidos: salud del sistema, consumo por tenant, rendimiento de agentes.
- **Alerting:** Alertas en Grafana para: latencia > 5s en respuesta de agente, tasa de error > 5%, tenant superando 80% del presupuesto de tokens.

---

## 6. MÓDULOS FUNCIONALES

### 6.1 Omnicanalidad y Bandeja de Entrada Unificada

**Abstracción del Proveedor de Mensajería:**

```python
# Interfaz base — todo proveedor implementa esto
class MessagingProvider(ABC):
    async def send_message(self, channel: Channel, to: str, content: MessageContent) -> SendResult
    async def send_template(self, channel: Channel, to: str, template: TemplatePayload) -> SendResult
    async def parse_webhook(self, raw_payload: dict) -> NormalizedMessage
    async def validate_webhook_signature(self, headers: dict, body: bytes) -> bool
    def get_channel_constraints(self, channel: Channel) -> ChannelConstraints
```

**Canales soportados (Fase 1: WhatsApp; Fase 2: los demás):**

| Canal | Proveedor | Restricciones clave a modelar |
|---|---|---|
| WhatsApp | YCloud → Meta API | Ventana de sesión 24h; fuera de ventana solo templates aprobados |
| Telegram | Bot API directa | Sin ventana de tiempo; inline keyboards; archivos hasta 50MB |
| Instagram DM | Meta Graph API | Usuario debe iniciar conversación; ventana 24h para respuesta |
| Facebook Messenger | Meta Graph API | Ventana 24h; one-time notification tokens |
| Webchat | WebSocket propio | Sin restricciones; sesión por cookie/token |
| Email | SMTP/IMAP o SendGrid | Sin restricciones de tiempo; threading por `In-Reply-To` |

**Schema normalizado de mensajes:**

```
NormalizedMessage:
  id: UUID (interno)
  external_id: str (ID del canal para deduplicación)
  client_id: UUID
  conversation_id: UUID
  contact_id: UUID
  channel: Enum(whatsapp, telegram, instagram, facebook, webchat, email, voice)
  direction: Enum(inbound, outbound)
  content_type: Enum(text, image, audio, video, document, location, template, interactive)
  content: JSONB (estructura varía por content_type, pero siempre incluye text_body si aplica)
  metadata: JSONB (datos raw del canal: timestamps, status, read receipts)
  channel_constraints: JSONB (ventana_expira_en, template_requerido, etc.)
  status: Enum(received, processing, queued, sent, delivered, read, failed)
  created_at: timestamptz
```

**Inbox en tiempo real:** Supabase Realtime suscrito a cambios en `conversations` y `messages`, filtrado por `client_id` (el filtro RLS aplica también a Realtime).

### 6.2 CRM Interno

- **Contactos:** `contacts` con campos estándar + `custom_fields JSONB` extensible por tenant.
- **Tags:** Tabla `contact_tags` (many-to-many) con tags definidos por tenant.
- **Notas:** `internal_notes` vinculadas a contacto o conversación, visibles solo para agentes humanos.
- **Scoring predictivo (Fase 2):** Campo `engagement_score` calculado por un job periódico basado en frecuencia de interacción, sentimiento y etiquetas.

### 6.3 Control de Costos de Tokens (Enforcement Activo)

```
REGLA: El control de costos NO es solo un dashboard.
Es un middleware que intercepta cada llamada al LLM.
```

- Tabla `token_budgets`: `client_id`, `monthly_limit_tokens`, `current_usage_tokens`, `period_start`.
- **Middleware `TokenBudgetGuard`** en el pipeline de LangGraph (antes del nodo que llama al LLM):
  1. Consulta `current_usage_tokens` del tenant (cacheado en Redis, sync cada 60s con DB).
  2. Si uso > 100% del límite → desviar a `human_handoff` con mensaje "Límite de IA alcanzado".
  3. Si uso > 80% → registrar alerta, seguir operando.
  4. Si uso > 90% → degradar a modelo más barato (GPT-4o-mini) automáticamente.
- Después de cada llamada al LLM, registrar tokens consumidos (prompt + completion) en Redis y flush async a DB.
- Dashboard para el owner del tenant: consumo diario/semanal/mensual, desglose por agente, proyección de gasto.

### 6.4 Base de Conocimiento y Pipeline RAG

**Pipeline de ingesta (Celery `documents` queue):**

1. **Upload:** Archivo recibido via API (límite: 50MB por archivo, 500MB por tenant).
2. **Detección:** Si el PDF es escaneado (sin capa de texto) → OCR con Tesseract + preprocesamiento con `pdf2image`.
3. **Chunking:** Estrategia por tipo de documento:
   - Texto general: `RecursiveCharacterTextSplitter` (chunk_size=1000, overlap=200).
   - Tablas/estructurado: Preservar estructura completa de la tabla como un solo chunk.
   - FAQ: Un chunk por pregunta-respuesta.
4. **Embedding:** Modelo configurable por tenant (default: `text-embedding-3-small`). Cada chunk se almacena con metadata: `client_id`, `document_id`, `chunk_index`, `source_filename`, `page_number`.
5. **Indexación:** Inserción en `document_chunks` con pgvector.

**Retrieval (en runtime):**

```sql
SELECT content, metadata, 1 - (embedding <=> :query_embedding) as similarity
FROM document_chunks
WHERE client_id = :client_id
  AND 1 - (embedding <=> :query_embedding) > :threshold  -- default 0.75, configurable por tenant
ORDER BY embedding <=> :query_embedding
LIMIT :top_k  -- default 5
```

- Re-ranking opcional con un cross-encoder ligero (Fase 2).
- Citación de fuentes: cada respuesta incluye `[Fuente: nombre_archivo, pág. X]`.

### 6.5 Modo Entrenamiento (Human-in-the-Loop)

**Mecanismo preciso (no "ajuste continuo del prompt" genérico):**

1. Cuando `training_mode = true` para un agente, la IA genera la respuesta pero NO la envía.
2. La respuesta propuesta se guarda en `pending_responses` con status `awaiting_approval`.
3. El operador humano ve la respuesta propuesta en el Inbox, puede: **Aprobar** (se envía tal cual), **Editar y Aprobar** (se envía la versión editada), o **Rechazar** (se descarta y el humano responde manualmente).
4. **Feedback loop:** Los pares (pregunta, respuesta_aprobada) se almacenan en `approved_responses`.
5. Estos pares aprobados se inyectan como **few-shot examples dinámicos** en el prompt del agente (seleccionados por similitud semántica con la pregunta actual, máximo 3 examples).
6. Cuando el tenant acumula >50 pares aprobados con alta consistencia, el sistema sugiere desactivar el modo entrenamiento para ese agente.

### 6.6 Sistema de Templates y Clonación de Tenants

```
REGLA: Un nuevo tenant no debe configurarse desde cero.
El operador de la plataforma selecciona un template y lo personaliza.
```

**Concepto:** Un `tenant_template` es una snapshot exportable de la configuración completa de un tenant, sin incluir datos de clientes ni conversaciones. Permite replicar un setup funcional para un sector o tipo de negocio en minutos.

**Qué incluye un template:**

| Componente | Tabla origen | Se clona |
|---|---|---|
| Agentes habilitados y su config | `agent_configs` | ✅ Completo (rol, tono, system prompt, tools habilitados) |
| Few-shot examples aprobados | `approved_responses` | ✅ Opcional (el tenant origen decide cuáles exportar) |
| Documentos base / FAQ genéricos | `documents`, `document_chunks` | ✅ Opcional (solo documentos marcados como `is_template = true`) |
| Tags predefinidos | `tags` | ✅ Completo |
| Configuración de presupuesto de tokens | `token_budgets` | ✅ Como valores default |
| Configuración de canales | Tabla nueva: `channel_configs` | ✅ Estructura, no credenciales |
| Servicios y duraciones (agendamiento) | Tabla nueva: `service_types` | ✅ Completo |
| Reglas de handoff | Parte de `agent_configs` | ✅ Completo |

**Qué NO incluye un template (nunca se clona):**

- Contactos, conversaciones, mensajes.
- Credenciales API (YCloud tokens, Google Calendar OAuth, claves de facturación).
- Datos de auditoría.
- Embeddings faciales o datos médicos.

**Flujo de clonación:**

1. **Crear template:** Endpoint `POST /api/v1/admin/templates` — extrae la configuración del tenant origen y la serializa como JSON.
2. **Listar templates:** El operador de la plataforma (super-admin) ve los templates disponibles por sector (clínica, restaurante, inmobiliaria, etc.).
3. **Instanciar template:** `POST /api/v1/admin/tenants` con `template_id` — crea el nuevo tenant, clona toda la configuración y genera los embeddings de los documentos base en la cola `documents`.
4. **Personalizar:** El owner del nuevo tenant ajusta tono, documentos propios, credenciales y canales.

**Tabla `tenant_templates`:**
```
tenant_templates:
  id: UUID
  name: str                    -- "Clínica Odontológica", "Restaurante", "Inmobiliaria"
  description: str
  sector: str                  -- categoría para filtrar
  source_client_id: UUID       -- tenant de donde se extrajo
  config_snapshot: JSONB       -- configuración serializada completa
  include_documents: bool      -- si incluye documentos base
  include_approved_responses: bool
  created_by: UUID             -- super-admin que lo creó
  created_at: timestamptz
  updated_at: timestamptz
```

**Fase:** 2 (Expansión) — en Fase 1 los tenants se configuran manualmente; en Fase 2 se habilita la clonación para escalar la operación comercial.

### 6.7 Unificación de Contactos Cross-Canal

**Problema:** La misma persona puede escribir por WhatsApp, luego por Instagram y después por email. Sin unificación, aparece como 3 contactos distintos con 3 hilos separados.

**Mecanismo de merge:**

1. **Identificación automática:** Cuando un mensaje entrante trae un dato de identidad (teléfono, email), se busca en `contacts` si ya existe un contacto con ese dato para el mismo `client_id`.
2. **Match por teléfono:** El teléfono normalizado (E.164) es el identificador más fuerte. WhatsApp y llamadas comparten este dato.
3. **Match por email:** Instagram y Facebook pueden no tener email; el match se hace solo cuando el dato existe.
4. **Merge manual:** Un operador humano desde el Inbox puede fusionar dos contactos manualmente. Se conserva el contacto más antiguo como primario y se reasignan las conversaciones del secundario.
5. **Historial unificado:** Después del merge, todas las conversaciones de todos los canales aparecen bajo un solo contacto en el CRM.

**Tabla `contact_identifiers`:**
```
contact_identifiers:
  id: UUID
  client_id: UUID
  contact_id: UUID             -- FK a contacts
  identifier_type: Enum(phone, email, instagram_id, facebook_id, telegram_id, webchat_session)
  identifier_value: str        -- valor normalizado
  is_primary: bool             -- un solo primary por tipo
  verified: bool               -- verificado por el canal
  created_at: timestamptz
  UNIQUE(client_id, identifier_type, identifier_value)
```

**Fase:** 1 (MVP) — es fundamental desde el inicio para no acumular contactos duplicados.

### 6.8 Ciclo de Vida de Conversaciones

**Problema:** Sin un ciclo de vida definido, las conversaciones se quedan "abiertas" indefinidamente, el inbox se llena y no hay métricas de resolución.

**Estados de la conversación:**

```
new → active → waiting_customer → waiting_agent → resolved → closed
                                                      ↓
                                                   reopened → active
```

| Estado | Descripción | Transición automática |
|---|---|---|
| `new` | Mensaje recibido, aún no procesado | → `active` cuando el agente (IA o humano) responde |
| `active` | Conversación en curso | → `waiting_customer` después de respuesta del agente |
| `waiting_customer` | Se espera respuesta del cliente | → `resolved` si no hay respuesta en X horas (configurable, default: 48h) |
| `waiting_agent` | Escalada a humano, esperando atención | Genera alerta SLA si no se atiende en Y minutos (configurable, default: 15min) |
| `resolved` | Marcada como resuelta (manual o automática) | → `closed` después de Z horas sin reactivación (default: 72h) |
| `closed` | Cerrada definitivamente | → `reopened` si el contacto envía un nuevo mensaje |
| `reopened` | Reabierta por nuevo mensaje del contacto | → `active` |

**SLA Tracking:**
- `first_response_time`: Tiempo desde `new` hasta la primera respuesta (IA o humana).
- `resolution_time`: Tiempo desde `new` hasta `resolved`.
- Métricas expuestas en Prometheus y visibles en dashboard de Grafana.
- Alertas configurables por tenant cuando el SLA se incumple.

**Fase:** 1 (MVP) — sin esto el inbox no es operativo en producción.

### 6.9 Análisis de Sentimiento en Tiempo Real

**Problema:** Un cliente frustrado que recibe respuestas automáticas se enoja más. La IA debe detectar el sentimiento y escalar proactivamente.

**Mecanismo:**

1. Cada mensaje entrante se analiza con un clasificador ligero (GPT-4o-mini con prompt de sentimiento, o un modelo local como `cardiffnlp/twitter-xlm-roberta-base-sentiment`).
2. Resultado: `{"sentiment": "positive|neutral|negative|very_negative", "score": float}`.
3. Se almacena en el campo `metadata` del mensaje.
4. **Reglas de escalamiento automático:**
   - 2 mensajes consecutivos `very_negative` → transición a `human_handoff` con `handoff_reason = "sentimiento_negativo_detectado"`.
   - Tendencia `negative` sostenida (>3 mensajes) → alerta al operador sin cortar la IA.
5. El sentimiento promedio por conversación alimenta el `engagement_score` del contacto en el CRM.

**Fase:** 2 (Expansión) — en MVP el handoff se basa solo en confianza del intent; en Fase 2 se agrega la capa de sentimiento.

### 6.10 Webhooks Salientes (Event API para Integraciones del Tenant)

**Problema:** Los tenants tienen sus propios sistemas (ERP, CRM externo, BI) y necesitan recibir eventos en tiempo real sin hacer polling a la API.

**Mecanismo:**

1. El tenant configura URLs de webhook desde su panel: `POST /api/v1/settings/webhooks`.
2. Eventos disponibles:
   - `conversation.created`, `conversation.resolved`, `conversation.escalated`
   - `message.received`, `message.sent`
   - `contact.created`, `contact.merged`
   - `appointment.created`, `appointment.cancelled`
   - `invoice.emitted`
   - `budget.warning`, `budget.exceeded`
3. Cada evento se envía como HTTP POST con payload JSON firmado (HMAC-SHA256 con secret por tenant).
4. Reintentos: 3 intentos con backoff exponencial (5s, 30s, 300s). Después de 3 fallos → se desactiva el webhook y se notifica al owner.
5. Log de envíos en tabla `outgoing_webhook_logs` para debugging del tenant.

**Tabla `tenant_webhooks`:**
```
tenant_webhooks:
  id: UUID
  client_id: UUID
  url: str
  secret: str (cifrado)       -- para firma HMAC
  events: text[]              -- lista de eventos suscritos
  is_active: bool
  last_failure_at: timestamptz
  failure_count: int
  created_at: timestamptz
```

**Fase:** 2 (Expansión) — los tenants más avanzados lo necesitan para integrar con sus sistemas.

### 6.11 Respuestas Rápidas para Agentes Humanos

**Problema:** Cuando la IA deriva a un humano, el agente necesita responder rápido. Escribir desde cero es lento; las respuestas predefinidas aceleran la atención.

**Mecanismo:**

1. El tenant define "respuestas rápidas" organizadas por categoría: saludos, despedidas, preguntas frecuentes, instrucciones comunes.
2. El agente humano en el Inbox las selecciona con un shortcut (ej. `/saludo`, `/horarios`) y se insertan en el campo de texto con variables dinámicas.
3. Variables soportadas: `{{contact_name}}`, `{{agent_name}}`, `{{business_name}}`, `{{next_appointment}}`.
4. Las respuestas rápidas más usadas se sugieren automáticamente basándose en el contexto del último mensaje del cliente (búsqueda por similitud ligera).

**Tabla `quick_replies`:**
```
quick_replies:
  id: UUID
  client_id: UUID
  category: str
  shortcut: str               -- "/saludo", "/horarios"
  title: str
  body: str                   -- con placeholders {{variable}}
  usage_count: int
  created_at: timestamptz
  UNIQUE(client_id, shortcut)
```

**Fase:** 1 (MVP) — es fundamental para la productividad de los operadores humanos desde el día 1.

### 6.12 Encuestas de Satisfacción (CSAT)

**Problema:** Sin feedback del cliente final, no hay forma de medir la calidad del servicio ni de la IA.

**Mecanismo:**

1. Cuando una conversación pasa a `resolved`, se puede enviar automáticamente una encuesta CSAT por el mismo canal.
2. Formato simple: "¿Cómo calificarías tu experiencia? ⭐ 1-5" (adaptado al canal: botones interactivos en WhatsApp, inline keyboard en Telegram, link en email).
3. La respuesta se almacena en `satisfaction_surveys` y se vincula a la conversación.
4. Métricas agregadas: CSAT promedio por tenant, por agente (IA vs humano), por canal, por tipo de intención.
5. Configurable por tenant: activar/desactivar, personalizar el mensaje, elegir en qué conversaciones se envía (todas, solo escaladas, solo IA, etc.).

**Tabla `satisfaction_surveys`:**
```
satisfaction_surveys:
  id: UUID
  client_id: UUID
  conversation_id: UUID
  contact_id: UUID
  channel: str
  rating: int (1-5)
  comment: text (opcional)
  handled_by: Enum(ai, human, mixed)  -- quién atendió la conversación
  agent_id: UUID (nullable)           -- si fue humano
  sent_at: timestamptz
  responded_at: timestamptz (nullable)
```

**Fase:** 2 (Expansión) — para el MVP basta con que funcione; en Fase 2 se mide la calidad.

### 6.13 Modo Sandbox / Testing por Tenant

**Problema:** El owner de un tenant quiere probar cambios en los prompts, agregar documentos o activar un agente nuevo sin afectar la operación en vivo.

**Mecanismo:**

1. Cada tenant puede activar un "modo sandbox" que duplica la configuración activa en un espacio de pruebas.
2. El sandbox tiene su propio número de teléfono de prueba (o un endpoint de webchat dedicado).
3. Los cambios en el sandbox (prompts, documentos, agent_configs) no afectan producción hasta que el owner hace "Publicar".
4. "Publicar" ejecuta un diff entre sandbox y producción y aplica los cambios atómicamente.
5. Rollback: se puede revertir al estado anterior con un clic (se mantiene una snapshot pre-publicación).

**Implementación:**
- Campo `environment: Enum(production, sandbox)` en `agent_configs`, `documents`, `approved_responses`.
- Las queries de producción filtran siempre por `environment = 'production'`.
- "Publicar" copia los registros de sandbox a producción dentro de una transacción.

**Fase:** 3 (Avanzado) — en las fases iniciales los cambios van directo a producción; el sandbox es una feature de madurez.

### 6.14 Backup y Recuperación ante Desastres

**Estrategia:**

1. **PostgreSQL:** `pg_dump` automatizado diario (comprimido) almacenado en almacenamiento externo (S3-compatible o Backblaze B2). Retención: 30 días daily + 12 meses monthly.
2. **Redis:** RDB snapshots cada 15 minutos + AOF para durabilidad. Redis es recuperable, pero diseñado para ser reconstruible desde PostgreSQL (caché, no fuente de verdad).
3. **Supabase Storage (documentos):** Réplica a bucket externo via cronjob.
4. **Pruebas de restore:** Cronjob mensual que restaura el backup más reciente en un entorno de prueba y ejecuta un healthcheck básico (count de tablas, integridad referencial).
5. **RTO (Recovery Time Objective):** < 4 horas.
6. **RPO (Recovery Point Objective):** < 24 horas (worst case: último backup diario).

**Fase:** 1 (MVP) — los backups se configuran desde el inicio. No es negociable para un SaaS.

---

## 7. ECOSISTEMA DE AGENTES (LangGraph)

### 7.1 Arquitectura del Grafo

```
                    ┌─────────────────┐
    Mensaje ──────► │ intent_routing  │
    Entrante        │ (Semantic Router)│
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐──────────────┐
              ▼              ▼              ▼              ▼
        ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐
        │ rag_query │ │ scheduling│ │ billing   │ │ marketing │
        │           │ │ _agent    │ │ _agent    │ │ _agent    │
        └─────┬─────┘ └─────┬─────┘ └─────┬─────┘ └─────┬─────┘
              │              │              │              │
              ▼              ▼              ▼              ▼
        ┌─────────────────────────────────────────────────────┐
        │            token_budget_check                       │
        └────────────────────┬────────────────────────────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        ┌───────────┐ ┌───────────────┐ ┌──────────────┐
        │ respond   │ │ human_handoff │ │ training_    │
        │           │ │               │ │ mode_approval│
        └───────────┘ └───────────────┘ └──────────────┘
```

**Estados del Grafo:**

| Estado | Descripción | Transiciones posibles |
|---|---|---|
| `intent_routing` | Clasifica la intención del mensaje usando un LLM ligero (GPT-4o-mini) con las categorías de agentes habilitados para el tenant | → `rag_query`, `scheduling_agent`, `billing_agent`, `marketing_agent`, `clinical_agent`, `vision_agent`, `human_handoff` (si confianza < 0.7) |
| `rag_query` | Recupera contexto del vector store y genera respuesta anclada | → `token_budget_check` |
| `scheduling_agent` | Ejecuta herramientas de Google Calendar | → `token_budget_check` |
| `billing_agent` | Ejecuta herramientas de facturación electrónica | → `token_budget_check` |
| `clinical_agent` | Procesa audio médico y estructura datos RIPS | → `token_budget_check` |
| `vision_agent` | Procesa imágenes/frames para aforo o check-in | → `token_budget_check` |
| `marketing_agent` | Orquesta campañas masivas | → `token_budget_check` |
| `token_budget_check` | Verifica presupuesto de tokens del tenant | → `respond`, `human_handoff` (si presupuesto agotado) |
| `respond` | Envía la respuesta al canal correspondiente | → `training_mode_approval` (si modo entrenamiento activo), → `END` |
| `training_mode_approval` | Retiene respuesta para aprobación humana | → `END` |
| `human_handoff` | Asigna conversación a agente humano, envía alertas | → `END` |

**Configuración por tenant:**
Cada tenant activa/desactiva agentes con un toggle. El `intent_routing` solo considera los agentes habilitados en su configuración. Un tenant que solo necesita RAG + agendamiento no recibe rutas a billing o clinical.

### 7.2 State Schema (LangGraph)

```python
class ConversationState(TypedDict):
    client_id: str
    conversation_id: str
    contact_id: str
    channel: str
    channel_constraints: dict  # ventana de sesión, template requerido, etc.
    messages: Annotated[list[BaseMessage], add_messages]
    current_intent: str | None
    intent_confidence: float
    active_agent: str | None
    retrieved_context: list[Document]
    tool_results: list[dict]
    token_usage: TokenUsage  # prompt_tokens, completion_tokens, cost_usd
    budget_status: str  # "ok", "warning", "degraded", "exceeded"
    requires_approval: bool
    handoff_reason: str | None
    metadata: dict
```

### 7.3 Sub-Agentes Especializados (detalle de herramientas)

**Agente de Agendamiento (Fase 1):**
- Tools: `check_availability(employee_ids, date_range, service_type)`, `create_appointment(...)`, `modify_appointment(appointment_id, changes)`, `cancel_appointment(appointment_id, reason)`, `list_appointments(contact_id, date_range)`.
- Integración bidireccional con Google Calendar API.
- Duración dinámica según el `service_type` configurado por el tenant.
- Verificación de disponibilidad cruzada entre múltiples empleados.
- Confirmaciones automáticas por el canal de origen.

**Agente Clínico / RIPS (Fase 3):**
- Tools: `transcribe_audio(audio_url)`, `extract_medical_entities(text)`, `format_rips_payload(entities)`, `validate_rips_schema(payload)`.
- STT: Whisper API con `language="es"` y prompt de contexto médico.
- Extracción de entidades: diagnósticos (CIE-10), procedimientos (CUPS), medicamentos, signos vitales.
- Output: JSON estructurado para HCE (Historia Clínica Electrónica) o RIPS (Resolución 3374/2000 y actualizaciones).
- **Regulatorio:** Datos médicos cifrados con `pgcrypto`, acceso auditado, retención según normativa colombiana (mínimo 15 años para HC).

**Agente Financiero / Facturación (Fase 2):**
- Tools: `calculate_invoice(items, tax_rules)`, `emit_electronic_invoice(invoice_data)`, `check_payment_status(invoice_id)`, `send_invoice_to_contact(invoice_id, channel)`.
- Integración con API de facturación electrónica (DIAN Colombia / adaptable por país).
- Validación de NIT/RUT antes de emisión.

**Agente de Visión (Fase 3 — Módulo Independiente):**
- **NOTA ARQUITECTÓNICA:** Este módulo requiere un servicio separado con acceso a GPU (o modelos optimizados ONNX). No comparte workers con el pipeline conversacional.
- Tools: `capture_frame(camera_id)`, `count_people(frame)`, `identify_face(frame, contact_id)`, `get_occupancy(location_id)`.
- Modelos: YOLOv8 para detección de personas, ArcFace/InsightFace para embeddings faciales.
- **Regulatorio (CRÍTICO):** Consentimiento explícito del contacto para almacenar embeddings faciales. Cumplimiento Ley 1581/2012 (Habeas Data Colombia). Opción de opt-out con eliminación garantizada de embeddings.

**Agente de Marketing (Fase 2):**
- Tools: `create_campaign(segment, template, schedule)`, `get_segment(tag_filters, engagement_score_range)`, `send_bulk_messages(campaign_id)`, `get_campaign_metrics(campaign_id)`.
- Campañas por WhatsApp (templates aprobados por Meta) y Email.
- Segmentación basada en tags y scoring predictivo del CRM.
- Envío masivo via cola `bulk` de Celery con throttling para respetar rate limits del proveedor.

---

## 8. CANAL DE VOZ (Fase 2)

- **Telefonía:** Integración con Twilio Voice (o Vonage) para recibir/realizar llamadas.
- **Flujo:**
  1. Llamada entrante → Twilio webhook → FastAPI.
  2. Audio streaming via WebSocket a un servicio de STT (Whisper API en modo streaming, o Deepgram para real-time).
  3. Transcripción en tiempo real → alimenta el `intent_routing` del grafo de LangGraph.
  4. Respuesta del agente → TTS (Text-to-Speech) via Twilio `<Say>` o ElevenLabs para voz natural.
  5. Al finalizar: resumen generado por IA se guarda como nota en la conversación del CRM.
- **Alternativa batch (más simple, Fase 2 temprana):** El usuario envía un audio por WhatsApp → se transcribe con Whisper → se procesa como texto normal en el pipeline.

---

## 9. FASES DE DESARROLLO (ROADMAP PRIORIZADO)

### FASE 1 — MVP CORE (Semanas 1-6)
Lo mínimo para que un tenant pueda operar en producción con WhatsApp + RAG + agendamiento.

| Módulo | Detalle |
|---|---|
| WhatsApp + YCloud | Canal principal, templates Meta, webhook receiver con idempotencia |
| RAG integrado | Pipeline de ingesta (texto + OCR), retrieval con pgvector, strict grounding |
| Agente de Agendamiento | Google Calendar, disponibilidad cruzada, duración dinámica |
| Inbox unificado | Supabase Realtime, derivación humana con alertas |
| CRM base | Contactos, tags, notas internas |
| Unificación de contactos | `contact_identifiers` cross-canal, merge manual (sección 6.7) |
| Ciclo de vida de conversaciones | Estados, auto-cierre, SLA tracking (sección 6.8) |
| Respuestas rápidas | Shortcuts para agentes humanos (sección 6.11) |
| Modo entrenamiento | Human-in-the-loop con few-shot dinámico |
| Control de costos | TokenBudgetGuard con enforcement activo |
| Seguridad | RLS, RBAC, cifrado, auditoría |
| Backup | pg_dump diario + restore test mensual (sección 6.14) |

### FASE 2 — EXPANSIÓN (Semanas 7-14)
Multicanal completo, integraciones avanzadas y herramientas de crecimiento.

| Módulo | Detalle |
|---|---|
| Canales adicionales | Telegram, Instagram DM, Facebook Messenger, Webchat, Email |
| Canal de voz (batch) | Audio por WhatsApp → Whisper → texto en pipeline |
| Templates de tenants | Clonación de configuración por sector (sección 6.6) |
| Análisis de sentimiento | Escalamiento proactivo por sentimiento negativo (sección 6.9) |
| Webhooks salientes | Event API para integraciones del tenant (sección 6.10) |
| Encuestas CSAT | Satisfacción post-conversación por canal (sección 6.12) |
| Agente financiero | Facturación electrónica DIAN |
| Agente de marketing | Campañas masivas con segmentación |
| CRM avanzado | Scoring predictivo, custom fields |
| Re-ranking RAG | Cross-encoder para mejorar precisión de respuestas |

### FASE 3 — MÓDULOS AVANZADOS (Semanas 15+)
Features de alto valor que requieren infraestructura especializada.

| Módulo | Detalle |
|---|---|
| Canal de voz real-time | Twilio/Vonage + STT streaming + TTS |
| Agente clínico / RIPS | Dictado médico → JSON estructurado para HCE |
| Agente de visión | Servicio GPU separado, YOLO + ArcFace, cumplimiento Habeas Data |
| Modo sandbox | Testing por tenant sin afectar producción (sección 6.13) |
| Multi-idioma | Detección automática de idioma + respuesta en el idioma del contacto |

---

## 10. METODOLOGÍA ITERATIVA (PASOS DE CONSTRUCCIÓN)

No intentes generar todo el código base a la vez. Construiremos de forma iterativa. Espera mi aprobación explícita en cada paso antes de avanzar al siguiente. Cada paso incluye criterios de aceptación verificables.

---

### PASO 1: Arquitectura y Schema de Base de Datos (FASE 1 MVP)

**1.1 — Diagrama de Arquitectura**
Genera un diagrama Mermaid de alto nivel que muestre:
- Flujo de un mensaje desde el canal hasta la respuesta.
- Componentes: Traefik, FastAPI, Redis, Celery (con colas), Supabase (PostgreSQL + pgvector + Realtime + GoTrue), servicio LLM externo.
- Límites de los contenedores Docker.
- Flechas de comunicación con protocolos (HTTP, WebSocket, AMQP/Redis).

**1.2 — Schema PostgreSQL del Core**
Genera DDL ejecutable para las tablas del MVP (Fase 1):
- **Tenants y Auth:** `clients` (tenants), `users` (con RBAC y rol por tenant).
- **CRM:** `contacts`, `contact_identifiers` (unificación cross-canal, sección 6.7), `tags`, `contact_tags`, `internal_notes`.
- **Conversaciones:** `conversations` (con estados del ciclo de vida, sección 6.8), `messages` (schema normalizado de la sección 6.1).
- **RAG:** `documents`, `document_chunks` (con columna `embedding vector(1536)` y índice HNSW).
- **Costos:** `token_budgets`, `token_usage_log`.
- **Webhooks:** `webhook_dedup`.
- **Agentes:** `agent_configs` (toggle de agentes por tenant), `quick_replies` (respuestas rápidas, sección 6.11).
- **Entrenamiento:** `pending_responses`, `approved_responses`.
- **Auditoría:** `audit_logs`.

Tablas de Fase 2 (DDL preparado pero no habilitado en MVP):
- `tenant_templates` (sección 6.6), `tenant_webhooks`, `outgoing_webhook_logs` (sección 6.10).
- `satisfaction_surveys` (sección 6.12), `service_types`, `channel_configs`.

**1.3 — Políticas RLS**
Genera las políticas RLS para CADA tabla, usando el patrón `SET LOCAL`.

**1.4 — Índices**
Genera todos los índices necesarios: B-tree para FKs y filtros frecuentes, GIN para campos JSONB, HNSW para embeddings vectoriales.

**✅ Criterios de Aceptación del Paso 1:**
- [ ] El DDL se ejecuta sin errores en PostgreSQL 15 con la extensión pgvector 0.7+.
- [ ] Las políticas RLS pasan un test de aislamiento: insertar datos con `client_id = A`, setear `app.current_client_id = B`, y verificar que un `SELECT *` devuelve 0 filas.
- [ ] El diagrama Mermaid renderiza correctamente y muestra todos los componentes del stack.
- [ ] Todos los campos JSONB que se consultan tienen índices GIN.
- [ ] La columna `embedding` tiene un índice HNSW con `vector_cosine_ops`.

---

### PASO 2: Infraestructura Docker y Configuración

**2.1 — docker-compose.yml**
Genera el archivo con los siguientes servicios:
- `traefik` (con configuración de rate limiting y TLS).
- `fastapi` (con healthcheck, variables de entorno, volúmenes).
- `supabase-db` (PostgreSQL con pgvector y pgcrypto).
- `supabase-auth` (GoTrue).
- `supabase-realtime`.
- `supabase-storage`.
- `supabase-pgbouncer` (configurado en modo `transaction` con `server_reset_query` apropiado).
- `redis`.
- `celery-webhooks` (worker de la cola webhooks).
- `celery-ai` (worker de la cola ai_inference).
- `celery-documents` (worker de la cola documents).
- `celery-notifications` (worker de la cola notifications).
- `celery-beat` (scheduler para tareas periódicas).
- `prometheus` + `grafana` (con volumen para dashboards predefinidos).

**2.2 — Variables de Entorno**
Genera `.env.example` con todas las variables necesarias, agrupadas por servicio y documentadas.

**2.3 — Configuraciones de Servicios**
- `traefik.yml` con middlewares de rate limiting.
- `celery_config.py` con definición de colas, routing y concurrencia.
- Script de inicialización de PostgreSQL que habilita extensiones (`pgvector`, `pgcrypto`, `uuid-ossp`) y ejecuta el DDL del Paso 1.

**✅ Criterios de Aceptación del Paso 2:**
- [ ] `docker-compose up -d` levanta todos los servicios sin errores.
- [ ] El healthcheck de FastAPI responde 200 en `/health`.
- [ ] pgBouncer conecta correctamente a PostgreSQL y las extensiones están habilitadas.
- [ ] Los workers de Celery se registran correctamente con sus colas asignadas.
- [ ] Traefik proxea correctamente al backend con TLS (o sin TLS en modo desarrollo).
- [ ] Grafana es accesible y muestra los dashboards vacíos.

---

### PASO 3: Webhook Receiver, Pipeline de Documentos y API Base

**3.1 — FastAPI Application Structure**
```
app/
├── main.py                    # FastAPI app factory
├── core/
│   ├── config.py              # Settings con Pydantic
│   ├── security.py            # JWT validation, RBAC middleware
│   ├── database.py            # Async SQLAlchemy + SET LOCAL middleware
│   └── dependencies.py        # Dependency injection
├── middleware/
│   ├── tenant_context.py      # Extrae client_id del JWT → SET LOCAL
│   └── token_budget.py        # TokenBudgetGuard
├── api/
│   ├── v1/
│   │   ├── webhooks.py        # Receiver de webhooks (público, firma validada)
│   │   ├── conversations.py   # CRUD conversaciones
│   │   ├── contacts.py        # CRUD contactos + tags
│   │   ├── documents.py       # Upload y gestión de documentos
│   │   ├── agents.py          # Configuración de agentes por tenant
│   │   └── auth.py            # Login, registro, refresh
│   └── internal/
│       └── health.py          # Healthcheck + readiness
├── models/                    # SQLAlchemy models
├── schemas/                   # Pydantic schemas (request/response)
├── services/
│   ├── messaging/
│   │   ├── base.py            # MessagingProvider ABC
│   │   ├── ycloud.py          # Implementación YCloud
│   │   └── factory.py         # Provider factory por canal
│   ├── document_pipeline.py   # Lógica de ingesta
│   └── rag.py                 # Retrieval + generation
├── tasks/                     # Celery tasks
│   ├── webhook_processor.py
│   ├── document_ingestion.py
│   └── notifications.py
└── agents/                    # LangGraph (Paso 4)
```

**3.2 — Webhook Receiver**
- Endpoint `POST /api/v1/webhooks/{provider}/{channel}`.
- Validación de firma del proveedor (HMAC para YCloud).
- Deduplicación contra `webhook_dedup`.
- Parseo con `MessagingProvider.parse_webhook()` → `NormalizedMessage`.
- Encolamiento en Celery `webhooks` queue → respuesta 200 inmediata.

**3.3 — Document Pipeline (Celery Tasks)**
- Task `ingest_document`: Orquesta el pipeline completo (detección → OCR → chunking → embedding → almacenamiento).
- Task `ocr_document`: Tesseract con preprocessing (deskew, binarización).
- Task `embed_chunks`: Genera embeddings en batches de 100 chunks.
- Progreso reportado vía Supabase Realtime para que el frontend muestre el estado.

**✅ Criterios de Aceptación del Paso 3:**
- [ ] Un webhook de prueba de YCloud es recibido, deduplicado, parseado y almacenado como `NormalizedMessage` en la DB.
- [ ] Un segundo envío del mismo webhook (mismo `external_id`) es ignorado (idempotencia).
- [ ] Un PDF con texto se ingesta correctamente: chunks + embeddings almacenados en `document_chunks`.
- [ ] Un PDF escaneado pasa por OCR y produce chunks legibles.
- [ ] El middleware `tenant_context` setea correctamente `SET LOCAL app.current_client_id` y las queries respetan RLS.
- [ ] Los endpoints protegidos rechazan requests sin JWT válido o con rol insuficiente.
- [ ] La estructura del proyecto sigue el layout definido en 3.1.

---

### PASO 4: LangGraph — Orquestación de Agentes

**4.1 — Definición del Grafo**
Implementa el `StateGraph` de LangGraph con los nodos y transiciones descritos en la sección 7.1.

**4.2 — Semantic Router (intent_routing)**
- Usa GPT-4o-mini con un prompt de clasificación que lista solo los agentes habilitados para el tenant.
- Output estructurado: `{"intent": str, "confidence": float, "reasoning": str}`.
- Si `confidence < 0.7` → `human_handoff`.

**4.3 — RAG Node**
- Retrieval con pgvector (query de la sección 6.4).
- Generation con strict grounding: el system prompt incluye `"Si la información no está en el contexto proporcionado, responde exactamente: 'No tengo información suficiente para responder. Voy a transferirte con un asesor.'" `.
- Si la respuesta contiene la frase de fallback → transición a `human_handoff`.

**4.4 — Tool Bindings**
- Implementa los tools del Agente de Agendamiento (Google Calendar API).
- Los demás agentes se implementan como stubs que loguean la llamada y retornan un placeholder (se implementan en fases posteriores).

**4.5 — Human Handoff**
- Actualiza la conversación con `status = "escalated"`.
- Envía notificación push y email al equipo del tenant (configurable: a todos los agentes, o round-robin, o al menos ocupado).
- Inserta nota interna con `handoff_reason`.

**4.6 — Checkpointing**
- Usa el checkpointer de LangGraph con PostgreSQL como backend para persistir el estado de conversaciones largas.

**✅ Criterios de Aceptación del Paso 4:**
- [ ] Un mensaje entrante de prueba recorre el grafo completo: `intent_routing` → `rag_query` → `token_budget_check` → `respond`.
- [ ] Un mensaje con intención de agendar recorre: `intent_routing` → `scheduling_agent` → `token_budget_check` → `respond` (con mock de Google Calendar).
- [ ] Un mensaje sin contexto RAG suficiente activa `human_handoff` (strict grounding).
- [ ] Un tenant con presupuesto de tokens agotado recibe `human_handoff` con el mensaje apropiado.
- [ ] El modo entrenamiento retiene la respuesta para aprobación cuando está activo.
- [ ] El estado de la conversación se persiste y puede reanudarse después de un restart del worker.
- [ ] Los tokens consumidos se registran correctamente en `token_usage_log`.

---

### PASO 5: Frontend del Inbox y Dashboard (Post-Backend)

> **NOTA:** Los pasos 1-4 cubren todo el backend del MVP. El frontend (Inbox unificado, dashboard de costos, configuración de agentes) se definirá en un prompt separado una vez que el backend esté funcional y testeado.

---

## 11. CONVENCIONES DE CÓDIGO

- **Python:** PEP 8, type hints en todas las funciones, docstrings Google-style.
- **Async:** Usar `async/await` consistentemente en FastAPI y en las queries de DB (SQLAlchemy async).
- **Naming:** snake_case para variables/funciones, PascalCase para clases, UPPER_SNAKE_CASE para constantes.
- **Error handling:** Excepciones custom que heredan de `AppException(status_code, error_code, message)`. Nunca exponer tracebacks al cliente.
- **Tests:** Cada paso debe incluir al menos tests unitarios para la lógica de negocio crítica (RLS, deduplicación, token budget, intent routing).
- **Logging:** Loguru con contexto `client_id` y `trace_id` en cada log.

---

## 12. RESTRICCIONES Y ADVERTENCIAS

1. **NO generar código de frontend** en los pasos 1-4. Solo backend, API y workers.
2. **NO usar ORMs mágicos** que oculten el SQL de RLS. Las queries de seguridad (SET LOCAL, políticas RLS) deben ser explícitas y auditables.
3. **NO hardcodear** claves API, secretos o configuraciones específicas de un tenant en el código.
4. **NO implementar** el agente de visión como un sub-proceso del mismo worker de Celery. Requiere un servicio separado.
5. **NO asumir** que todos los tenants necesitan todos los agentes. La arquitectura debe funcionar con solo 1 agente activo (RAG puro).

---

Confirma que has entendido estas instrucciones y procede con el **Paso 1** (secciones 1.1 a 1.4).
