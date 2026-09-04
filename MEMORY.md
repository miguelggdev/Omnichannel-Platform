# MEMORY.md — Decisiones Arquitectónicas y Contexto del Proyecto

> Registro acumulativo de decisiones, bugs conocidos, patrones aprendidos y contexto
> que toda sesión de Claude debe conocer. No borrar entradas; solo agregar.

---

## Decisiones Arquitectónicas (ADR)

### ADR-001: SET LOCAL en lugar de SET para variables de sesión RLS
- **Fecha:** 2026-09-01
- **Contexto:** pgBouncer en modo `transaction` (requerido para pooling eficiente en SaaS multi-tenant) resetea las variables de sesión al devolver la conexión al pool. Usar `SET app.current_client_id = '...'` provoca que la variable persista o se pierda de forma impredecible entre requests.
- **Decisión:** Usar siempre `SET LOCAL app.current_client_id = :client_id`. `SET LOCAL` tiene scope de transacción y se resetea automáticamente al finalizar la transacción, lo cual es compatible con pgBouncer transaction mode.
- **Consecuencia:** Todas las queries RLS deben ejecutarse dentro de una transacción explícita (`async with session.begin():`). El middleware `TenantContextMiddleware` abre la transacción y ejecuta SET LOCAL antes de pasar el control al endpoint.

### ADR-002: Pre-filtro vectorial por client_id (nunca post-ranking)
- **Fecha:** 2026-09-01
- **Contexto:** En búsquedas RAG con pgvector, filtrar por `client_id` después del cálculo de similaridad (post-ranking) es un riesgo de seguridad: el ranking podría incluir chunks de otros tenants que luego se filtran, pero el cálculo de distancia ya consumió recursos y expone información sobre la existencia de datos.
- **Decisión:** Siempre aplicar `WHERE client_id = current_setting('app.current_client_id')::uuid` ANTES del operador de distancia `<=>`. PostgreSQL con pgvector optimiza esto con índices parciales.
- **Consecuencia:** Los índices HNSW se crean por tabla, no por tenant. El filtro WHERE precede al ORDER BY en la query.

### ADR-003: MessagingProvider como abstracción (no acoplamiento directo a YCloud)
- **Fecha:** 2026-09-01
- **Contexto:** El MVP usa YCloud como proveedor de WhatsApp, pero el sistema debe soportar múltiples canales y proveedores sin refactoring masivo.
- **Decisión:** Crear una clase abstracta `MessagingProvider` con 5 métodos (send_text, send_media, send_template, get_status, parse_webhook). Cada proveedor implementa la interfaz. Un factory resuelve el proveedor por `channel + provider_config`.
- **Consecuencia:** Agregar un nuevo canal o proveedor requiere solo una nueva implementación de la ABC y una entrada en el factory. No se modifica código de negocio.

### ADR-004: TokenBudgetGuard con degradación gradual
- **Fecha:** 2026-09-01
- **Contexto:** Los tenants tienen presupuestos mensuales de tokens. Cortar el servicio abruptamente al 100% genera mala experiencia.
- **Decisión:** Implementar 3 umbrales:
  - 0-89%: Operación normal con modelo configurado por tenant.
  - 90-99%: Degradación — switch automático a modelo más barato (gpt-4o-mini).
  - 100%: Corte — el agente redirige a human_handoff con mensaje de presupuesto agotado.
- **Consecuencia:** Redis cachea el uso acumulado con TTL de 1 hora. El nodo `token_budget_check` del grafo LangGraph consulta Redis antes de cada inferencia.

### ADR-005: Training Mode con few-shot dinámico por similaridad semántica
- **Fecha:** 2026-09-01
- **Contexto:** El modo entrenamiento permite a humanos aprobar respuestas que luego se reutilizan. Necesitábamos definir cómo se seleccionan los ejemplos aprobados.
- **Decisión:** Flujo de 6 pasos:
  1. El agente genera una respuesta candidata.
  2. Se envía al supervisor humano para aprobación.
  3. Si aprobada, se guarda el par (pregunta, respuesta) con embedding en `approved_responses`.
  4. En futuras consultas similares, se recuperan los top-3 pares aprobados por cosine similarity.
  5. Se inyectan como few-shot examples en el prompt del LLM.
  6. El threshold de similaridad para seleccionar ejemplos es 0.80 (más estricto que RAG general).
- **Consecuencia:** La tabla `approved_responses` tiene columna `embedding vector(1536)` con índice HNSW.

### ADR-006: Celery con 5 colas especializadas
- **Fecha:** 2026-09-01
- **Contexto:** Un solo worker procesando webhooks y tareas de IA genera latencia cruzada. Los webhooks necesitan respuesta en <100ms.
- **Decisión:** 5 colas con prioridades:
  - `webhooks` (alta): recepción y ACK rápido.
  - `ai_inference` (media): procesamiento LLM, RAG.
  - `documents` (baja): ingestión y chunking de documentos.
  - `notifications` (media): envío de mensajes salientes.
  - `bulk` (baja): operaciones masivas, reportes.
- **Consecuencia:** Cada cola puede escalar workers independientemente. Docker Compose define un servicio por cola.

### ADR-007: Supabase self-hosted en lugar de cloud
- **Fecha:** 2026-09-01
- **Contexto:** Supabase Cloud tiene limitaciones de extensiones (pgvector, pgcrypto) y no permite configuración avanzada de PostgreSQL.
- **Decisión:** Usar Supabase self-hosted con Docker Compose. Incluye PostgreSQL 15+, pgBouncer, GoTrue (auth), Storage, Realtime.
- **Consecuencia:** Requiere gestión propia de backups, actualizaciones y monitoreo. El Sprint 2 configura la infraestructura Docker completa.

---

## Bugs Conocidos y Pitfalls

### BUG-001: Alias de SELECT no se puede usar en WHERE (PostgreSQL)
- **Descripción:** PostgreSQL no permite referenciar alias definidos en SELECT dentro de la cláusula WHERE del mismo query. Esto aplica al cálculo de similaridad en búsquedas RAG.
- **Incorrecto:**
  ```sql
  SELECT *, 1 - (embedding <=> :query_embedding) AS similarity
  FROM document_chunks
  WHERE client_id = :client_id
    AND similarity > :threshold  -- ERROR: column "similarity" does not exist
  ```
- **Correcto:**
  ```sql
  SELECT *, 1 - (embedding <=> :query_embedding) AS similarity
  FROM document_chunks
  WHERE client_id = :client_id
    AND 1 - (embedding <=> :query_embedding) > :threshold
  ORDER BY embedding <=> :query_embedding
  LIMIT :top_k
  ```
- **Impacto:** Todas las queries de similaridad en el servicio RAG y en approved_responses.

### BUG-002: pgBouncer transaction mode y SET vs SET LOCAL
- **Descripción:** Ver ADR-001. `SET` sin `LOCAL` en pgBouncer transaction mode puede causar que el `client_id` de un tenant se "filtre" a la siguiente request que reutilice la misma conexión del pool.
- **Impacto:** Fuga de datos entre tenants. CRÍTICO.
- **Prevención:** Grep periódico por `SET app.current_client_id` sin `LOCAL` en el codebase. Agregar test de integración que valide aislamiento.

---

## Patrones Aprendidos

### PAT-001: Webhook idempotency con deduplicación
- **Patrón:** Antes de procesar un webhook entrante, verificar `(channel, external_message_id)` en tabla `webhook_dedup`. Si existe, retornar 200 sin procesar. Si no, insertar y procesar.
- **Razón:** Los proveedores de mensajería (YCloud, Twilio, Meta) pueden reenviar webhooks por timeouts o errores de red.
- **TTL:** Las entradas de dedup se limpian después de 72 horas.

### PAT-002: NormalizedMessage como contrato interno
- **Patrón:** Todo mensaje entrante se normaliza a `NormalizedMessage(channel, sender, text, media_url, timestamp, metadata)` inmediatamente en el webhook receiver, antes de cualquier lógica de negocio.
- **Razón:** Desacopla la lógica de negocio del formato específico de cada proveedor de mensajería.

### PAT-003: ConversationState como TypedDict inmutable por paso
- **Patrón:** El estado del grafo LangGraph es un `TypedDict` que se pasa entre nodos. Cada nodo retorna un nuevo dict parcial que se mergea (no muta el original).
- **Razón:** LangGraph maneja el estado de forma funcional. Mutar el estado directamente causa bugs sutiles en checkpointing y replay.

### PAT-004: Traefik como API Gateway con rate limiting por tenant
- **Patrón:** Traefik v3 con middleware de rate limiting configurado por labels de Docker. Cada servicio expone su propio rate limit basado en el plan del tenant.
- **Razón:** Evita que un tenant abuse del sistema y afecte a otros. El rate limiting se aplica antes de llegar a FastAPI.

---

## Modelo de Datos — Resumen de Tablas

### Fase 1 (MVP) — 18 tablas
| # | Tabla | Propósito |
|---|---|---|
| 1 | `clients` | Tenants/organizaciones |
| 2 | `users` | Usuarios humanos por tenant (agentes, admins) |
| 3 | `contacts` | Contactos/clientes finales |
| 4 | `contact_identifiers` | Teléfonos, emails por contacto (multi-canal) |
| 5 | `tags` | Etiquetas por tenant |
| 6 | `contact_tags` | Relación N:M contactos-tags |
| 7 | `internal_notes` | Notas internas sobre contactos |
| 8 | `conversations` | Conversaciones con 7 estados |
| 9 | `messages` | Mensajes (entrantes y salientes) |
| 10 | `documents` | Documentos base de conocimiento |
| 11 | `document_chunks` | Chunks con embeddings para RAG |
| 12 | `token_budgets` | Presupuesto mensual de tokens por tenant |
| 13 | `token_usage_log` | Log granular de uso de tokens |
| 14 | `webhook_dedup` | Deduplicación de webhooks |
| 15 | `agent_configs` | Configuración de agentes IA por tenant |
| 16 | `quick_replies` | Respuestas rápidas predefinidas |
| 17 | `pending_responses` | Respuestas pendientes de aprobación (training mode) |
| 18 | `approved_responses` | Respuestas aprobadas con embeddings (few-shot) |

### Fase 2 (Expansión) — 6 tablas
| # | Tabla | Propósito |
|---|---|---|
| 19 | `audit_logs` | Auditoría de acciones |
| 20 | `tenant_templates` | Templates para clonación de tenants |
| 21 | `tenant_webhooks` | Webhooks salientes configurados por tenant |
| 22 | `outgoing_webhook_logs` | Log de intentos de webhooks salientes |
| 23 | `satisfaction_surveys` | Encuestas CSAT post-conversación |
| 24 | `channel_configs` | Configuración multi-canal por tenant |

### Enums importantes
- **conversation_status:** `new`, `bot_active`, `human_active`, `waiting_human`, `waiting_client`, `resolved`, `archived`
- **message_direction:** `inbound`, `outbound`
- **message_type:** `text`, `image`, `audio`, `video`, `document`, `location`, `template`, `interactive`
- **user_role:** `super_admin`, `admin`, `supervisor`, `agent`

---

## Contexto de Infraestructura

### Servicios Docker Compose (Sprint 2)
1. `traefik` — API Gateway, TLS, routing
2. `api` — FastAPI application (2+ réplicas)
3. `supabase-db` — PostgreSQL 15 + pgvector + pgcrypto
4. `supabase-auth` — GoTrue (autenticación)
5. `supabase-storage` — Almacenamiento de archivos
6. `supabase-realtime` — Websockets para tiempo real
7. `pgbouncer` — Connection pooling (transaction mode)
8. `redis` — Cache + message broker
9. `celery-webhooks` — Worker cola webhooks
10. `celery-ai` — Worker cola AI inference
11. `celery-documents` — Worker cola documentos
12. `celery-notifications` — Worker cola notificaciones
13. `celery-bulk` — Worker cola bulk operations
14. `celery-beat` — Scheduler periódico
15. `prometheus` — Métricas
16. `grafana` — Dashboards

### Variables de entorno críticas
- `DATABASE_URL` — PostgreSQL connection string
- `PGBOUNCER_URL` — pgBouncer connection string (para la app)
- `REDIS_URL` — Redis connection string
- `OPENAI_API_KEY` — API key de OpenAI
- `YCLOUD_API_KEY` — API key de YCloud
- `JWT_SECRET` — Secreto para tokens JWT
- `ENCRYPTION_KEY` — Clave para pgcrypto

---

## Historial de Sesiones

| Fecha | Sesión | Trabajo realizado |
|---|---|---|
| 2026-09-01 | Sesión 1 | Análisis y mejora del prompt maestro, adición de features |
| 2026-09-02 | Sesión 2 | Investigación de plugins/skills, generación del SDD |
| 2026-09-03 | Sesión 3 | Documento Word para devs, creación de archivos repo (CLAUDE.md, PROGRESS.md, MEMORY.md, specs) |
