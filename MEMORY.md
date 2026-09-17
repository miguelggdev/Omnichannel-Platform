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

### ADR-007: ~~Supabase self-hosted en lugar de cloud~~ [SUPERSEDED por ADR-020]
- **Fecha:** 2026-09-01
- **Contexto:** Supabase Cloud tiene limitaciones de extensiones (pgvector, pgcrypto) y no permite configuración avanzada de PostgreSQL.
- **Decisión:** Usar Supabase self-hosted con Docker Compose. Incluye PostgreSQL 15+, pgBouncer, GoTrue (auth), Storage, Realtime.
- **Consecuencia:** Requiere gestión propia de backups, actualizaciones y monitoreo. El Sprint 2 configura la infraestructura Docker completa.

---

### ADR-028: Deduplicación de webhooks fail-open ante caída de Redis
- **Fecha:** 2026-09-10
- **Contexto:** PAT-001 define dedup en dos niveles (Redis rápido + `webhook_dedup` persistente). Falta decidir qué hacer cuando Redis no responde: rechazar el webhook (fail-closed) o dejarlo pasar (fail-open).
- **Decisión:** `mark_if_new()` devuelve `True` ante cualquier excepción de Redis y deja la unicidad al `UniqueConstraint(channel, external_message_id)` de `webhook_dedup`, que el worker verifica dentro de la misma transacción que el mensaje. Perder mensajes definitivamente es peor que procesar un duplicado, y el nivel 2 existe justamente para eso.
- **Consecuencia:** Con Redis caído el sistema sigue recibiendo, a costa de más carga en PostgreSQL. Los duplicados se cortan igual, solo que una capa más adentro.

### ADR-029: `release_mark()` al fallar el encolado en Celery
- **Fecha:** 2026-09-10
- **Contexto:** El endpoint marca el mensaje en Redis *antes* de encolarlo. Si `delay()` falla (broker caído), la marca queda puesta 24h: el reintento del proveedor se descartaría como duplicado y el mensaje se perdería sin dejar rastro.
- **Decisión:** Ante un fallo de encolado se borra la marca (`release_mark`) y se responde 503, para que el proveedor reintente.
- **Consecuencia:** La ventana de duplicado real es mínima y el mensaje no se pierde. Alternativa descartada: escribir primero en `webhook_dedup` — habría metido una escritura en base de datos dentro del presupuesto de <100ms del endpoint.

### ADR-030: Resolución del tenant en webhooks vía `DEFAULT_CLIENT_ID`
- **Fecha:** 2026-09-10
- **Contexto:** Los webhooks no llevan JWT, así que `TenantContextMiddleware` los exime y el `client_id` no se puede deducir del request. La spec del Sprint 4 permite resolverlo "por configuración global" en el MVP; la tabla `channel_configs` es de Fase 2.
- **Decisión:** `_resolve_client_id()` lee `settings.DEFAULT_CLIENT_ID`. Sin valor o con un UUID inválido lanza `ClientResolutionError`, el mensaje agota reintentos y termina en la DLQ. Nunca cae a un tenant por defecto silencioso.
- **Consecuencia:** El despliegue MVP es efectivamente de un solo tenant para mensajería entrante. `_resolve_client_id()` queda como único punto a cambiar cuando exista `channel_configs`.

### ADR-031: `extra="ignore"` en Settings
- **Fecha:** 2026-09-10
- **Contexto:** Ver BUG-004. El `.env` es compartido entre la app y docker-compose, y declara variables que la app nunca lee (`POSTGRES_*`, `GF_*`, `TRAEFIK_*`, `CELERY_*_CONCURRENCY`...).
- **Decisión:** `extra="ignore"` en el `model_config` de `Settings`, en vez de declarar las 44 claves sobrantes como campos muertos.
- **Consecuencia:** Un typo en el nombre de una variable de entorno ya no se detecta al arrancar. A cambio, la app arranca con su propio `.env.example`. El precio se considera menor que mantener 44 campos que nadie lee.

---

### ADR-032: Storage con bucket único y aislamiento por prefijo de ruta
- **Fecha:** 2026-09-14
- **Contexto:** Los archivos del knowledge base viven en Supabase Storage (ADR-020). Había que decidir entre un bucket por tenant o uno compartido.
- **Decisión:** Un único bucket privado (`SUPABASE_STORAGE_BUCKET`, por defecto `documents`) con la ruta `{client_id}/{document_id}/{filename}`. `build_object_path()` en `app/services/storage.py` es el único sitio que construye esa ruta. `sanitize_filename()` hace basename manual y filtra caracteres, porque el nombre lo elige el cliente y no debe poder salirse de su prefijo.
- **Consecuencia:** Como el bucket es privado, `documents.file_url` guarda la **ruta del objeto**, no una URL pública: leerlo pasa siempre por `download_from_storage()`, que autentica con la service key. Si algún día se quiere un bucket por tenant, se cambia una función.
- **Nota:** `app/services/storage.py` no está asignado a ningún rol en la Matriz §6. Lo necesitan el endpoint de subida (Dev B) y el pipeline de ingesta (Dev A), así que vive fuera de ambos. `download_from_storage()` es exactamente lo que la spec §2 llama desde `DocumentPipeline`.

### ADR-033: Transacción explícita en los endpoints de documentos
- **Fecha:** 2026-09-14
- **Contexto:** `app/api/v1/documents.py` tiene que encolar en Celery y borrar archivos de Storage. Con la dependency `get_tenant_session`, la transacción se cierra cuando FastAPI limpia las dependencias, o sea **después** de que el endpoint retorna.
- **Decisión:** Abrir `tenant_session()` a mano dentro de cada endpoint, para controlar dónde termina la transacción.
- **Razón:** El `delay()` tiene que ocurrir con la fila ya visible: el worker abre su propia conexión y no vería un documento sin commitear. Y el borrado en Storage va después del commit, porque si la transacción se revirtiera nos quedaríamos con el documento en base y sin su archivo.
- **Consecuencia:** Estos endpoints no usan `get_tenant_session`. `app/api/v1/auth.py` tampoco la usa, así que no rompe ninguna convención establecida.

### ADR-034: Los nodos del grafo leen la configuración del tenant de la fila activa de `agent_configs`
- **Fecha:** 2026-09-15
- **Contexto:** `specs/sprint-06-langgraph.md` asume un `agent_configs` con `agent_type`, `is_enabled` y un JSONB `settings`, y consulta una fila por tipo de agente. El modelo real (Sprint 1) tiene **una** fila por tenant con `name`, `model`, `temperature`, `system_prompt`, `welcome_message`, `handoff_message`, `training_mode`, `similarity_threshold`, `is_active` y un JSONB `config`.
- **Decisión:** `app/agents/nodes/_tenant.py::get_agent_settings()` es el único punto que traduce esa fila a un dataclass `AgentSettings`. Los agentes habilitados salen de `config.enabled_agents` (default `["rag"]`), y los parámetros de retrieval de `config.rag_threshold` / `config.rag_top_k`. `agent_configs.model` cumple el papel del `model_name` del spec, y `similarity_threshold` queda para los few-shot (0.80), no para los chunks de contexto (0.75).
- **Consecuencia:** Un tenant sin fila activa no se queda sin servicio: se usan los defaults (`OPENAI_CHAT_MODEL`, solo agente RAG, sin modo entrenamiento). Cuando exista una tabla de agentes por tipo, se cambia una función y ningún nodo se entera.
- **Relacionado:** `scheduling` queda deliberadamente fuera del default de `enabled_agents` — el agente llega en Sprint 7 y hasta entonces no debe enrutarse nada hacia él.

### ADR-035: El grafo compilado no se cachea entre tareas de Celery
- **Fecha:** 2026-09-15
- **Contexto:** `specs/sprint-06-langgraph.md` §12 propone un `_graph_cache` por tenant para no recompilar el grafo en cada mensaje.
- **Decisión:** No cachearlo. `app/tasks/ai_processor.py` compila el grafo dentro de cada invocación y ejecuta sus corrutinas con `run_isolated()` (`app/core/database.py`), que vacía el pool del engine al terminar, dentro del mismo event loop que abrió las conexiones.
- **Razón:** Cada tarea de Celery abre su propio event loop. Un grafo compilado guarda el `AsyncPostgresSaver` y sus conexiones atadas al loop que lo creó; reusarlo en el siguiente loop es exactamente el root cause de BUG-006 / BUG-011 ("attached to a different loop"). Compilar cuesta microsegundos; lo caro es el checkpointer, y esas conexiones se cierran al terminar la tarea.
- **Consecuencia:** La tarea de IA nace ya alineada con la regla que dejó PR #12 (BUG-011): ningún `app/tasks/*.py` llama a `asyncio.run()` directamente. Si algún día se quisiera cachear el grafo, habría que cachearlo por (tenant, loop) o sacar el checkpointer del objeto compilado.

### ADR-036: Las tablas del checkpointer de LangGraph se crean por Alembic, no por `checkpointer.setup()`
- **Fecha:** 2026-09-16
- **Contexto:** `AsyncPostgresSaver.setup()` (langgraph-checkpoint-postgres) crea sus 4 tablas (`checkpoint_migrations`, `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) con `CREATE TABLE IF NOT EXISTS` en tiempo de ejecución. Eso exige privilegio `CREATE` en el schema — el rol con el que corre la app (`app_user` en CI; el rol de aplicación en producción) solo tiene DML, la misma regla que rige para el resto del schema desde Sprint 1.
- **Decisión:** `app/agents/graph.py` nunca llama a `.setup()`. `migrations/versions/003_langgraph_checkpoints.py` crea las 4 tablas una sola vez, con el SQL final de `AsyncPostgresSaver.MIGRATIONS` (sin `CREATE INDEX CONCURRENTLY`: las tablas nacen vacías y esa cláusula no puede correr dentro de una transacción, que es como Alembic ejecuta cada migración). `migrations/env.py` excluye esas 4 tablas del diff de `alembic check` vía `include_name`, porque no son modelos SQLAlchemy y sin el filtro autogenerate las marca para borrar en cada corrida.
- **Relacionado:** `requirements.txt` no declaraba `psycopg[binary,pool]` — sin el extra `[binary]`, `psycopg` v3 (la libreria que usa `AsyncPostgresSaver`, distinta de `psycopg2-binary` que usa Alembic) no se puede ni importar sin `libpq` del sistema. Se agregó explícito.
- **Consecuencia:** Si `langgraph-checkpoint-postgres` agrega una migración interna nueva en una versión futura, hay que replicarla a mano en una migración nueva de Alembic — no se sincroniza sola.
- **Corrección 2026-09-16:** la primera versión de `_checkpointer_conninfo()` prefería `DATABASE_URL_DIRECT` (razón: evitar prepared statements contra el pooler). Era un camino muerto: `docker-compose.yml` no le pasa esa variable a ningún worker de Celery, así que el checkpointer siempre corría contra `DATABASE_URL` en la práctica — lo que de hecho funciona bien, porque `prepare_threshold=0` en `conn_kwargs` ya neutraliza el problema de prepared statements, y el checkpointer no hace DDL (eso es justo lo que sí exige conexión directa, y ya no pasa desde este mismo ADR). Se simplificó a usar siempre `DATABASE_URL`: es además la elección correcta para un pool que se abre y cierra en cada mensaje, ya que los slots de conexión directa de Supabase Cloud son limitados y el pooler está pensado para ese patrón de conexiones cortas y frecuentes.

### ADR-037: El auto-cierre de conversaciones itera tenant por tenant con RLS activa
- **Fecha:** 2026-09-16
- **Contexto:** `app.tasks.bulk_auto_close_conversations` (Celery Beat, cada 15 min) es cross-tenant por naturaleza: barre las conversaciones de todos los clientes. Pero el rol con el que corre la aplicación está sujeto a RLS (`FORCE ROW LEVEL SECURITY` en las 18 tablas desde BUG-005; en CI la suite corre como `app_user`, `NOSUPERUSER NOBYPASSRLS`). `specs/sprint-07-scheduling-crm.md` §14 plantea las dos salidas y no elige: (a) un rol de servicio con `BYPASSRLS` y una session factory aparte, (b) iterar por tenant con `SET LOCAL`.
- **Decisión:** la (b). El worker abre un `tenant_session(client_id)` por tenant y corre los dos UPDATE dentro de esa transacción, con el `client_id` explícito en el WHERE además de la política. No se crea ningún rol privilegiado ni se toca `docker-compose.yml`.
- **Razón:** la (a) es más rápida pero abre un camino sin RLS en producción para ahorrar un bucle que hoy recorre un solo tenant; CLAUDE.md (restricción 5) no permite saltarse el aislamiento en ningún sprint, y un rol con `BYPASSRLS` en el worker es exactamente eso. La (b) además es verificable en CI tal como está montado hoy.
- **Limitación conocida:** la lista de tenants sale de `SELECT id FROM clients`, y esa tabla también tiene RLS (política por `id`). Sin contexto de tenant la consulta falla, así que `_load_active_client_ids()` cae a `DEFAULT_CLIENT_ID` — el mismo valor con el que `webhook_processor._resolve_client_id()` resuelve **todos** los mensajes entrantes del MVP. En la práctica, hoy, cubre el 100% de las conversaciones que existen. El día que haya varios tenants de verdad (tabla `channel_configs`, Fase 2), `_load_active_client_ids()` es el único punto a cambiar: o se le pasa un DSN de rol de servicio, o se lee la lista de donde la resuelva el enrutamiento de canales.
- **Consecuencia:** el barrido es O(tenants) transacciones en vez de una. Con el volumen del MVP es irrelevante; si algún día pesa, la salida no es quitar la RLS sino paralelizar el bucle o mover el barrido a una tarea por tenant.

### ADR-038: Los endpoints que dependen de una entrega pendiente importan perezoso y degradan con 503
- **Fecha:** 2026-09-16
- **Contexto:** Dos endpoints de Dev B del Sprint 7 dependen de código de Dev A que todavía no existe: `POST /contacts/{id}/merge/{target}` necesita `app/services/contact_unifier.py`, y los tres de `/agent-logs` necesitan `app/models/agent_action_log.py`. Un `import` normal en la cabecera del router hace fallar `create_app()` entero: no arrancaría ni `/health`.
- **Decisión:** el import va dentro de la función (`contacts.py::merge_contacts`, `agent_logs.py::_agent_action_log_model()`), envuelto en `try/except ImportError`, y el fallo se traduce a un `AppException(503, ...)` con un `error_code` propio (`UNIFIER_UNAVAILABLE`, `AGENT_LOGGING_UNAVAILABLE`). El router se registra siempre; los demás endpoints del mismo módulo funcionan con normalidad.
- **Razón:** es el mismo criterio con el que `ai_processor.py` (Sprint 6) trata la ausencia del grafo — degradar con un motivo legible en vez de tumbar el proceso — y evita la alternativa fea de registrar routers condicionalmente, que haría que la documentación de OpenAPI cambiara según qué esté entregado.
- **Consecuencia:** los dos módulos necesitan `ignore_missing_imports` en `pyproject.toml` (`app.services.contact_unifier`, `app.models.agent_action_log`) mientras dure la espera; hay que quitarlos de esa lista cuando Dev A entregue, igual que se hizo con `app.services.messaging.*` en Sprint 4. Los tests cubren las dos mitades: el 503 de hoy y el camino completo con el modelo sustituido por un doble.
- **Nota sobre el orden de las comprobaciones:** el `require_role` se evalúa antes que la disponibilidad (es una dependency de FastAPI), así que un usuario sin permiso recibe 403 y no el 503 — no se filtra el estado interno del sistema a quien no debería verlo.

### ADR-039: `waiting_human -> resolved` se agrega a la máquina de estados
- **Fecha:** 2026-09-16
- **Contexto:** la tabla de transiciones de `specs/sprint-07-scheduling-crm.md` §9 deja `waiting_human` con una sola salida: `human_active`. `human_handoff_node` (Sprint 6) pone las conversaciones en ese estado y ningún nodo del grafo vuelve a tocarlas; las dos reglas de auto-cierre miran `waiting_client` y `resolved`.
- **Decisión:** `app/services/conversation_lifecycle.py::VALID_TRANSITIONS` agrega `waiting_human -> resolved`. Es la única diferencia con la tabla de la spec.
- **Razón:** sin ella, un handoff que nadie atiende es un callejón sin salida permanente: no se puede cerrar por API, no lo alcanza el auto-cierre y no se puede archivar. La conversación quedaría en la bandeja para siempre.
- **Consecuencia:** el resto de la tabla queda igual que en la spec, incluido que `resolved` y `archived` son terminales. Eso es deliberado y consistente con `webhook_processor._get_or_create_conversation()`, que al llegar un mensaje nuevo descarta las cerradas (`CLOSED_STATUSES`) y abre una conversación nueva en vez de reabrir la vieja: por eso `resolved_at` nunca se limpia y no existe ninguna transición de vuelta.

### ADR-040: El autor de un cambio viaja en un ContextVar, no en un parámetro
- **Fecha:** 2026-09-17
- **Contexto:** el rastro de auditoría lo escribe un trigger de PostgreSQL (migración 004). El trigger sabe a qué tenant pertenece cada cambio porque lo lee de la propia fila, pero no *quién* lo hizo: eso solo lo sabe la aplicación, y tiene que pasárselo por `app.current_user_id`.
- **Alternativa descartada:** añadir un parámetro `user_id` obligatorio a `tenant_session()`. Son ~30 llamadas; cualquiera que olvidara pasarlo dejaría un hueco silencioso en el rastro — un cambio hecho por una persona registrado como acción del sistema. Y nada lo delataría: el rastro seguiría escribiéndose, solo que mintiendo.
- **Decisión:** `app/core/database.py` expone `current_user_id: ContextVar[UUID | None]`, que `AuditContextMiddleware` (`app/middleware/audit.py`) puebla por petición y `tenant_session()` lee por defecto. El parámetro explícito sigue existiendo y tiene prioridad, para los casos en que haga falta forzarlo.
- **Consecuencia:** ninguna llamada existente cambia y todas heredan el usuario de su petición. Lo que corre fuera de una petición (un worker de Celery) queda en `None`, que es exactamente lo que debe registrarse: una acción del sistema no la hizo nadie. El `reset()` del middleware no es opcional — sin él, el valor se queda pegado al contexto reutilizado y un cambio sin autenticar podría acabar atribuido al último usuario que pasó por ahí.

### ADR-041: `audit_logs` se borra en cascada con su tenant
- **Fecha:** 2026-09-17
- **Contexto:** la primera versión de la migración 004 declaró la FK `audit_logs.client_id -> clients.id` sin `ondelete`. En cuanto el trigger empezó a dejar filas, el `DELETE FROM clients` de la limpieza de **cualquier** test de integración empezó a fallar por una tabla que ese test nunca escribió (39 errores de teardown en CI, repartidos por `test_crm_api`, `test_graph_flow` y `test_webhook_flow`).
- **Decisión:** `ON DELETE CASCADE` en `client_id`, `ON DELETE SET NULL` en `user_id`.
- **Razón:** la alternativa era enseñarle `audit_logs` a la limpieza de cada test, y a cualquier código futuro que dé de baja un cliente. El rastro de auditoría de un tenant no tiene sentido sin el tenant, y una baja completa de cliente (RGPD a nivel de organización) tiene que poder ejecutarse. `user_id` es al revés: dar de baja a un empleado no debe borrar lo que hizo, solo dejar la fila sin autor.
- **Consecuencia:** el rastro no sobrevive al borrado del tenant. Si algún día hace falta conservarlo para cumplimiento después de la baja, la salida no es quitar el CASCADE sino exportarlo antes de borrar.

### ADR-042: Las tareas de mantenimiento son envoltorios sobre scripts de bash
- **Fecha:** 2026-09-17
- **Contexto:** el backup y la prueba de restauración (spec §7) son `pg_dump`, `pg_restore`, `psql` y el CLI de `aws` encadenados.
- **Decisión:** la lógica vive en `scripts/backup.sh` y `scripts/restore_test.sh`; `app/tasks/maintenance.py` solo los invoca con `subprocess.run` (lista fija, sin shell), acota el tiempo y traduce un código de salida distinto de cero en una excepción.
- **Razón:** reimplementar esas herramientas desde Python añadiría una capa propia que puede fallar por su cuenta, y los scripts se pueden ejecutar a mano para verificar la configuración sin levantar Celery. Lo que aporta Celery es el calendario y que el fallo quede visible.
- **Consecuencia:** un backup que falla marca la tarea como fallida en vez de "terminar" sin hacer nada, que es el modo en que un backup roto se descubre el día que hace falta restaurar. Los nombres son `app.tasks.bulk_*` y no los `app.tasks.maintenance.*` de la spec §7.3: no existe cola `maintenance` y el routing de Sprint 2 es por prefijo de nombre, así que con el nombre del spec habrían caído en la cola `webhooks`, bloqueando la recepción de mensajes durante la hora que dura un backup.

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

### BUG-002: Supavisor/pgBouncer transaction mode y SET vs SET LOCAL
- **Descripción:** Ver ADR-001. `SET` sin `LOCAL` en Supavisor/pgBouncer transaction mode puede causar que el `client_id` de un tenant se "filtre" a la siguiente request que reutilice la misma conexión del pool.
- **Impacto:** Fuga de datos entre tenants. CRÍTICO.
- **Prevención:** Grep periódico por `SET app.current_client_id` sin `LOCAL` en el codebase. Agregar test de integración que valide aislamiento.

---

## Patrones Aprendidos

### BUG-003: Dashboard token-budget apunta a un datasource self-hosted inexistente
- **Descripción:** `grafana/dashboards/token-budget-monitoring.json` (10 paneles) consulta el datasource `{"type": "postgres", "uid": "supabase-db"}`, el contenedor que ADR-020 eliminó. Con Supabase Cloud ese datasource no existe, así que los 10 paneles no renderizan datos.
- **Detectado:** Sprint 2, Dev B, al crear el provisioning de Grafana.
- **Estado:** ABIERTO — asignado a Dev A en Sprint 8 (`grafana/dashboards/*.json` es suyo en la Matriz §6, junto con las métricas custom de Prometheus).
- **Opciones evaluadas:** (a) provisionar un datasource Postgres contra Supabase Cloud — descartado en Sprint 2 por meter credenciales de la BD dentro del provisioning de Grafana; (b) repuntar los paneles a Prometheus — es lo coherente con el stack, pero las métricas de tokens no existen en Prometheus hasta el Sprint 8.
- **Nota:** El provisioning creado en Sprint 2 (`grafana/provisioning/`) solo declara el datasource Prometheus (`uid: prometheus`). No enmascara este bug.

### BUG-004: `Settings` rechazaba el propio `.env.example` del repo
- **Descripción:** `Settings` heredaba el `extra="forbid"` por defecto de `BaseSettings`, pero `.env.example` declara 68 claves y la clase solo modelaba 24. Las 44 restantes (`POSTGRES_DB`, `REDIS_HOST`, `S3_*`, `GF_*`, `TRAEFIK_*`, `CELERY_*_CONCURRENCY`, `VAPI_*`, `APP_PORT`, ...) hacían que `Settings()` lanzara `ValidationError` con 44 errores y la app no arrancara.
- **Impacto:** 12 tests de `tests/unit/test_auth.py` erroraban en setup (`create_app()` importa `Settings`). En CI pasaba inadvertido porque el workflow inyecta solo las variables que la clase declara.
- **Efecto secundario de seguridad:** el `ValidationError` de Pydantic imprime el *valor* de cada campo rechazado, así que un `.env` real volcaba secretos (incluida `POSTGRES_PASSWORD`) en la salida de pytest y en cualquier log de arranque.
- **Detectado:** Sprint 4, Dev B.
- **Estado:** CERRADO — `extra="ignore"` (ADR-031), commit `591e416`. `app/core/config.py` es de Dev A; tocado de forma aislada y marcado en el PR.

### BUG-005: La migración baseline de Alembic no crea RLS (CRÍTICO)
- **Descripción:** `migrations/versions/001_baseline.py` crea las 24 tablas, FKs e índices, pero no contiene ni un `ENABLE ROW LEVEL SECURITY`, ni un `FORCE`, ni una sola `CREATE POLICY` — 0 coincidencias en todo el archivo. Las políticas viven solo en `supabase/init/init.sql`, que con ADR-020 (Supabase Cloud) ya no se ejecuta: no hay contenedor de Postgres que corra `/docker-entrypoint-initdb.d`.
- **Impacto:** Una base creada solo con Alembic queda **sin aislamiento entre tenants**. `SET LOCAL app.current_client_id` se aplica pero no lo filtra nada, y cualquier tenant vería los datos de los demás. Contradice la regla 1 de CLAUDE.md.
- **Por qué no saltó antes:** `tests/integration/test_rls_all_tables.py` (25 tests) solo corre con `--run-db`, y sin base de datos se omite. La suite pasa en verde con el agujero abierto.
- **Detectado:** Sprint 4, Dev B, al revisar cómo resolver el `client_id` de los webhooks.
- **Estado:** CERRADO 2026-09-14 — `migrations/versions/002_rls_policies.py` agrega RLS a las 18 tablas reales. Ver también NOTA-002 (el CI que lo tapaba) y BUG-006 (bugs adicionales que aparecieron al arreglar esto).

### NOTA-001: Los Quality Gates de METHODOLOGY §7 son más laxos que el CI
- **Descripción:** Verificar en local lo que dice METHODOLOGY §7 no garantiza un CI verde. Dos desajustes reales, ambos me costaron un rebote en el PR #5:
  - **Gate 1** pide `ruff check app/`. El CI corre además `ruff format app/ tests/ --check --diff`. Un archivo bien lintado puede estar mal formateado.
  - **Gate 2** pide `mypy app/ --ignore-missing-imports`. El CI corre `mypy app/ --config-file=pyproject.toml` (sin esa flag) y además instala `types-redis` y `sqlalchemy[mypy]`. Con la forma laxa pasaban 8 errores que el CI sí veía: `Redis` es genérico en los stubs (`Redis[str]` con `decode_responses=True`), los stubs de `types-redis` todavía no conocen `aclose()` (hay que usar `close()`), y los imports de módulos aún inexistentes necesitan un override explícito en vez de la flag global.
- **Cómo verificar de verdad antes de abrir un PR** (los comandos exactos del workflow):
  ```
  ruff check app/ tests/
  ruff format app/ tests/ --check --diff
  mypy app/ --config-file=pyproject.toml     # con types-redis instalado
  pytest tests/
  ```
- **Pitfall aparte:** correr los gates sobre el working tree y no sobre `HEAD`. Un arreglo sin commitear da verde en local y rojo en el CI. Comprobar `git status` antes de dar por buenos los gates.
- **Detectado:** Sprint 4, Dev B. METHODOLOGY.md no es de ningún rol en la Matriz §6; queda anotado aquí en vez de editarlo por mi cuenta.

### NOTA-002: El CI valida RLS contra `init.sql`, no contra la migración de Alembic
- **Descripción:** El job "Integration Tests (RLS + DB)" siembra el esquema con `psql -f supabase/init/init.sql` — el archivo que **sí** tiene las políticas RLS. Por eso los tests de aislamiento pasan en verde mientras `migrations/versions/001_baseline.py` no crea ninguna (BUG-005). El CI nunca ejerce el camino que realmente corre contra Supabase Cloud.
- **Agravante:** ese mismo job corre `pytest tests/integration/ -v --tb=short` **sin** `--run-db`, así que todo lo marcado con `db` (incluidos los 25 tests de `test_rls_all_tables.py` y los 8 de `test_webhook_flow.py`) se omite. Solo `tests/unit/test_rls_isolation.py` se ejecuta con `--run-db`.
- **Consecuencia:** el verde del CI en RLS era engañoso, y por la misma razón ningún test de `tests/integration/` (RLS o no) había corrido nunca de verdad en CI desde Sprint 1.
- **Detectado:** Sprint 4, Dev B, al revisar por qué el CI pasaba con BUG-005 presente.
- **Estado:** CERRADO 2026-09-14 — `.github/workflows/ci.yml` siembra ahora vía `alembic upgrade head` y corre `pytest tests/integration/ -v --tb=short --run-db`. `tests/unit/test_rls_isolation.py` (el único que sí corría con `--run-db`, pero con `@pytest.mark.skip` individual en sus 9 tests desde Sprint 1) se eliminó por redundante con `test_rls_all_tables.py`.

### BUG-006: Cuatro bugs que ningún test había ejecutado nunca contra Postgres real
- **Descripción:** Al cerrar BUG-005 y encender `--run-db` en CI por primera vez, salieron a la luz cuatro bugs preexistentes que la suite nunca había ejercido de verdad:
  1. **`app/core/database.py::tenant_session()`** ejecutaba `text("SET LOCAL app.current_client_id = :client_id")` con un parámetro bind. PostgreSQL no admite parámetros en `SET` — es un error de sintaxis, no una particularidad de SQLAlchemy/asyncpg. Este era el mecanismo central de aislamiento por tenant (el mismo patrón que CLAUDE.md documentaba como "obligatorio"); nunca se había ejecutado con éxito contra un Postgres real. Fix: `SELECT set_config('app.current_client_id', :client_id, true)` — `set_config()` sí acepta parámetros, y `is_local=true` da el mismo scope de transacción que `SET LOCAL`.
  2. **`tests/conftest.py::db_engine`** era `scope="session"` (un solo engine para toda la suite), pero pytest-asyncio abre un event loop nuevo por test por defecto. Un engine async de SQLAlchemy queda atado al loop donde se creó; del segundo test en adelante las conexiones del pool se corrompían (`RuntimeError: attached to a different loop`, o incluso SQL con "syntax error" en columnas al azar por buffers de conexión reusados desde el loop equivocado). Fix: `db_engine` a scope de función.
  3. **`tests/integration/test_webhook_flow.py`** reusa el engine singleton real de `app.core.database` (no uno propio de test) — mismo síntoma que (2) pero sin scope de fixture que tocar, porque el engine se crea una vez al importar el módulo. `pytest.mark.asyncio(loop_scope="module")` **no** lo arregló (siguió fallando, con `InternalClientError: got result for unknown protocol state 3`). Lo que sí funcionó: `await engine.dispose()` al inicio del fixture `webhook_tenant` — vacía el pool sin usar ninguna conexión existente, y el siguiente checkout crea una conexión nueva en el loop del test actual.
  4. **`tests/integration/test_rls_all_tables.py`**: bind params pegados directo a un cast `::vector` (`:embedding::vector`) confundían el parser de `text()` de SQLAlchemy (`syntax error at or near ":"`) — fix: parentizar (`(:embedding)::vector`). Y un `Result.scalar()` llamado dos veces sobre el mismo `Result` (`ResourceClosedError`) — un `Result` de SQLAlchemy solo se puede consumir una vez.
- **Por qué importa:** los 4 bugs son independientes de BUG-005 en sí, pero **ninguno era detectable sin `--run-db` activo**, que es justo lo que NOTA-002 tenía apagado. Vale la pena tenerlo presente: la próxima vez que se agregue código que dependa de una sesión de DB real, no asumir que pasar en CI significa que se ejecutó — confirmar que el job relevante no está silenciosamente saltando tests.
- **Detectado y cerrado:** 2026-09-14, en la misma sesión que BUG-005.

### BUG-007: El fixture `authenticated_client` emitía un JWT que el middleware no puede leer
- **Descripción:** `tests/conftest.py` construía el token a mano con la claim `sub`, pero `TenantContextMiddleware` lee `payload["user_id"]` — habría dado `KeyError` y un 500. Además firmaba con `os.getenv("JWT_SECRET", "test-secret-key-for-testing-only")` en vez del `JWT_SECRET` de la app, así que tampoco habría validado.
- **Por qué no saltó antes:** ningún test usaba el fixture. Existía desde Sprint 3 y los primeros en ejercitarlo fueron los tests de documentos, en Sprint 5.
- **Estado:** CERRADO — ahora usa `create_access_token()`, la misma función que emite los tokens en producción, así el payload no puede desincronizarse del middleware. Se añadió `authenticated_client_factory` para pedir rol o tenant concretos.

### BUG-008: `DocumentResponse` fallaba porque `created_at` llegaba a None
- **Descripción:** `created_at` y `updated_at` son `server_default`, así que tras `session.flush()` siguen a `None` hasta que PostgreSQL los rellena. Serializar el documento ahí mismo hacía fallar la validación de Pydantic.
- **Agravante en async:** no basta con acceder al atributo para que SQLAlchemy los cargue. La carga perezosa necesita IO, y en contexto asíncrono eso revienta con `MissingGreenlet`; hay que pedirlos explícitamente con `await session.refresh(document)`.
- **Detectado:** Sprint 5, Dev B, por el primer test que ejercitó la subida completa.
- **Estado:** CERRADO — `await session.refresh(document)` antes de serializar.

### BUG-009: Los tests de aislamiento RLS pasaban en vacío por dos motivos distintos
- **Descripción:** Desde Sprint 1, `tests/integration/test_rls_all_tables.py` (22 tests activos) daba verde **sin comprobar RLS en ningún momento**. Dos defectos independientes, cada uno suficiente por sí solo:
  1. **El rol del CI era superusuario.** `POSTGRES_USER` del contenedor de PostgreSQL se crea como `SUPERUSER`, y PostgreSQL ignora las políticas RLS para superusuarios **incluso con `FORCE`**. Toda la suite corría con ese rol.
  2. **La lectura cruzada era sobre datos sin commitear.** `assert_rls_isolation()` insertaba con `session_a` sin commitear y leía desde `session_b`, que es otra conexión en otra transacción. Lo que bloqueaba esa lectura era el aislamiento MVCC, no RLS. El test pasaba idéntico con las políticas desactivadas.
- **Por qué se tardó tanto en ver:** los dos defectos se enmascaraban mutuamente. Con el rol superusuario, RLS no filtraba nada — pero los asserts seguían pasando gracias a MVCC, así que nada delataba el bypass. Y como los asserts pasaban, nadie sospechaba del rol.
- **Cómo salió:** `tests/integration/test_document_pipeline.py` (Sprint 5) es el primer test del repo que **commitea** la fila y después la lee desde otro tenant, a través de los endpoints. `GET /api/v1/documents/{id}` de un documento ajeno devolvió 200 en vez de 404.
- **Defecto real que destapó:** `_get_document_or_404()` usaba `session.get()`, delegando el aislamiento entero a RLS. Contradice la restricción 2 de CLAUDE.md: las queries de seguridad deben ser explícitas. Corregido con un filtro por `client_id` en el WHERE; RLS queda como segunda barrera.
- **Estado:** CERRADO.
  - `ci.yml` crea `app_user` (`NOSUPERUSER NOBYPASSRLS`) después de las migraciones y la suite de integración se conecta con él. Alembic sigue corriendo como el dueño de las tablas.
  - `assert_rls_isolation()` simula el otro tenant cambiando `app.current_client_id` **dentro de la misma transacción** que hizo el INSERT. Esa transacción ve su propia fila sin commitear, así que lo único que puede ocultarla es la política. Si alguien desactiva RLS, ahora falla.
  - Dos tests-guardia en `test_document_pipeline.py` verifican la premisa: que las tablas tengan `ENABLE` + `FORCE` + política, y que el rol de conexión no sea superusuario ni tenga `BYPASSRLS`.
- **Lección transferible:** un test de aislamiento que nunca ha fallado no prueba nada. Antes de confiar en uno, hay que verlo fallar — desactivando la política, o comprobando que el mecanismo que debería bloquear es realmente el que bloquea. Aplica a los ~30 tests de RLS del repo y a cualquier test de permisos que se escriba de aquí en adelante.

### BUG-010: `RAGService` rompía contra Postgres real por falta de cast `::vector`
- **Descripción:** `app/services/rag.py::retrieve()` y `retrieve_few_shot_examples()` bindeaban `:query_embedding` como `str` en SQL crudo (`text()`), sin castear. Con el driver `asyncpg`, un bind param `str` se envía tipado como `text`, y `vector <=> text` no tiene cast implícito en PostgreSQL — la query falla al ejecutarse contra Postgres real. Mismo root cause que el `::vector` que ya había hecho falta agregar en `tests/integration/test_rls_all_tables.py` durante el cierre de BUG-005/BUG-006, pero nunca se aplicó en `rag.py` porque se escribió después, en Sprint 5.
- **Por qué no lo detectó ningún test:** `tests/unit/test_rag.py` sustituye la sesión por un `SpySession` que solo inspecciona el string SQL emitido — nunca lo ejecuta. Y no existía ningún test de integración para `RAGService`: `tests/integration/test_document_pipeline.py` (Sprint 5, Dev B) cubre documentos y CRUD, pero el retrieval no era responsabilidad de Dev B.
- **Cómo salió:** revisión general de bugs pedida por el usuario después de cerrar el Sprint 5, cruzando el código de `rag.py` contra el patrón ya aprendido en BUG-005/006 (mismo tipo de SQL crudo con pgvector y asyncpg).
- **Estado:** CERRADO ([PR #11](https://github.com/miguelggdev/Omnichannel-Platform/pull/11)). Cast `(:query_embedding)::vector` agregado en las 3 posiciones de cada método (SELECT, WHERE, ORDER BY), más `(:document_ids)::uuid[]` en el filtro `ANY()` por la misma razón. 5 tests de integración nuevos ejecutan `RAGService` contra Postgres real; verificado en CI (no solo el checkmark): **46 passed, 6 skipped** con el fix, vs. una query que directamente no ejecutaba antes.
- **Lección transferible:** con SQL crudo (`text()`) + pgvector + asyncpg, **todo** bind param de tipo `vector` necesita cast explícito `::vector` (y por la misma lógica, cualquier tipo no-estándar bindeado como lista para `ANY()`, como `uuid[]`, conviene castearlo también) — no basta con haberlo aprendido una vez en un archivo; hay que revisarlo en cada lugar nuevo que emita SQL crudo contra columnas `vector`. Un test unitario que solo inspecciona el string SQL (patrón `SpySession`) no reemplaza un test de integración contra Postgres real para código que ejecuta SQL crudo con tipos de extensión.

### BUG-011: El engine async de SQLAlchemy se reusaba entre `asyncio.run()` de tareas de Celery
- **Descripción:** `webhook_processor.py` y `document_ingestion.py` llaman `asyncio.run()` una vez por ejecución de tarea, pero `app/core/database.py::engine` es un singleton de módulo. Un worker prefork de Celery procesa muchas tareas secuenciales en el mismo proceso — cada `asyncio.run()` abre un event loop nuevo, pero el pool de conexiones del engine sobrevive entre llamadas. Mismo root cause que **BUG-006** (`RuntimeError: ... attached to a different loop`), pero ese se arregló solo en los fixtures de test; en el código de producción nunca se mitigó.
- **Por qué no lo detectó ningún test:** ningún test de integración invocaba la tarea real dos veces seguidas dentro del mismo proceso — cada test de CI usa su propio proceso/loop vía pytest-asyncio, que no reproduce el patrón de un worker Celery de vida larga.
- **Cómo salió:** revisión general de bugs pedida por el usuario después de cerrar el Sprint 5, notando que `webhook_processor.py`/`document_ingestion.py` repetían exactamente el patrón que BUG-006 ya había fichado como peligroso (engine de módulo + loop nuevo por llamada).
- **Estado:** CERRADO ([PR #12](https://github.com/miguelggdev/Omnichannel-Platform/pull/12)). `app/core/database.py::run_isolated(coro)` ejecuta la corrutina con `asyncio.run()` y dispone el pool en un `finally`, dentro del mismo loop que abrió las conexiones — así la próxima tarea del mismo worker arranca con el pool limpio. Los dos workers de Celery lo usan ahora en vez de `asyncio.run()` directo. Test unitario (`tests/unit/test_database.py`) simula dos tareas seguidas con dobles.
- **Lección transferible:** un patrón de bug ya fichado (BUG-006) puede reaparecer en código nuevo que repite la misma estructura (engine/recurso de módulo + `asyncio.run()` repetido) sin que nadie lo conecte, si el fix original solo se aplicó donde se descubrió (los tests) y no se generalizó al resto del código con el mismo riesgo (los workers de Celery). Vale la pena, al cerrar un bug de esta clase, revisar todo el repo por el mismo patrón, no solo el archivo donde apareció.

### BUG-012: Documentos sin contenido extraíble quedaban `completed` con `chunk_count=0`
- **Descripción:** `DocumentPipeline.process()` no distinguía "0 chunks porque el documento no tiene contenido" de "documento procesado con éxito": si la extracción (u OCR) no producía texto en ninguna página, igual marcaba `status="completed"`. Un escaneo ilegible o un archivo corrupto quedaban indistinguibles de un documento sin contenido relevante para RAG, sin ninguna señal visible para el usuario.
- **Cómo salió:** revisión general de bugs pedida por el usuario después de cerrar el Sprint 5.
- **Estado:** CERRADO ([PR #12](https://github.com/miguelggdev/Omnichannel-Platform/pull/12)). `process()` lanza `EmptyDocumentError` (nueva excepción en `document_pipeline.py`) antes de embeber/guardar si el chunker no produjo ningún chunk. `document_ingestion.py` la trata igual que `PipelineUnavailableError`: no se reintenta (el mismo archivo va a dar el mismo resultado vacío) y el documento queda `failed` con el motivo en `metadata.error`. Verificado contra Postgres real: `process()` no llega a marcar `completed` ni a guardar nada.
- **Nota relacionada, no arreglada:** al escribir el test de este fix se detectó que `app/services/document_pipeline.py::_TEXT_EXTRACTORS` no tiene entrada para `"txt"`, pese a que `documents.py::ALLOWED_TYPES` sí acepta `text/plain` → `txt` en la subida. Todo `.txt` subido hoy falla en `_extract_text()` con `ValueError: Tipo de archivo no soportado para extraccion: txt`, se reintenta 2 veces y termina `failed` — nunca llega siquiera a la lógica de `EmptyDocumentError`. Pendiente de decidir con el usuario (agregar un extractor trivial para texto plano, o quitar `txt` de `ALLOWED_TYPES` si no se va a soportar).

### BUG-013: Las notas automáticas del bot no caben en `internal_notes`
- **Descripción:** `specs/sprint-06-langgraph.md` §9 pide que el handoff a humano deje una `InternalNote` con el motivo y las métricas. `internal_notes.author_id` es **NOT NULL** y referencia `users`: una nota generada por el bot no tiene autor humano, y firmarla con un admin cualquiera del tenant le atribuiría algo que no escribió. El propio spec anota el problema ("considerar crear un system user por tenant, o hacer user_id nullable") sin resolverlo.
- **Workaround aplicado (Sprint 6, Dev B):** `human_handoff_node` escribe el motivo, el intent, la confianza del RAG y el uso de presupuesto en `conversations.metadata.handoff` (JSONB). Es igual de consultable, no falsea la autoría y no exige migración.
- **Estado:** ABIERTO — decisión pendiente del usuario. Dos salidas razonables: (a) un usuario de sistema por tenant, creado en el onboarding, que firme las notas automáticas; (b) migración que haga `internal_notes.author_id` nullable. La (a) mantiene la integridad referencial; la (b) es más simple pero obliga a que toda la UI contemple notas sin autor.
- **Impacto si no se resuelve:** ninguno funcional — el motivo del handoff no se pierde. Lo que falta es que esas notas aparezcan en el hilo de notas del contacto en el panel (Sprint 15).

### BUG-014: `ai_processor` nunca se sumó a `TASK_MODULES`
- **Descripción:** [PR #13](https://github.com/miguelggdev/Omnichannel-Platform/pull/13) (Sprint 6, Dev B) agregó `app/tasks/ai_processor.py` con la tarea `app.tasks.ai_process_response`, pero `app/tasks/celery_app.py::TASK_MODULES` — la lista que el worker importa al arrancar para registrar sus tareas — no se actualizó. Un worker de la cola `ai_inference` levantado con esa lista incompleta nunca importa el módulo, nunca registra la tarea, y todo lo que `webhook_processor.py::_enqueue_ai_processing()` encole ahí queda `NotRegistered` en silencio: ningún mensaje llega jamás al grafo de conversación, sin ningún error visible en los logs del encolador.
- **Por qué no lo detectó ningún test:** ningún test verificaba `TASK_MODULES` contra las tareas reales que expone cada módulo — exactamente el mismo hueco que dejó pasar el bug equivalente en Sprint 4.
- **Cómo salió:** revisión del código de Dev B al implementar `app/agents/graph.py` (Sprint 6, Dev A), notando que el archivo nuevo no aparecía en `celery_app.py`.
- **Estado:** CERRADO ([PR #14](https://github.com/miguelggdev/Omnichannel-Platform/pull/14)). `ai_processor` agregado a `TASK_MODULES`. `tests/unit/test_celery_app.py` agrega la regresión: importa `TASK_MODULES` y verifica que las tareas de los tres workers (`webhook_process_incoming`, `document_ingest`, `ai_process_response`) queden registradas.
- **Lección transferible:** es la segunda vez que este mismo archivo (`celery_app.py::TASK_MODULES`) se queda corto cuando se agrega un módulo de tarea nuevo (la primera fue Sprint 4). Vale la pena tratarlo como una lista que necesita su propio test de regresión, no confiar en que se recuerde a mano cada vez.

### BUG-015: `config.enabled_agents: []` (deshabilitar todo a propósito) se trataba igual que "no configurado"
- **Descripción:** `app/agents/nodes/_tenant.py::_as_agents()` normaliza `config.enabled_agents` a una tupla. La versión original hacía `if agentes: return agentes` — una lista vacía (después de filtrar) caía al `else` y devolvía `DEFAULT_ENABLED_AGENTS` (`("rag",)`), exactamente igual que si el tenant nunca hubiera tocado `enabled_agents`. Un tenant que configura `enabled_agents: []` a propósito (para forzar que todo mensaje vaya a un humano, sin ningún agente automático) seguía viendo el intent `rag_query` disponible y las consultas seguían intentando responderse solas.
- **Por qué no lo detectó ningún test:** ningún test de Sprint 6 ejercitaba `_as_agents()` directamente — todos sustituyen `get_agent_settings()` entero por un `AgentSettings` fijo (`agent_doubles.parchear_agent_settings`), así que nunca pasan por la normalización real del JSONB.
- **Cómo salió:** revisión de bugs pedida por el usuario al cerrar el Sprint 6.
- **Estado:** CERRADO. `_as_agents()` ahora distingue "ausente / tipo inválido" (cae al default) de "lista, aunque vacía después de filtrar" (se respeta tal cual, incluida una tupla vacía). `tests/unit/test_tenant_settings.py` agrega la cobertura directa que faltaba, incluido el caso de punta a punta contra `get_agent_settings()`.
- **Lección transferible:** al normalizar un valor opcional con un default, "vacío" y "ausente" no son lo mismo — una lista vacía explícita puede ser una configuración deliberada. `if valor:` colapsa esa distinción; hay que comprobar el tipo/presencia, no la verdad del valor ya procesado.

### BUG-016: El bot seguía respondiendo a conversaciones ya escaladas a un humano
- **Descripción:** `webhook_processor.py::_resolve_conversation()` reutiliza cualquier conversación activa cuyo `status` no esté en `CLOSED_STATUSES` (`resolved`, `archived`) — eso incluye `human_active` y `waiting_human`. `_process_message()` encolaba el procesamiento de IA de forma incondicional para cualquier mensaje nuevo, y `ai_processor.py::process_ai_response()` nunca leía `conversations.status`: arma el estado inicial y llama al grafo directo. Resultado: un contacto ya escalado a un agente humano (`waiting_human`) o siendo atendido en ese momento (`human_active`) que volvía a escribir seguía recibiendo una respuesta automática del bot, como si nadie lo hubiera tomado — y, en el peor caso, el grafo podía disparar otro handoff sobre una conversación que ya estaba escalada.
- **Por qué no lo detectó ningún test:** el `TestEncoladoDeIA`/`TestProcessMessage` de Sprint 6 solo cubrían conversaciones nuevas (`bot_active`); ningún test ejercitaba `_process_message()` con una conversación existente en `human_active`/`waiting_human`.
- **Cómo salió:** revisión del [PR #16](https://github.com/miguelggdev/Omnichannel-Platform/pull/16) (Sprint 7, Dev B). El PR en sí no lo causó — es un hueco de Sprint 6 (`webhook_processor.py`/`ai_processor.py`) que la nueva máquina de estados de `ConversationLifecycle` dejó en evidencia — pero se corrigió aparte, sin bloquear ese PR.
- **Estado:** CERRADO ([PR #17](https://github.com/miguelggdev/Omnichannel-Platform/pull/17), mergeado en `main`). `webhook_processor.py` agrega `HUMAN_OWNED_STATUSES = ("human_active", "waiting_human")`: `_process_message()` captura `conversation.status` dentro de la transacción y, si está en ese conjunto, no llama a `_enqueue_ai_processing()` (el mensaje igual queda guardado). `tests/unit/test_webhook_processor.py::TestNoEncolaConversacionHumana` cubre los dos estados que cortan y los tres que siguen yendo al grafo (`new`, `bot_active`, `waiting_client`). Verificado en CI real: 285 unitarios passed/1 skipped, 57 de integración passed/6 skipped.
- **Lección transferible:** cuando dos módulos comparten un concepto (aquí, el status de la conversación) pero uno de ellos no lo consulta nunca, el gap no aparece en ningún test unitario de cualquiera de los dos por separado — hace falta un test que ejercite el flujo completo con el estado "raro" puesto a propósito.

### BUG-017: El login no funciona contra un rol sujeto a RLS
- **Descripción:** `app/api/v1/auth.py::login` busca al usuario por email con `AsyncSessionLocal()`, **sin** contexto de tenant — y no puede tenerlo: el tenant se deduce del usuario, y el usuario todavía no se conoce. La política de RLS de `users` (migración 002) es `USING (client_id = current_setting('app.current_client_id')::uuid)`, así que esa consulta evalúa un parámetro que en esa transacción no vale nada y la consulta revienta. Contra un rol con `NOBYPASSRLS` —el de CI (`app_user`) y el que debe usar la aplicación en producción— **ningún login funciona**.
- **Error concreto:** `invalid input syntax for type uuid: ""`, no `unrecognized configuration parameter` que sería lo esperable. El motivo es sutil y vale la pena recordarlo: en cuanto **otra** transacción del mismo backend ejecuta `set_config('app.current_client_id', ..., true)`, el parámetro queda definido para la conexión; al revertirse al final de esa transacción vuelve a **cadena vacía**, no a inexistente. Con un pooler por delante reutilizando conexiones, ese es el estado normal.
- **Por qué no lo detectó ningún test:** `tests/unit/test_auth.py` sustituye la sesión entera por un mock, así que nunca ejerció una política de RLS. Ningún test de integración tocaba el login: la suite de integración autentica generando el JWT directamente con `create_access_token()`, sin pasar por el endpoint.
- **Cómo salió:** al decidir si `users` podía llevar trigger de auditoría (Sprint 8, spec §9.2). Se escribió `tests/integration/test_audit_gdpr.py::TestLoginBajoRls` para comprobarlo contra Postgres real en vez de razonarlo sobre el papel, y falló.
- **Estado:** ABIERTO. El test queda como `xfail(strict=True)`: el día que se arregle, pasará y CI fallará por xpass, avisando de que hay que quitar el marcador y cerrar este bug.
- **Impacto:** crítico en producción si la aplicación corre con un rol sujeto a RLS. Hoy no se ha manifestado porque ningún entorno desplegado ha ejercitado el login contra ese rol.
- **Salidas razonables (decisión pendiente, no es de un sprint de observabilidad):**
  - (a) Una función `SECURITY DEFINER` que resuelva `email -> (user, client_id)` y sea el único punto con acceso sin contexto. Mantiene la RLS intacta para todo lo demás y acota la excepción a una firma concreta.
  - (b) Una política adicional en `users` que permita leer cuando no hay contexto de tenant. Más simple, pero abre una vía de lectura cross-tenant sobre la tabla de usuarios: hay que acotarla con cuidado (por ejemplo, solo las columnas que el login necesita).
  - (c) Un rol de autenticación aparte con `BYPASSRLS`, usado solo por el endpoint de login. Es lo que menos código toca y lo que más superficie privilegiada añade.
- **Relacionado:** por esto la migración 004 deja `users` sin trigger de auditoría. Auditarla ahora taparía este bug detrás de un error distinto (el trigger intentaría insertar en `audit_logs`, cuya política evalúa el mismo parámetro). Cuando se cierre, agregar ese trigger es una migración de una línea.

### PAT-001: Webhook idempotency con deduplicación
- **Patrón:** Antes de procesar un webhook entrante, verificar `(channel, external_message_id)` en tabla `webhook_dedup`. Si existe, retornar 200 sin procesar. Si no, insertar y procesar.
- **Razón:** Los proveedores de mensajería (YCloud, Twilio, Meta) pueden reenviar webhooks por timeouts o errores de red.
- **TTL:** Las entradas de dedup se limpian después de 72 horas.

### PAT-002: NormalizedMessage como contrato interno
- **Patrón:** Todo mensaje entrante se normaliza a `NormalizedMessage(channel, sender, text, media_url, timestamp, metadata)` inmediatamente en el webhook receiver, antes de cualquier lógica de negocio.
- **Razón:** Desacopla la lógica de negocio del formato específico de cada proveedor de mensajería.
- **Implementado:** Sprint 4, Dev A — `app/schemas/message.py`. `ChannelEnum`/`MessageTypeEnum` usan `enum.StrEnum`, no `class X(str, Enum)` — Python 3.11+ y `ruff` (regla UP042) piden `StrEnum` directamente; el comportamiento con Pydantic es identico.

### PAT-002b: MetaProvider no debe exigir credenciales en el constructor
- **Patrón:** `MetaProvider.__init__(provider_config)` usa `provider_config.get("page_access_token", "")` / `.get("app_secret", "")`, no indexación directa (`provider_config["..."]`).
- **Razón:** `_resolve_provider()` en `app/api/v1/webhooks.py` (Dev B) construye el provider con `{"channel": channel}` únicamente en el camino real de recepción de webhooks — nunca pasa `page_access_token` ni `app_secret` ahí. `specs/sprint-04-webhooks.md` §4 muestra el constructor con indexación directa (`provider_config["page_access_token"]`), lo que revienta con `KeyError` en todo mensaje entrante de Meta si se sigue al pie de la letra. Ninguno de los dos valores se usa desde `self` en el resto de la clase: `send_message`/`send_template` los reciben por `channel_config` en cada llamada, y `validate_signature` recibe el secreto como parámetro (`settings.META_APP_SECRET`, resuelto por el endpoint).
- **Detectado:** Sprint 4, Dev A, sesión 12, al contrastar el pseudocódigo de la spec contra la llamada real que ya estaba en `main` (`webhooks.py::_resolve_provider`).

### PAT-003: ConversationState como TypedDict inmutable por paso
- **Patrón:** El estado del grafo LangGraph es un `TypedDict` que se pasa entre nodos. Cada nodo retorna un nuevo dict parcial que se mergea (no muta el original).
- **Razón:** LangGraph maneja el estado de forma funcional. Mutar el estado directamente causa bugs sutiles en checkpointing y replay.

### PAT-004: Traefik como API Gateway con rate limiting por tenant
- **Patrón:** Traefik v3 con middleware de rate limiting configurado por labels de Docker. Cada servicio expone su propio rate limit basado en el plan del tenant.
- **Razón:** Evita que un tenant abuse del sistema y afecte a otros. El rate limiting se aplica antes de llegar a FastAPI.

### PAT-005: Chunking por caracteres, no por tokens, pese al pseudocódigo del spec
- **Patrón:** `DocumentChunker` usa `RecursiveCharacterTextSplitter` con su `length_function` por defecto (`len`, cuenta caracteres). No se sobreescribe con una función basada en tokens.
- **Razón:** `specs/sprint-05-rag.md` §4 sugiere en su pseudocódigo un `length_function` basado en tokens de tiktoken. Pero el test de contrato que Dev B ya había comprometido (`tests/unit/test_rag.py::TestChunking::test_respeta_el_tamano_maximo`) verifica `len(chunk.content) <= chunk_size * 1.2`, es decir caracteres. Seguir el spec al pie de la letra habría roto ese test. `token_count` se calcula aparte con `tiktoken` (`cl100k_base`), solo como metadata para `document_chunks.token_count` — no influye en dónde se corta.
- **Detectado:** Sprint 5, Dev A, sesión 13, al leer `test_rag.py` antes de implementar (práctica ya establecida: el test comprometido es la fuente de verdad, no el pseudocódigo del spec).

### PAT-006: PyMuPDF en vez de pdf2image para rasterizar PDFs
- **Patrón:** `OCRService._pdf_to_images()` usa `pymupdf.open(...).get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))` a 300dpi, no `pdf2image.convert_from_bytes()`.
- **Razón:** `pdf2image` depende del binario de sistema `poppler-utils`, que no está instalado en el `Dockerfile` (Sprint 2). Agregarlo es más invasivo que sumar `pymupdf`, que trae su propio motor de render (MuPDF) embebido como dependencia de pip pura, sin binarios de sistema adicionales.
- **Detectado:** Sprint 5, Dev A, sesión 13.

### PAT-007: Overrides de mypy para libs sin stubs van en el módulo que llama, no en la lib
- **Patrón:** Cuando una librería sin stubs (PyMuPDF, pytesseract) tiene alguna firma parcialmente tipada que dispara `disallow_untyped_calls`, el override de `pyproject.toml` debe listar el **módulo propio que la llama** (`app.services.ocr`, `app.services.document_pipeline`), no la librería (`pymupdf.*`).
- **Razón:** Un override de mypy se aplica al código del módulo listado que mypy está chequeando, no a quién lo importa. Poner `disallow_untyped_calls = false` en `pymupdf.*` no silencia nada porque mypy no analiza el código interno de esa librería con esa opción — sigue exigiendo tipos estrictos en el módulo llamante. Mismo patrón que ya existía para `app.tasks.*`/`disallow_untyped_decorators` con `@shared_task` de Celery.
- **Detectado:** Sprint 5, Dev A, sesión 13 — primer intento (override en `pymupdf.*`) no eliminó los errores; corregido apuntando a los módulos llamantes.

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
### Fase 3 (Lead Management) — 10 tablas
| # | Tabla | Propósito |
|---|---|---|
| 27 | `lead_pipeline_stages` | Etapas configurables del pipeline por tenant |
| 28 | `leads` | Entidad principal de lead (extiende contact con datos de ventas) |
| 29 | `lead_sources` | Fuentes de captura configuradas por tenant |
| 30 | `lead_activities` | Log de actividades por lead (llamadas, emails, cambios de etapa) |
| 31 | `lead_scores` | Historial de scoring (FIT, behavioral, AI) |
| 32 | `lead_sequences` | Secuencias de follow-up automatizadas |
| 33 | `lead_sequence_steps` | Pasos individuales de cada secuencia |
| 34 | `lead_sequence_enrollments` | Leads inscritos en secuencias activas |
| 35 | `deals` | Oportunidades de venta con valor y probabilidad |
| 36 | `scheduled_calls` | Llamadas agendadas (IA o humanas) |

### Enums importantes
- **conversation_status:** `new`, `bot_active`, `human_active`, `waiting_human`, `waiting_client`, `resolved`, `archived`
- **message_direction:** `inbound`, `outbound`
- **message_type:** `text`, `image`, `audio`, `video`, `document`, `location`, `template`, `interactive`
- **user_role:** `super_admin`, `admin`, `supervisor`, `agent`
- **lead_stage_type:** `new`, `enriched`, `qualified`, `assigned`, `follow_up`, `meeting_scheduled`, `proposal`, `negotiation`, `won`, `lost`, `disqualified`
- **lead_source_type:** `web_form`, `linkedin`, `facebook_ad`, `google_ad`, `instagram`, `referral`, `manual`, `api`, `whatsapp`, `import`
- **deal_stage:** `new_contact`, `qualified`, `proposal`, `negotiation`, `closed_won`, `closed_lost`
- **call_type:** `ai_voice`, `human`, `hybrid`
- **call_status:** `pending`, `confirmed`, `in_progress`, `completed`, `no_show`, `cancelled`, `rescheduled`

---

## Contexto de Infraestructura

### Servicios Docker Compose (Sprint 2) — Actualizado por ADR-020
> PostgreSQL, Auth, Storage y Realtime los provee Supabase Cloud.
> docker-compose.yml solo orquesta los servicios propios.

1. `traefik` — API Gateway, TLS, routing
2. `api` — FastAPI application (2+ réplicas)
3. `redis` — Cache + message broker
4. `celery-webhooks` — Worker cola webhooks
5. `celery-ai` — Worker cola AI inference
6. `celery-documents` — Worker cola documentos
7. `celery-notifications` — Worker cola notificaciones
8. `celery-bulk` — Worker cola bulk operations
9. `celery-beat` — Scheduler periódico
10. `prometheus` — Métricas
11. `grafana` — Dashboards
12. `celery-lead-enrichment` — Worker cola enrichment de leads (Sprint 6)

### Variables de entorno críticas — Actualizado por ADR-020
- `DATABASE_URL` — Connection string via Supavisor Transaction Pooler (puerto 6543, para la app)
- `DATABASE_URL_DIRECT` — Conexión directa a PostgreSQL (puerto 5432, solo para migraciones Alembic)
- `SUPABASE_PROJECT_REF` — Referencia del proyecto Supabase Cloud
- `SUPABASE_URL` — URL del proyecto Supabase Cloud
- `SUPABASE_PUBLISHABLE_KEY` — API key pública (formato v2: sb_publishable_*)
- `SUPABASE_SECRET_KEY` — API key secreta (formato v2: sb_secret_*)
- `REDIS_URL` — Redis connection string
- `OPENAI_API_KEY` — API key de OpenAI
- `YCLOUD_API_KEY` — API key de YCloud
- `JWT_SECRET` — Secreto para tokens JWT (gestionado por Supabase Cloud)
- `ENCRYPTION_KEY` — Clave para pgcrypto
- `TELEGRAM_BOT_TOKEN` — Token del bot de Telegram para monitoreo
- `TELEGRAM_ADMIN_CHAT_ID` — Chat ID del super admin para alertas
- `CLOUDFLARE_API_TOKEN` — Token de Cloudflare para WAF/Tunnel
- `CLOUDFLARE_ZONE_ID` — Zone ID de Cloudflare
- `META_APP_SECRET` — App Secret de Meta (Facebook/Instagram)
- `META_PAGE_ACCESS_TOKEN` — Page Access Token de Meta
- `META_WEBHOOK_VERIFY_TOKEN` — Token de verificación de webhooks Meta
- `ANTHROPIC_API_KEY` — API key de Anthropic (Claude) para Admin Assistant
- `ADMIN_ASSISTANT_MODEL` — Modelo Claude a usar (default: claude-sonnet-4-20250514)
- `ADMIN_ASSISTANT_MAX_TOKENS` — Máximo de tokens por respuesta del asistente (default: 1024)
- `ADMIN_ASSISTANT_RATE_LIMIT` — Límite de mensajes por minuto por admin (default: 20)
#### Variables Lead Management (Sprints 5-8)
- `CLEARBIT_API_KEY` — API key de Clearbit (enrichment de empresas y contactos)
- `HUNTER_API_KEY` — API key de Hunter.io (verificación de emails)
- `VAPI_API_KEY` — API key de Vapi.ai (llamadas con voz IA)
- `VAPI_PHONE_NUMBER` — Número de teléfono Vapi.ai
- `BLAND_AI_API_KEY` — API key de Bland.ai (llamadas con voz IA, alternativa)
- `BLAND_AI_PHONE_NUMBER` — Número de teléfono Bland.ai
- `PHANTOMBUSTER_API_KEY` — API key de PhantomBuster (scraping LinkedIn)
- `FB_ADS_ACCESS_TOKEN` — Access Token de Facebook Ads API (captura de leads)
- `GOOGLE_ADS_API_KEY` — API key de Google Ads (captura de leads)

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
| 2026-09-06 | Sesión 8 | Sprint 2 completo: docker-compose.yml (16 servicios), Dockerfile multi-stage, Traefik v3 (static + dynamic config + TLS), Celery config (5 colas + beat schedule), wait-for-it.sh, .dockerignore, .env.example actualizado (ANTHROPIC_API_KEY, ADMIN_ASSISTANT_*, META vars, REALTIME_SECRET_KEY_BASE). Actualización de Admin Assistant spec con patrón WebMCP/UI Actions (F6) |
| 2026-09-08 | Sesión 9 | Merge PR #1 (Supabase Cloud migration, ADR-020) a main. Diseño completo del módulo Lead Management (Sprints 5-8): análisis de BuilderX/AI CRM, pipeline de 8 etapas (CAPTURA→ENRIQUECE→CALIFICA→ASIGNA→FOLLOW-UP→AGENDA→MIDE→CIERRA), 10 tablas nuevas (#27-36), triple scoring (FIT+Behavioral+AI), secuencias multi-canal con RAG, integración Vapi/Bland.ai para voz IA, theming configurable por tenant. 5 ADRs nuevos (#021-025). Spec en `specs/sprint-16-19-lead-management.md` |
| 2026-09-14/15 | Sesiones 11-13 | Sprint 4 y 5 cerrados de punta a punta. PR #5/#4/#7/#8/#9/#10 revisados y mergeados. BUG-005 (RLS nunca habilitado en Alembic) y BUG-006 (4 bugs nunca ejecutados por `--run-db` silenciosamente saltado) resueltos. BUG-009 (RLS testeando MVCC + rol superusuario en CI) encontrado por Dev B, revisado y verificado a fondo. Sprint 4 Dev A (messaging providers) y Sprint 5 Dev A (chunker/embedding/OCR/pipeline/RAG) implementados y entregados vía PR #8 y #10. Ver PAT-002b, BUG-009, PAT-005/006/007 |

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


### ADR-020: Supabase Cloud en lugar de self-hosted (supersede ADR-007)

- **Fecha:** 2026-09-07
- **Contexto:** ADR-007 justificó self-hosted citando "limitaciones de extensiones (pgvector, pgcrypto)" en Supabase Cloud. Esa restricción no está vigente: pgvector y pgcrypto son extensiones estándar de Supabase Cloud, habilitables desde Database → Extensions en el dashboard — pgvector es de hecho un producto propio de Supabase ("Supabase Vector"). No hay limitación real para estos dos casos.
- **Decisión:** Usar Supabase Cloud (proyecto administrado) en lugar de self-hosted. La app se conecta vía el Transaction Pooler de Supavisor (puerto 6543 — mismo rol que pgBouncer, misma razón de ADR-001/BUG-002 sobre `SET LOCAL`). Las migraciones de Alembic corren contra la conexión directa (`DATABASE_URL_DIRECT`). Auth, Storage y Realtime los provee el proyecto Cloud.
- **Consecuencia:**
  - Sprint 2 ya no incluye "Configs de Supabase self-hosted" (`supabase/docker/`), ni los servicios Docker #3–7 de la lista original (supabase-db, supabase-auth, supabase-storage, supabase-realtime, pgbouncer) — los reemplaza el proyecto Cloud.
  - `PGBOUNCER_URL` se reemplaza por `DATABASE_URL` (pooler) y `DATABASE_URL_DIRECT` (migraciones) — ver `.env.example` actualizado.
  - El DDL de Sprint 1 (antes `supabase/init/init.sql`) pasa a vivir como migración de Alembic en `migrations/versions/`.
  - Se pierde configuración avanzada de PostgreSQL fuera de lo que expone Supabase Cloud — esto sí es una limitación real. Revisar si ADR-012 (streaming replication a VPS secundario) sigue siendo necesario, dado que Supabase Cloud ya incluye point-in-time recovery gestionado.
  - **Importante:** el trabajo de Sprint 1 y 2 registrado en PROGRESS.md como "Completado" (docker-compose de 16 servicios, `init.sql` de 520+ líneas, etc.) fue diseñado sobre el esquema self-hosted en una sesión previa y — según el propio PROGRESS.md — nunca se pusheó al repo. Antes de subirlo hay que ajustarlo a este ADR, o se reintroduce todo lo que este cambio elimina.

### ADR-021: Lead Management como módulo activable por tenant
- **Fecha:** 2026-09-08
- **Contexto:** No todos los tenants necesitan lead management. Agregarlo a todos incrementa complejidad de UI y costo.
- **Decisión:** Campo `lead_management_enabled` en `clients`. El middleware solo carga rutas de leads si está habilitado. El onboarding crea pipeline stages por defecto al activar.
- **Consecuencia:** Los endpoints de leads retornan 403 si el módulo no está habilitado para el tenant.

### ADR-022: Lead scoring triple (FIT + Behavioral + AI)
- **Fecha:** 2026-09-08
- **Contexto:** BuilderX usa solo FIT Score. Un score único no captura engagement ni contexto conversacional.
- **Decisión:** 3 dimensiones con pesos configurables por tenant (default 40/30/30):
  - **FIT Score:** Match con ICP (industria, tamaño, cargo, región). Estático.
  - **Behavioral Score:** Engagement (respuestas, velocidad, clicks). Dinámico.
  - **AI Score:** Claude/GPT analiza conversaciones y da score + reasoning. Periódico.
- **Consecuencia:** `total_score` es columna GENERATED ALWAYS. Recalculación: FIT al enriquecer, Behavioral en cada interacción, AI cada 24h o al cambiar de etapa.

### ADR-023: Secuencias de follow-up multi-canal con RAG
- **Fecha:** 2026-09-08
- **Contexto:** El follow-up genérico tiene baja tasa de respuesta. Personalizar con el knowledge base del negocio mejora conversión.
- **Decisión:** El nodo LangGraph de generación de mensajes de follow-up usa: contexto del lead, RAG del knowledge base del tenant, few-shot de approved_responses, y canal óptimo del lead.
- **Consecuencia:** Cada mensaje de follow-up consume tokens (controlado por TokenBudgetGuard). La cola `ai_inference` procesa la generación.

### ADR-024: Vapi + Bland.ai como providers de voz IA (patrón ABC)
- **Fecha:** 2026-09-08
- **Contexto:** Se necesitan llamadas con voz IA para follow-up y calificación. Depender de un solo proveedor es riesgo.
- **Decisión:** Crear `VoiceCallProvider` ABC con métodos: `initiate_call`, `get_status`, `get_recording`, `get_transcript`. Implementaciones: `VapiProvider`, `BlandAiProvider`. Factory resuelve por config del tenant.
- **Consecuencia:** Patrón idéntico a `MessagingProvider` (ADR-003). Cada tenant configura su proveedor de voz. Los recordings se almacenan en Supabase Storage.

### ADR-025: Theming configurable por tenant
- **Fecha:** 2026-09-08
- **Contexto:** El frontend necesita soportar dark/orange (inspirado en BuilderX), dark/blue, light mode, toggle, y acento configurable.
- **Decisión:** `clients.theme_config` JSONB con: `mode` (dark/light/system), `accent` (hex color), `variant` (default/compact). El frontend Next.js usa CSS custom properties + next-themes.
- **Consecuencia:** Las CSS variables se generan dinámicamente desde `theme_config`. shadcn/ui soporta theming nativo con HSL variables.

### ADR-026: accessLog de Traefik a stdout en lugar de fichero
- **Fecha:** 2026-09-09
- **Contexto:** `specs/sprint-02-docker.md` §3 especifica `accessLog.filePath: "/var/log/traefik/access.log"`. `docker-compose.yml` no declara ningún volumen para esa ruta, así que los logs quedarían dentro del contenedor y se perderían en cada reinicio, además de crecer sin rotación.
- **Decisión:** Mantener `accessLog` en stdout (formato json, sin `filePath`) — desviación consciente de la spec. Los recoge el driver de logging de Docker, que es la práctica estándar en contenedores y el camino natural hacia la agregación de logs del Sprint 8.
- **Consecuencia:** La spec del Sprint 2 queda desalineada en este punto; este ADR es la justificación. Si en Sprint 8 se decide agregar logs a fichero, se hace con un volumen nombrado (`traefik_logs`) y rotación explícita, no con el `filePath` suelto de la spec.

### ADR-027: ping y métricas Prometheus habilitados en Traefik
- **Fecha:** 2026-09-09
- **Contexto:** El healthcheck de `docker-compose.yml` para traefik es `["CMD","traefik","healthcheck"]`, comando que consulta el endpoint `/ping`. `traefik/traefik.yml` no habilitaba `ping`, por lo que el servicio nunca alcanzaba estado `healthy` y fallaba el criterio de aceptación "todos los servicios healthy en menos de 2 minutos".
- **Decisión:** Habilitar `ping` y `metrics.prometheus` sobre el entryPoint interno `traefik` (puerto 8080, ya expuesto por `api.insecure: true`). Prometheus scrapea `traefik:8080/metrics`.
- **Consecuencia:** El puerto 8080 queda sirviendo dashboard, `/ping` y `/metrics`. En producción hay que cerrarlo al exterior (`api.insecure: false` + router autenticado), junto con el hardening del Sprint 8.
