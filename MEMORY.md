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

### ADR-008: Facebook Messenger e Instagram DM como canales MVP (no Fase 2)
- **Fecha:** 2026-09-05
- **Contexto:** Originalmente, Instagram DM y Facebook Messenger estaban planificados para Sprint 9 (Fase 2). Durante la revisión del pitch deck, se decidió incluirlos como canales MVP desde Fase 1 para maximizar el alcance de mercado desde el lanzamiento. Facebook tiene ~3B usuarios y Instagram ~2B usuarios — ambos representan canales críticos para la captación de clientes.
- **Decisión:** Mover la implementación de MetaProvider (que cubre Instagram DM y Facebook Messenger via Meta Graph API unificada) de Sprint 9 a Sprint 4. Ambos canales comparten la misma clase `MetaProvider` con un `MetaChannel` enum que diferencia el sub-canal, minimizando el esfuerzo adicional.
- **Consecuencia:**
  - Sprint 4 ahora implementa 2 proveedores: `YCloudProvider` (WhatsApp) y `MetaProvider` (Instagram + Facebook).
  - Sprint 9 se reduce a Telegram, Webchat, Email y Audio Transcription.
  - El MVP lanza con 3 canales de mensajería (WhatsApp, Instagram DM, Facebook Messenger) cubriendo las plataformas más utilizadas en LATAM.
  - Se requieren credenciales adicionales de Meta (App Secret, Page Access Token, Webhook Verify Token) en .env.
  - El schema DDL no requiere cambios (el ENUM `channel_type` ya incluía 'facebook' e 'instagram').

### ADR-009: Agent Activity Logging con Decorator Middleware
- **Fecha:** 2026-09-05
- **Contexto:** Se necesita auditoría completa de las acciones de cada nodo del grafo LangGraph para debugging, compliance y análisis de rendimiento.
- **Decisión:** Implementar un decorator `@logged_node(node_name, action_type)` que wrappea cada nodo del grafo. El decorator mide duración, captura input/output, registra tokens y errores, y escribe en la tabla `agent_action_logs`.
- **Consecuencia:** Cada ejecución de nodo genera un registro. La sesión DB se inyecta como campo efímero `_db_session` en ConversationState (no se persiste en checkpointer). Los logs se retienen 30 días (detallados) y 1 año (agregados).

### ADR-010: Onboarding como endpoint público con rate limiting
- **Fecha:** 2026-09-05
- **Contexto:** Nuevos clientes necesitan auto-registrarse sin intervención manual del super admin.
- **Decisión:** Crear `POST /api/v1/onboarding/register` como endpoint público (sin auth), protegido por rate limiting (5 req/IP/hora) y verificación de email. El endpoint crea client + admin user + default agent_config + token_budget en una sola transacción.
- **Consecuencia:** El super admin no necesita crear clientes manualmente. Se requiere servicio de email (Celery task) para verificación. El free tier otorga 50,000 tokens/mes.

### ADR-011: Cloudflare como capa de seguridad frente a Traefik
- **Fecha:** 2026-09-05
- **Contexto:** La aplicación necesita protección DDoS, WAF y CDN sin complejidad operativa propia.
- **Decisión:** Usar Cloudflare como proxy reverso frente a Traefik. Cloudflare maneja DNS, DDoS, WAF managed rules y certificados. Traefik confía las IPs de Cloudflare para X-Forwarded-For. Como alternativa, Cloudflare Tunnel evita exponer puertos del servidor.
- **Consecuencia:** Las reglas de firewall del servidor solo abren 80/443 y SSH. La comunicación Cloudflare ↔ Traefik usa mTLS (authenticated origin pulls).

### ADR-012: PostgreSQL Streaming Replication a VPS secundario
- **Fecha:** 2026-09-05
- **Contexto:** Un solo servidor con backups diarios tiene RPO de hasta 24 horas. Para SaaS multi-tenant esto es inaceptable.
- **Decisión:** Configurar streaming replication asíncrona a un VPS secundario. El primario envía WAL logs continuamente. RPO se reduce a < 1 minuto. En caso de fallo del primario, el secundario se promueve manualmente.
- **Consecuencia:** Requiere VPS secundario con PostgreSQL. Monitoreo de replication lag en Prometheus (alerta si > 30s). El backup diario pg_dump sigue ejecutándose como safety net.

### ADR-013: Telegram Bot para monitoreo de super admin
- **Fecha:** 2026-09-05
- **Contexto:** El super admin necesita visibilidad del estado del sistema desde dispositivos móviles, sin acceder al servidor.
- **Decisión:** Implementar un bot de Telegram (python-telegram-bot) que responde a comandos (/status, /docker, /db, /redis, /celery, /backup, /logs). Además, envía alertas proactivas (CPU, RAM, disco, crashes, queue depth, backup failures).
- **Consecuencia:** El bot es un servicio Docker independiente. Solo responde al chat_id configurado (whitelist). Las alertas se deduplicar con Redis (cooldown 15 min). Requiere TELEGRAM_BOT_TOKEN y TELEGRAM_ADMIN_CHAT_ID en .env.

### ADR-014: Frontend con Next.js 14 + shadcn/ui (Sprint 15)
- **Fecha:** 2026-09-05
- **Contexto:** La plataforma necesita una interfaz de administración web con theme toggle, responsive design e i18n completo.
- **Decisión:** Usar Next.js 14 (App Router) con TypeScript, shadcn/ui (Radix UI + Tailwind CSS), Zustand, TanStack Query, next-intl (6 idiomas), next-themes, y Recharts. Dockerizado con output standalone.
- **Consecuencia:** Se agrega Sprint 15 (Fase 4) al plan. El frontend se sirve como servicio Docker adicional vía Traefik. La autenticación usa JWT con httpOnly cookies.

### ADR-015: Suspensión de clientes con alertas de pago automatizadas
- **Fecha:** 2026-09-05
- **Contexto:** Clientes que no pagan necesitan ser notificados progresivamente antes de suspender el servicio.
- **Decisión:** Implementar sistema de alertas escalonadas: el super admin configura `alert_days_before_suspension` (ej: [7, 3, 1]) y `suspension_date`. Un Celery Beat task diario envía alertas y auto-suspende en la fecha configurada. El cliente suspendido recibe un `alert_message` configurable en lugar de respuestas del agente.
- **Consecuencia:** Requiere campos adicionales en `clients` (suspension_date, payment_alert_config JSONB, alert_message, suspended_at). El middleware `verify_client_is_active` bloquea mensajes salientes excepto el alert_message.

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

### Feature Enhancement Tables (Sesiones 5 y 7)
| # | Tabla | Propósito |
|---|---|---|
| 25 | `agent_action_logs` | Log de acciones de cada nodo LangGraph por conversación |
| 26 | `admin_assistant_history` | Historial de conversaciones del Admin Assistant por tenant/usuario |

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
- `TELEGRAM_BOT_TOKEN` — Token del bot de Telegram para monitoreo
- `TELEGRAM_ADMIN_CHAT_ID` — Chat ID del super admin para alertas
- `CLOUDFLARE_API_TOKEN` — Token de Cloudflare para WAF/Tunnel
- `CLOUDFLARE_ZONE_ID` — Zone ID de Cloudflare
- `REPLICA_HOST` — Host del servidor VPS de replicación
- `REPLICA_PORT` — Puerto PostgreSQL en réplica
- `META_APP_SECRET` — App Secret de Meta (Facebook/Instagram)
- `META_PAGE_ACCESS_TOKEN` — Page Access Token de Meta
- `META_WEBHOOK_VERIFY_TOKEN` — Token de verificación de webhooks Meta
- `ANTHROPIC_API_KEY` — API key de Anthropic (Claude) para Admin Assistant
- `ADMIN_ASSISTANT_MODEL` — Modelo Claude a usar (default: claude-sonnet-4-20250514)
- `ADMIN_ASSISTANT_MAX_TOKENS` — Máximo de tokens por respuesta del asistente (default: 1024)
- `ADMIN_ASSISTANT_RATE_LIMIT` — Límite de mensajes por minuto por admin (default: 20)

---

## Historial de Sesiones

| Fecha | Sesión | Trabajo realizado |
|---|---|---|
| 2026-09-01 | Sesión 1 | Análisis y mejora del prompt maestro, adición de features |
| 2026-09-02 | Sesión 2 | Investigación de plugins/skills, generación del SDD |
| 2026-09-03 | Sesión 3 | Documento Word para devs, creación de archivos repo (CLAUDE.md, PROGRESS.md, MEMORY.md, specs) |
| 2026-09-05 | Sesión 4 | Pitch deck investor (pptx), inclusión de Facebook e Instagram como canales MVP, actualización de specs (sprint-04, sprint-09) y docs del proyecto |
| 2026-09-05 | Sesión 5 | Integración de 11 nuevas features en specs: onboarding, personalización, theme toggle, responsive, i18n (6 idiomas), Celery admin, Telegram bot, agent logging, client mgmt, backup/replicación, seguridad. Creación de Sprint 15 (Frontend). Addendums para Sprints 3, 6, 8, 14 |
| 2026-09-05 | Sesión 6 | Eliminación de branch `develop` (feature/* → main directo). Dev Playbook artifact con 8 agentes + 6 roles secundarios. 5 funcionalidades adicionales: pre-commit hooks, GitHub Actions CI (8 stages), Alembic migration checks, RLS tests expandidos (25 tablas), Grafana Token Budget dashboard. Transferencia de 14+ archivos a PC vía device bridge |
| 2026-09-06 | Sesión 7 | Análisis del proyecto voz existente (AGENTE CONVERSACIONAL). Diseño de feature #12: Admin Assistant (chat+voz) con Claude + Edge TTS + Web Speech API. Spec completa (`specs/sprint-03-addendum-admin-assistant.md`). DDL: nueva tabla `admin_assistant_history` (#26), campos `admin_assistant_enabled`/`admin_assistant_voice_enabled` en `clients`, RLS + índices. ADR-019 |

---

## ADRs Adicionales

### ADR-016: Eliminación de branch develop (git simplificado)
- **Fecha:** 2026-09-05
- **Contexto:** Con solo 2 devs, la branch `develop` agrega fricción sin beneficio. Cada merge a develop requiere después otro merge a main, duplicando trabajo de CI/CD.
- **Decisión:** Flujo simplificado: `feature/sprint-{NN}-descripcion` → PR a `main` → CI pass → merge. Sin branch intermedia.
- **Consecuencia:** Branch protection solo en `main` (require PR, 1 approval, status checks, no force push). Sync diario con `git pull origin main`. Code review con `git diff main...`.

### ADR-017: CI Pipeline con 8 stages en GitHub Actions
- **Fecha:** 2026-09-05
- **Contexto:** Se necesita un pipeline de CI que valide lint, types, tests unitarios, tests de integración (con PostgreSQL+Redis), migraciones Alembic, seguridad y build Docker.
- **Decisión:** 8 stages: lint → typecheck → test-unit → test-integration (con servicios PostgreSQL pgvector + Redis) → migration-check → security (bandit + pip-audit + secrets + RLS patterns) → docker-build → frontend (condicional). Job final `ci-pass` como merge gate.
- **Consecuencia:** El job `ci-pass` es el status check requerido en branch protection. No se puede mergear si cualquier stage falla.

### ADR-018: Pre-commit hooks con detect-secrets
- **Fecha:** 2026-09-05
- **Contexto:** Prevenir que secrets, debug statements o código mal formateado lleguen al repositorio.
- **Decisión:** Usar pre-commit con: ruff (lint+format), mypy, detect-secrets, sqlfluff, commitizen (formato de commits), y hooks estándar (trailing whitespace, YAML/JSON check, no large files, no merge conflicts).
- **Consecuencia:** Cada dev debe ejecutar `pre-commit install` después de clonar. Los hooks corren antes de cada commit local.

### ADR-019: Admin Assistant con Claude + Edge TTS (costo cero en TTS/STT)
- **Fecha:** 2026-09-06
- **Contexto:** Se analizó el proyecto existente del usuario ("AGENTE CONVERSACIONAL") que usa Gemini Flash + Fish Audio TTS + Google STT + FAISS RAG para un asistente clínico dental con voz. Se identificaron componentes reutilizables de costo cero: Edge TTS (`es-CO-SalomeNeural`) para síntesis de voz y Web Speech API del navegador para STT.
- **Decisión:** Implementar un Admin Assistant (chat + voz opcional) para cada administrador de tenant. Stack:
  - **LLM:** Claude API (Anthropic) con function calling — herramientas read-only (query_conversations, query_token_usage, etc.) y write con confirmación (update_welcome_message, toggle_agent_node, etc.)
  - **TTS:** Edge TTS (`edge-tts` Python package, voz `es-CO-SalomeNeural`) — gratuito, streaming, sin API key
  - **STT:** Web Speech API (nativa del navegador Chrome/Edge/Safari) — gratuito, sin backend
  - **RAG:** Reutilizar el pipeline existente del proyecto (embeddings + pgvector) para consultar documentación de la plataforma
  - **Transporte:** WebSocket bidireccional para chat y streaming de audio chunks
  - **Toggleable:** `admin_assistant_enabled` y `admin_assistant_voice_enabled` en tabla `clients`
- **Consecuencia:**
  - Nueva tabla `admin_assistant_history` (tabla #26) para historial de conversaciones del asistente
  - 2 nuevos campos en `clients`: `admin_assistant_enabled BOOLEAN`, `admin_assistant_voice_enabled BOOLEAN`
  - El asistente NO es para usuarios finales, solo para admins/supervisores del panel
  - El costo operativo es solo el consumo de tokens de Claude (sin costos de TTS/STT)
  - Se distribuye en 3 sprints: backend (Sprint 3), diagnósticos del sistema (Sprint 8), widget frontend (Sprint 15)
  - Spec completa en `specs/sprint-03-addendum-admin-assistant.md`
