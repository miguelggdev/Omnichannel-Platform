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

### ADR-040: Google Calendar con un service account global, no OAuth2 por tenant
- **Fecha:** 2026-09-17
- **Contexto:** `specs/sprint-07-scheduling-crm.md` §3 modela un flujo OAuth2: cada tenant autoriza vía consent screen, el refresh token se guarda cifrado con `pgp_sym_encrypt` en `agent_configs.settings.oauth_refresh_token_encrypted`. Ese cifrado (`app/core/encryption.py`) es un entregable de **Sprint 8** que todavía no existe, y `agent_configs.settings`/`agent_type`/`is_enabled` no son columnas reales (ver ADR previo de Sprint 6 en `app/agents/nodes/_tenant.py`: hay una sola fila de config por tenant, con `config` JSONB).
- **Decisión:** un único **service account** de Google para toda la plataforma, configurado una vez vía `.env` (`GOOGLE_CALENDAR_CREDENTIALS_JSON`, JSON en base64; `GOOGLE_CALENDAR_ID` como fallback). Cada tenant comparte su calendario de Google con el email del service account y guarda su `calendar_id`/`timezone` — ninguno de los dos sensible — sin cifrar en `agent_configs.config.scheduling`.
- **Razón:** es la opción que la propia spec recomienda para producción ("Service Account: no requiere interacción del usuario") y `.env.example` ya la anticipaba con esas dos variables, comentadas bajo "Google Calendar (Sprint 7)", antes de que este sprint arrancara. Evita inventar un cifrado ad-hoc que Sprint 8 tendría que rehacer con el mecanismo estándar del proyecto.
- **Consecuencia:** `app/services/calendar.py::GoogleCalendarService.from_tenant()` no necesita descifrar nada; si `GOOGLE_CALENDAR_CREDENTIALS_JSON` falta, levanta `CalendarCredentialsError` (config global rota, afecta a todos los tenants). Si un tenant no configuró `calendar_id`, levanta `SchedulingNotConfiguredError` (solo afecta a ese tenant). El nodo `scheduling` atrapa ambas y escala a un humano (`handoff_reason="scheduling_unavailable"`) en vez de dejar que el turno falle.

### ADR-041: `ContactUnifier` solo implementa `merge()`, no `resolve_contact()`
- **Fecha:** 2026-09-17
- **Contexto:** `specs/sprint-07-scheduling-crm.md` §8 diseña `ContactUnifier` con dos métodos: `resolve_contact()` (buscar o crear un contacto por `channel`+`identifier_value` al recibir un mensaje) y `merge()` (fusión manual vía API). `app/tasks/webhook_processor.py::_resolve_contact()` ya hace exactamente lo primero desde Sprint 4, con su propia protección de ciclos de merge, probado en producción desde entonces.
- **Decisión:** `app/services/contact_unifier.py` solo implementa `merge()`, el único método que `app/api/v1/contacts.py::merge_contacts()` (ya entregado por Dev B) consume. El flujo automático de resolución sigue viviendo únicamente en `webhook_processor.py`.
- **Razón:** reimplementar `resolve_contact()` en `ContactUnifier` crearía dos versiones de la misma lógica (misma consulta, misma cadena de merge) que se pueden desincronizar con el tiempo — el clásico problema de tener una sola fuente de verdad duplicada en dos sitios.
- **Consecuencia:** tampoco se implementa `pgp_sym_encrypt`/`pgp_sym_decrypt` para `identifier_value` (mismo motivo que ADR-040: el cifrado real es Sprint 8) ni una columna `is_primary` que el modelo real no tiene. `merge()` nunca toca `identifier_value`, solo reasigna `contact_id`, así que no depende de si esa columna termina cifrada.

### ADR-042: El middleware de Agent Activity Logging abre su propia `tenant_session()`, no una inyectada en el estado
- **Fecha:** 2026-09-17
- **Contexto:** `specs/sprint-07-addendum-agent-logging.md` §4 espera `state["_db_session"]`: una sesión de SQLAlchemy inyectada en el `ConversationState` que el decorator `logged_node()` reutilizaría para escribir el log. Esa sesión no existe — el propio spec lo advierte en su nota de reprogramación (2026-09-16) — y cada nodo real de Sprint 6 resuelve su propia `tenant_session()` internamente (`app/agents/nodes/_tenant.py` y el resto del paquete).
- **Decisión:** `app/agents/middleware/logging_middleware.py::logged_node()` abre su **propia** `tenant_session()`, después de que el nodo envuelto termina (éxito o excepción), exclusivamente para escribir la entrada de `agent_action_logs`. Es best-effort: si esa escritura falla, se registra con `logger.exception()` y se descarta — nunca propaga.
- **Razón:** una tabla de auditoría/observabilidad no debe poder tumbar la respuesta al contacto. Si Postgres tiene un blip justo al escribir el log (después de que el nodo ya generó una respuesta válida), perder la traza de esa acción es aceptable; perder la respuesta no lo es.
- **Consecuencia:** cada nodo hace, en el peor caso, una transacción extra corta por turno solo para el log (aparte de la suya propia, si abre una). `_tokens_used`/`_model_used` en el resultado del nodo son opcionales: hoy ningún nodo de Sprint 6 los agrega a su dict de retorno, así que `tokens_used`/`model_used` quedan en `0`/`None` hasta que algún nodo los reporte a propósito.

### ADR-043: El autor de un cambio viaja en un ContextVar, no en un parámetro
- **Fecha:** 2026-09-17
- **Contexto:** el rastro de auditoría lo escribe un trigger de PostgreSQL (migración 006). El trigger sabe a qué tenant pertenece cada cambio porque lo lee de la propia fila, pero no *quién* lo hizo: eso solo lo sabe la aplicación, y tiene que pasárselo por `app.current_user_id`.
- **Alternativa descartada:** añadir un parámetro `user_id` obligatorio a `tenant_session()`. Son ~30 llamadas; cualquiera que olvidara pasarlo dejaría un hueco silencioso en el rastro — un cambio hecho por una persona registrado como acción del sistema. Y nada lo delataría: el rastro seguiría escribiéndose, solo que mintiendo.
- **Decisión:** `app/core/database.py` expone `current_user_id: ContextVar[UUID | None]`, que `AuditContextMiddleware` (`app/middleware/audit.py`) puebla por petición y `tenant_session()` lee por defecto. El parámetro explícito sigue existiendo y tiene prioridad, para los casos en que haga falta forzarlo.
- **Consecuencia:** ninguna llamada existente cambia y todas heredan el usuario de su petición. Lo que corre fuera de una petición (un worker de Celery) queda en `None`, que es exactamente lo que debe registrarse: una acción del sistema no la hizo nadie. El `reset()` del middleware no es opcional — sin él, el valor se queda pegado al contexto reutilizado y un cambio sin autenticar podría acabar atribuido al último usuario que pasó por ahí.

### ADR-044: `audit_logs` se borra en cascada con su tenant
- **Fecha:** 2026-09-17
- **Contexto:** la primera versión de la migración de auditoría declaró la FK `audit_logs.client_id -> clients.id` sin `ondelete`. En cuanto el trigger empezó a dejar filas, el `DELETE FROM clients` de la limpieza de **cualquier** test de integración empezó a fallar por una tabla que ese test nunca escribió (39 errores de teardown en CI, repartidos por `test_crm_api`, `test_graph_flow` y `test_webhook_flow`).
- **Decisión:** `ON DELETE CASCADE` en `client_id`, `ON DELETE SET NULL` en `user_id`.
- **Razón:** la alternativa era enseñarle `audit_logs` a la limpieza de cada test, y a cualquier código futuro que dé de baja un cliente. El rastro de auditoría de un tenant no tiene sentido sin el tenant, y una baja completa de cliente (RGPD a nivel de organización) tiene que poder ejecutarse. `user_id` es al revés: dar de baja a un empleado no debe borrar lo que hizo, solo dejar la fila sin autor.
- **Consecuencia:** el rastro no sobrevive al borrado del tenant. Si algún día hace falta conservarlo para cumplimiento después de la baja, la salida no es quitar el CASCADE sino exportarlo antes de borrar.

### ADR-045: Las tareas de mantenimiento son envoltorios sobre scripts de bash
- **Fecha:** 2026-09-17
- **Contexto:** el backup y la prueba de restauración (spec §7) son `pg_dump`, `pg_restore`, `psql` y el CLI de `aws` encadenados.
- **Decisión:** la lógica vive en `scripts/backup.sh` y `scripts/restore_test.sh`; `app/tasks/maintenance.py` solo los invoca con `subprocess.run` (lista fija, sin shell), acota el tiempo y traduce un código de salida distinto de cero en una excepción.
- **Razón:** reimplementar esas herramientas desde Python añadiría una capa propia que puede fallar por su cuenta, y los scripts se pueden ejecutar a mano para verificar la configuración sin levantar Celery. Lo que aporta Celery es el calendario y que el fallo quede visible.
- **Consecuencia:** un backup que falla marca la tarea como fallida en vez de "terminar" sin hacer nada, que es el modo en que un backup roto se descubre el día que hace falta restaurar. Los nombres son `app.tasks.bulk_*` y no los `app.tasks.maintenance.*` de la spec §7.3: no existe cola `maintenance` y el routing de Sprint 2 es por prefijo de nombre, así que con el nombre del spec habrían caído en la cola `webhooks`, bloqueando la recepción de mensajes durante la hora que dura un backup.

### ADR-046: El login busca al usuario con una función `SECURITY DEFINER`
- **Fecha:** 2026-09-17
- **Contexto:** BUG-025. Autenticar es la única operación del sistema que necesita leer `users` sin saber a qué tenant pertenece la fila: el tenant se deduce del usuario, y el usuario es justo lo que se busca. La RLS lo impide por diseño, así que hacía falta una excepción explícita. Las tres salidas estaban planteadas en BUG-025; el usuario eligió la (a).
- **Decisión:** `public.auth_lookup_user(p_email text)` (migración 007), `SECURITY DEFINER`, `STABLE`, con `SET search_path = pg_catalog, public, pg_temp` y los nombres calificados. Devuelve una sola fila —la del email exacto— con lo que el login necesita, incluido `client_is_active`, porque `clients` también tiene RLS y consultarla aparte chocaría con lo mismo. `app/api/v1/auth.py` la llama y nada más la llama.
- **Por qué esta y no las otras dos:** una política adicional sobre `users` para el caso "sin contexto" abre una vía de lectura cross-tenant sobre la tabla de usuarios que después hay que acotar a mano; un rol de autenticación con `BYPASSRLS` es lo que menos código toca y lo que más superficie privilegiada añade. La función deja la excepción en **una firma concreta, auditable y sin parámetros libres**: no lista, no acepta patrones, no toca otras tablas. El resto de la RLS se queda igual — nada de `NO FORCE`, ninguna política nueva.
- **Precondición que la migración comprueba:** `SECURITY DEFINER` hace que el cuerpo corra con los privilegios del **dueño** de la función, pero `FORCE ROW LEVEL SECURITY` (CLAUDE.md, regla 1) aplica las políticas también al dueño de la tabla. Así que la función solo esquiva la RLS si su dueño tiene `BYPASSRLS` o es superusuario — el rol de las migraciones, no el de la aplicación. Una precondición implícita que falla en silencio es exactamente lo que produjo BUG-025 (la función devolvería cero filas y el login diría "credenciales inválidas" para todo el mundo, sin un error en los logs), así que la migración **aborta el despliegue** si no se cumple.
- **Consecuencia:** `EXECUTE` se queda en el default de PostgreSQL (PUBLIC) porque el provisioning crea el rol de la aplicación *después* de correr las migraciones, tanto en CI como en Supabase, y un `GRANT` nominal ahí fallaría. Acotarlo al rol de la aplicación es una mejora del script que crea el rol, anotada en PROGRESS.md junto al `REVOKE` de `audit_logs`.

### ADR-047: El exporter OTLP solo se monta si hay endpoint; el tracing siempre
- **Fecha:** 2026-09-17
- **Contexto:** el spec §1.2 monta el `BatchSpanProcessor` con el exporter OTLP incondicionalmente. Sin un collector escuchando (cualquier arranque local, la suite de tests, un despliegue sin Jaeger), ese exporter reintenta en segundo plano y escribe un `ConnectionRefused` cada pocos segundos en el log de la aplicación.
- **Decisión:** `app/core/telemetry.py` instala el `TracerProvider` y las instrumentaciones siempre; el `BatchSpanProcessor` con OTLP solo si `OTEL_EXPORTER_OTLP_ENDPOINT` tiene valor. El default de la variable es cadena vacía.
- **Consecuencia:** un despliegue sin collector conserva lo que más se usa a diario —el `trace_id` en cada línea de log y en la cabecera `X-Trace-ID`— sin ruido de red. Encender el tracing distribuido es poner la variable, no tocar código. Los spans que no se exportan se descartan en memoria, sin coste apreciable.

### ADR-048: Las métricas se exponen en modo multiproceso, no por proceso
- **Fecha:** 2026-09-17
- **Contexto:** el spec §3.3 monta `make_asgi_app()` de `prometheus_client`, que sirve el registro del proceso que atiende el scrape. Pero la API corre con `uvicorn --workers 2` (Dockerfile) **y** `replicas: 2` (compose): cuatro procesos, cada uno con sus propios contadores en memoria. Los workers de Celery, lo mismo con el pool prefork. Un scrape devuelve el valor de uno cualquiera de ellos, elegido de hecho al azar, y la serie resultante sube y baja sin relación con el tráfico real: un contador acumulativo que retrocede convierte cualquier `rate()` en basura.
- **Decisión:** modo multiproceso de `prometheus_client` (`PROMETHEUS_MULTIPROC_DIR` + `MultiProcessCollector`), con un `tmpfs` por contenedor en `docker-compose.yml`. `build_registry()` devuelve el registro agregador cuando la variable está, y el registro normal cuando no (tests, arranque de un solo proceso).
- **Consecuencia:** no se pueden usar `Gauge` sin declarar su `multiprocess_mode`; en el catálogo no hay ninguno. El directorio tiene que estar vacío al arrancar el contenedor —de ahí el `tmpfs`— o el scrape suma las muestras de procesos muertos de la ejecución anterior.

### ADR-049: La profundidad de las colas la publica redis-exporter, no la aplicación
- **Fecha:** 2026-09-17
- **Contexto:** el spec §3.1 define `celery_queue_size` como métrica de la aplicación. Medirla desde la API obliga a un `LLEN` contra Redis dentro del endpoint de métricas (I/O síncrono en contexto async, contra la regla 4 de CLAUDE.md) y a un `Gauge` que cuatro procesos escribirían a la vez, sumando o pisándose según el `multiprocess_mode`.
- **Decisión:** un servicio `redis-exporter` con `REDIS_EXPORTER_CHECK_KEYS` sobre las 6 colas. Publica `redis_key_size{key="<cola>"}`, que es el largo real de la lista.
- **Consecuencia:** la profundidad la mide quien es dueño del dato (Redis), una sola vez y sin pasar por la aplicación. Los dashboards y la alerta `ColaCeleryProfunda` consultan `redis_key_size`, no `celery_queue_size` — esa métrica no existe. Las métricas de *tareas* (duración, estado) sí las publica la aplicación por señales de Celery, con las mismas etiquetas que el resto del catálogo y sin un `celery-exporter` más que mantener.

### ADR-050: Loguru por intercepción, sin reescribir los módulos existentes
- **Fecha:** 2026-09-17
- **Contexto:** CLAUDE.md (regla 5) pide Loguru con `client_id` y `trace_id` en cada línea, pero los ~40 módulos escritos hasta el Sprint 7 usan `logging` de la stdlib con `logger.info("...%s", x)`. Cambiarlos uno a uno es un diff enorme, sin comportamiento nuevo y con riesgo de romper mensajes en cada archivo tocado.
- **Decisión:** `setup_logging()` instala un `InterceptHandler` en la raíz de `logging` y no toca ninguna llamada existente. El contexto lo ponen `ObservabilityMiddleware` (API) y `bind_task_context()` (Celery) con `logger.contextualize()`.
- **Consecuencia:** todo lo que ya se loguea —incluido uvicorn, SQLAlchemy, httpx y Celery— sale con el mismo formato y el mismo contexto. El `filter={"sqlalchemy": "WARNING"}` del spec §2.1 no se usa: ese parámetro de Loguru agregaba un **segundo** sink que duplicaba cada línea de WARNING para arriba; silenciar librerías ruidosas se hace con `logging.getLogger(...).setLevel(...)`. Tampoco hay sink a `/var/log/app/app.log`: ningún contenedor monta ese volumen.

### ADR-051: Índice ciego (HMAC) junto a la columna cifrada
- **Fecha:** 2026-09-17
- **Contexto:** el spec §8.4 afirma que "el cifrado es determinístico para un mismo `ENCRYPTION_KEY`". Es falso: `pgp_sym_encrypt()` usa un IV aleatorio y cifra el mismo valor de forma distinta cada vez. Sobre esa premisa se caen dos cosas del sistema real: la búsqueda del webhook (`WHERE identifier_value = :telefono`, que dejaría de encontrar al contacto y crearía uno nuevo en cada mensaje) y el `UNIQUE (client_id, channel, identifier_value)`, que aceptaría como distintos dos ciphertexts del mismo teléfono.
- **Decisión:** `contact_identifiers` gana `identifier_hash`, el HMAC-SHA256 del valor normalizado con la misma clave (`app.core.encryption.blind_index`). El `UNIQUE` se mueve a esa columna y la búsqueda del webhook pasa por ella. El hash lo mantiene un listener `before_insert`/`before_update` del modelo, no cada llamador.
- **Por qué HMAC y no SHA-256:** un teléfono tiene poquísima entropía; una columna de hashes sin clave se invierte por fuerza bruta en minutos, y quedaría un identificador personal en claro al lado del cifrado.
- **Por qué un listener y no un parámetro:** la anonimización de RGPD reescribe `identifier_value`. Con el hash a cargo de quien escribe, ese camino habría dejado una fila anonimizada que se sigue encontrando por el teléfono que se suponía borrado — y sin error visible. Un `UPDATE` masivo con `sqlalchemy.update()` sí se salta el listener: está anotado en el modelo.
- **Consecuencia:** rotar `ENCRYPTION_KEY` obliga a recalcular la columna entera, no solo a re-cifrar. `contacts.first_name`, `last_name` y `display_name` se quedan en claro: la API del CRM los busca con `ILIKE` y no hay búsqueda parcial posible sobre datos cifrados; el spec los marca como "cifrado opcional por tenant" (§8.3), opcionalidad que necesita infraestructura por tenant que hoy no existe.

### ADR-052: El índice ciego es por tenant (`HMAC(clave, "{client_id}:{valor}")`)
- **Fecha:** 2026-09-19
- **Contexto:** ADR-051 calculaba `identifier_hash` con una clave global y sin tenant en el mensaje, así que el mismo teléfono daba el **mismo** hash en todos los tenants. La unicidad y las búsquedas de la aplicación ya llevaban `client_id`, así que no había fuga por la API; pero quien tuviera lectura cruda de la tabla (un rol con `BYPASSRLS`, un dump, un backup, el soporte del proveedor de base de datos) podía correlacionar por igualdad de hash que la misma persona escribe a dos clientes distintos de la plataforma — justo lo que el aislamiento por tenant pretende impedir entre clientes B2B que pueden ser competidores.
- **Decisión:** `blind_index(valor, client_id)` hashea `"{client_id}:{valor normalizado}"`. `client_id` es **obligatorio** (falla con `ValueError` si viene vacío): si fuera opcional, olvidarlo volvería en silencio al hash global. Migración `009_blind_index_per_tenant` recalcula todas las filas descifrando `identifier_value` (sin cambio de esquema ni de `UNIQUE`: dos filas con el mismo tenant/canal/valor siguen dando el mismo hash, así que no puede introducir duplicados).
- **Por qué el tenant en el mensaje y no una clave derivada por tenant:** el resultado es equivalente para el atacante, no exige gestionar sub-claves y la migración se expresa en una sola sentencia SQL (`encode(hmac(client_id::text || ':' || lower(trim(...)), :clave, 'sha256'), 'hex')`).
- **Riesgo que se controla con una prueba:** la expresión SQL de la migración y `blind_index()` tienen que dar lo mismo, o la aplicación deja de encontrar las filas migradas y cada mensaje entrante crea un contacto nuevo. `tests/integration/test_encryption.py::TestParidadConLaMigracion` evalúa la expresión de la migración contra Postgres real y la compara con la función de Python.
- **Consecuencia:** la migración toma bloqueos de fila sobre toda la tabla mientras dura; en una tabla grande, ventana de bajo tráfico. Necesita `ENCRYPTION_KEY` y la clave correcta (con otra, `pgp_sym_decrypt` falla y la transacción se revierte entera).

- **Runbook de despliegue de la migración 009** (la parte operativa que el código no puede hacer por sí solo):
  1. **Orden: parar, migrar, arrancar.** No se pueden solapar código nuevo y viejo con la migración. El código nuevo busca por `HMAC(clave, "{client_id}:{valor}")`; con los hashes viejos aún en la tabla no encuentra a nadie y **cada mensaje entrante crea un contacto duplicado**. El código viejo, si sigue corriendo después de migrar, escribe hashes con la fórmula global y deja filas inconsistentes. Por eso hace falta la ventana: detener API y workers (al menos `webhooks` y `ai_inference`), correr `alembic upgrade head`, arrancar la versión nueva. Los proveedores reintentan los webhooks que reciban un error mientras tanto, pero un servicio caído no es un 200: conviene avisar a los tenants o hacerlo en el valle de tráfico.
  2. **`ENCRYPTION_KEY`**: la **misma** que cifró la columna en la 008, en el entorno que ejecuta Alembic (`DATABASE_URL_DIRECT`, el rol dueño del schema con `BYPASSRLS`). Con otra, `pgp_sym_decrypt` falla y la transacción se revierte entera (no queda nada a medias). Si falta, la migración aborta antes de ejecutar nada (`tests/unit/test_migraciones_cifrado.py`).
  3. **Antes**: backup fresco y `scripts/restore_test.sh` sobre él. El UPDATE toma bloqueos de fila sobre toda la tabla hasta el commit.
  4. **Verificación después** (0 filas = correcto; sustituir `:clave` por la clave). Si devuelve algo, no arrancar la aplicación:
     ```sql
     SELECT count(*) FROM contact_identifiers
     WHERE identifier_hash <> encode(hmac(
         client_id::text || ':' || lower(trim(pgp_sym_decrypt(identifier_value, :clave))),
         :clave, 'sha256'), 'hex');
     ```
  5. **Reversa**: `alembic downgrade 008_encrypt_contact_identifiers` (también necesita la clave) y volver a desplegar el código anterior, en el mismo orden parar/migrar/arrancar.
  6. **Automatizado en `scripts/deploy_migration_009.sh`** (pasos 1, 2 y 4; el backup del paso 3 lo confirma un humano):
     - Sin argumentos **solo comprueba** (entorno, clave ≥ 32 caracteres, revisión actual = 008) y no toca nada: conviene correrlo *antes* de la ventana.
     - `--execute --backup-verificado` para API y workers, aplica `009`, corre `scripts/verify_migration_009.py` (0 filas desalineadas) y arranca. `--execute` se niega sin `--backup-verificado`.
     - **Si la migración o la verificación fallan, los servicios se quedan parados** (arrancar la aplicación sobre hashes inconsistentes duplica un contacto por mensaje) y el script imprime la reversa. Si la base ya está en 009, solo verifica.
     - Servicios y comando configurables (`SERVICIOS`, `COMPOSE`) si el despliegue no es el `docker-compose.yml` del repo. Los tests (`test_scripts_migracion_009.py`) usan `alembic`/`docker`/`python` falsos en el `PATH` y fijan ese orden y esas puertas; corren en Linux (CI).

### ADR-053: Telegram y email siguen el patrón real de canales (ABC de Sprint 4, credenciales por entorno), no el del spec
- **Fecha:** 2026-09-20
- **Contexto:** `specs/sprint-09-channels.md` describe una `MessagingProvider` con `parse_webhook(payload, headers)` y `send_message(recipient_id, content, **kwargs) -> dict`, una tabla `channel_configs` con `provider_config` y `webhook_secret` por tenant, una clase `ProviderFactory` y un endpoint `POST /webhooks/telegram/{channel_config_id}`. Nada de eso existe: la ABC real es `parse_webhook(raw_payload)` / `send_message(to, content, channel_config) -> str`, las credenciales son globales por entorno (ADR-030, `DEFAULT_CLIENT_ID`) y el endpoint es el genérico `POST /api/v1/webhooks/{provider}/{channel}`.
- **Decisión:** `TelegramProvider` y `EmailProvider` implementan la ABC real, se registran en `factory._PROVIDERS` y en `CHANNEL_PROVIDERS`, y sus credenciales salen de `Settings` (`TELEGRAM_CHANNEL_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `EMAIL_SMTP_*`, `EMAIL_FROM_ADDRESS`, `EMAIL_INBOUND_WEBHOOK_SECRET`). Sin constructor con config (como `YCloudProvider`): el token/SMTP viaja por llamada en `channel_config`.
- **Desviaciones de fondo, cada una por lo que rompería en producción:**
  - **Telegram sin `getFile` en `parse_webhook`:** el webhook debe responder en <100 ms; `media_url` = `telegram-file:<file_id>` y `TelegramProvider.get_file_url()` lo resuelve después (lo necesita la transcripción de audio, Dev B).
  - **Sin `parse_mode: HTML`:** el texto lo genera un LLM; un `<` o `&` sin escapar da 400 "can't parse entities" y el mensaje se pierde.
  - **Troceo propio a 4096:** nada aguas arriba consulta `get_channel_constraints()` (se comprobó con grep), así que lo hace el provider.
  - **`update_id` como id externo, no `message_id`:** `message_id` solo es único dentro de un chat y la clave de deduplicación es global por canal.
  - **Solo chats privados, y se descartan otros bots:** en un grupo, responder al `from.id` escribe un DM que Telegram rechaza; dos bots hablándose son un bucle.
  - **Sin lista blanca de IPs:** el mecanismo documentado es el `secret_token` de `setWebhook`; una lista depende de reenviar bien la IP tras Cloudflare/Traefik y falla en silencio.
  - **Email: TLS según el puerto.** El spec usa `use_tls=True` con el 587 por defecto; `use_tls` es TLS implícito (465), el 587 es STARTTLS y el handshake fallaría.
  - **Email: solo texto plano.** El spec llama a un `_render_html_template` que no define; un cuerpo de LLM insertado en HTML es un vector de inyección.
- **Consecuencia:** cuando exista `channel_configs` (Fase 2), `get_channel_config()` y `_resolve_client_id()` son los dos únicos puntos a cambiar, igual que para YCloud y Meta.

### ADR-054: Los descartes intencionales de un webhook son `IgnoredWebhookError` (200 `ignored`, sin traceback)
- **Fecha:** 2026-09-20
- **Contexto:** el endpoint trataba cualquier excepción de `parse_webhook()` como `parse_error`, con `logger.exception` (traceback completo). Con Telegram y email hay descartes legítimos y **frecuentes**: mensajes de grupos, stickers, autorrespuestas, listas de correo, rebotes. Registrarlos como errores llena el log de tracebacks y esconde los errores reales.
- **Decisión:** `IgnoredWebhookError(ValueError)` en `app/services/messaging/base.py`. El endpoint la atrapa antes del `except Exception`, responde 200 `{"status": "ignored"}` (el proveedor no reintenta) y registra un `INFO` sin traceback. Los payloads rotos siguen siendo `ValueError`/otra excepción → `parse_error` con traceback.
- **Consecuencia:** es un `ValueError` a propósito, para que nada que ya atrapara ese tipo cambie. Un provider nuevo distingue "no es para mí" (`IgnoredWebhookError`) de "está mal formado" (`ValueError`).

### ADR-055: Email — entrada autenticada con Basic auth en la URL, sin responder a automáticos, hilo por el último mensaje entrante
- **Fecha:** 2026-09-20
- **Contexto:** ni SendGrid ni Mailgun firman el Inbound Parse. Sin autenticación, cualquiera puede inyectar "emails de clientes" al agente. Además, un bot que contesta emails tiene dos riesgos propios: el bucle (contestar a otro autorespondedor) y abrir un hilo nuevo en el cliente de correo si faltan `In-Reply-To`/`References`.
- **Decisión:**
  - **Autenticación:** las credenciales van en la URL del webhook (`https://usuario:password@host/...`), los dos proveedores envían `Authorization: Basic`, y `validate_signature()` lo compara con `EMAIL_INBOUND_WEBHOOK_SECRET` (`usuario:password`) en tiempo constante. Secreto vacío = nunca valida.
  - **Anti-bucle:** `parse_webhook` descarta (`IgnoredWebhookError`) `Auto-Submitted` ≠ `no`, `Precedence: bulk|list|junk`, `List-Id`/`List-Unsubscribe`, `X-Autoreply`/`X-Autorespond`, `Return-Path: <>`, buzones de sistema (`mailer-daemon`, `noreply`...) y la propia `EMAIL_FROM_ADDRESS`. Los envíos propios llevan `Auto-Submitted: auto-replied` (RFC 3834).
  - **Hilo:** `deliver_message()` toma subject/`In-Reply-To`/`References` del último mensaje **entrante** de la conversación (`messages.metadata`), no del asunto como propone el spec, que uniría conversaciones ajenas con el mismo "Re: consulta". `MessageContent` gana `metadata` (solo lo usa email). El `Message-ID` propio queda como `external_message_id` para que el `In-Reply-To` del cliente lo resuelva.
  - **Inyección de cabeceras:** el asunto lo escribe un tercero; se colapsa a una línea antes de usarlo (y `EmailMessage` rechaza saltos de línea).
  - **`raw_payload` compacto** (`subject`, `message_id`, `in_reply_to`, `references`, `from`, `to`): acaba en `messages.metadata`, en la cola de Celery y en `audit_logs`; no el HTML ni el cuerpo.
- **Consecuencia:** el secreto viaja en la URL de configuración del proveedor (no en los logs de acceso: va en la cabecera, no en el path). Adjuntos ignorados (son `UploadFile`, no serializables a Celery; guardarlos exige Storage y límites).

### ADR-056: Los audios se transcriben en una tarea propia entre la persistencia y la IA, con la descarga tratada como entrada no confiable
- **Fecha:** 2026-09-20
- **Contexto:** el grafo solo lee `message["text"]`; un audio llegaba con `text=None` y el agente respondía sin haber escuchado nada. La URL del medio la entrega el proveedor (o quien falsifique un webhook): descargarla sin cuidado es un SSRF (`169.254.169.254`, red interna) y un vector de agotamiento de memoria/coste (audios enormes hacia Whisper, que se paga por minuto).
- **Decisión:**
  - **Tarea propia** `app.tasks.ai_transcribe_audio` (`app/tasks/audio_transcription.py`), enrutada a `ai_inference` por el prefijo `app.tasks.ai_` (**desde ADR-060 corre en la cola `media`** con el nombre `app.tasks.media_transcribe_audio`). `webhook_processor._process_message` la encola en vez de la IA cuando el mensaje es audio **sin texto** y con `media_url`; la tarea guarda el texto en `messages.content` (+ duración en `metadata.transcription`) y es ella quien encola `process_ai_response` con `message_data["text"]` puesto. No se transcribe en el webhook (<100 ms, regla 4) ni si un humano ya tiene la conversación (no se gasta Whisper, lo escuchará la persona).
  - **Descarga defensiva** (`app/services/transcription.py`): solo `https`; todas las IPs a las que resuelve el host deben ser públicas (`ipaddress.is_global`); redirecciones seguidas a mano (máx. 3) y **revalidadas salto a salto**; tope `WHISPER_MAX_AUDIO_BYTES` (20 MB) contra `Content-Length` **y** mientras se lee el cuerpo (un servidor chunked no lo esquiva). Los mensajes de error nunca incluyen la URL (la de Telegram lleva el token del bot).
  - **Telegram:** `media_url = telegram-file:<file_id>` se resuelve dentro de la tarea con `getFile` (`resolve_media_url`), no en el webhook (ADR-053).
  - **Formato:** Whisper decide por la extensión del nombre; se infiere del `Content-Type` y, si no, de la ruta (Telegram sirve `octet-stream` con `.oga`). `audio/amr` u otros → error permanente.
  - **Sin idioma forzado:** `WHISPER_LANGUAGE` vacío = autodetección (la plataforma atiende 6 idiomas).
  - **Errores en dos clases:** `TranscriptionError` (transitorio: red, 5xx, 429, límite de tasa → reintenta 2 veces, 10 s) y `PermanentTranscriptionError` (demasiado grande, formato, sin voz, 4xx, credencial inválida → no reintenta). Ambos caminos terminan, agotados, en `_emergency_handoff` con motivo `transcription_failed`, **salvo** que un humano ya tenga la conversación: no se le pisa a `waiting_human`/`human_active`.
  - **Idempotencia:** si un reintento encuentra `messages.content` ya escrito, no vuelve a llamar a Whisper.
  - **Coste:** `record_tokens(..., operation="transcription", cost_usd=minutos * WHISPER_COST_PER_MINUTE_USD)`. No descuenta del presupuesto de tokens del tenant (Whisper no factura por tokens); queda solo como métrica.
- **Consecuencia / límites conocidos:** la IP se valida al resolver y `httpx` resuelve de nuevo al conectar, así que un DNS-rebinding no queda cubierto (cerrarlo exige fijar la IP en el transporte). Solo se transcriben audios; el resto de medios (imagen, vídeo, documento) siguen sin pasar por ningún procesador.

### ADR-057: Unificación automática de contactos solo por teléfono verificado por el canal y coincidencia inequívoca
- **Fecha:** 2026-09-20
- **Contexto:** la misma persona escribe por varios canales y el agente la trata como desconocidos distintos (criterio 5 del spec). Pero unir dos contactos que no son la misma persona es **peor** que no unirlos: mezcla conversaciones, notas y etiquetas de gente distinta, y si cruza tenants es una fuga de datos. Requisito del usuario: no unir automáticamente salvo coincidencia inequívoca, sin mezclar información entre usuarios ni entre tenants.
- **Decisión:** `app/services/phone_unification.py`, invocado desde `webhook_processor._process_message` (en un **savepoint**: un fallo se registra y el mensaje sigue). Se une **solo si se cumplen todas**:
  1. **Teléfono verificado por el propio canal.** WhatsApp: el remitente. Telegram: solo el contacto que el usuario comparte *de sí mismo* (`contact.user_id == from.id`, `user_id` entero —`True == 1` no cuenta—, sin reenvío); el provider lo entrega en `NormalizedMessage.verified_phone`. Instagram, Facebook y email **nunca** unen (aunque un payload trajera el campo); un id de Telegram con forma de teléfono tampoco.
  2. **Mismo tenant.** Consultas con `client_id` explícito + RLS, y el índice ciego lleva el tenant en el HMAC (ADR-052): el mismo número en dos tenants da hashes distintos.
  3. **Exactamente un otro contacto** conoce el teléfono. Con dos o más (p. ej. un duplicado previo `+57…`/`57…`) → `AMBIGUO`, no se une, revisión manual.
  4. **Sin teléfonos contradictorios** en ninguno de los dos (otro número ⇒ no es seguro que sean la misma persona).
  5. **Sin canales solapados** (dos cuentas de Telegram, etc.).
  6. **Ninguno borrado por GDPR.**
  Lo demás sigue siendo fusión manual (`ContactUnifier`, endpoint de merge).
- **Registro del teléfono:** el verificado por un canal que no es WhatsApp se guarda como identificador de canal `verified_phone` (cifrado + índice ciego, como el resto; **no** es un canal de envío y `get_channel_config("verified_phone")` falla), para que un WhatsApp posterior encuentre al contacto de Telegram. Sin migración: `contact_identifiers.channel` es `String(50)` sin CHECK. Se evitó el nombre `phone` porque `ChannelEnum.phone` es un canal de mensajería (voz).
- **Cuándo se evalúa:** WhatsApp solo al **crear** el contacto (una consulta por mensaje no aporta: si el teléfono se registra después por otro canal, ese canal hace la unión); Telegram en cada mensaje con `verified_phone`.
- **Superviviente:** el contacto más antiguo (desempate por id, determinista). Deja `metadata.unifications` con el id absorbido y el motivo, sin datos personales.
- **Concurrencia:** `pg_advisory_xact_lock` por (tenant, hash del teléfono) al empezar: dos tareas de la misma persona por dos canales no pueden fusionarse en sentidos opuestos y dejar un ciclo de `merged_into_id`.
- **Consecuencia / límites:** un número de teléfono reasignado a otra persona sigue siendo "el mismo teléfono" (riesgo aceptado, inherente a identificar por número); los formatos no E.164 (sin código de país) no se unifican. El bot aún no ofrece un botón `request_contact` de Telegram para pedir el número: hoy la unión ocurre cuando el usuario lo comparte por su cuenta (siguiente paso natural). El texto del contacto compartido (con el número) sigue guardándose en `messages.content`/`metadata` sin cifrar (preexistente).

### ADR-058: El teléfono se pide en Telegram solo a petición del usuario (`/vincular`), con un flujo fijo que no pasa por la IA
- **Fecha:** 2026-09-20
- **Contexto:** ADR-057 unifica contactos por teléfono verificado, pero Telegram solo entrega el número si el propio usuario lo comparte con un botón `request_contact`. Faltaba el flujo que lo ofrece.
- **Decisión:**
  - **Trigger:** el usuario escribe `/vincular` (o `/link`, con o sin `@NombreDelBot`). El bot **no** lo pide por iniciativa propia en el primer mensaje: pedir un dato personal sin que nadie lo haya pedido es intrusivo y no tiene base de consentimiento. Un mensaje que solo *contiene* la palabra "vincular" sigue a la IA.
  - **Flujo fijo, fuera de la IA** (`app/services/contact_request.py`): un LLM no decide cuándo pedir un dato personal ni redacta el agradecimiento. `webhook_processor` responde con una tarea propia (`app.tasks.notification_send_channel_reply`, cola `notifications`, 3 reintentos, sin escalado a humano si se agotan: nadie espera una contestación) y no encola la IA.
  - **Teclado:** `MessageContent.metadata` acepta `request_contact` (etiqueta del botón → `ReplyKeyboardMarkup` con `one_time_keyboard`) y `remove_keyboard`. `deliver_message` gana un parámetro `metadata` que se suma al contexto de hilo de email (manda el llamador).
  - **Agradecimiento neutro:** al llegar el número (`verified_phone`) se agradece y se retira el teclado, **haya o no unificación**. Decir "te reconocí en WhatsApp" confirmaría que ese número ya pertenece a otro contacto. Sin `verified_phone` (tarjeta ajena o reenviada) el mensaje sigue a la IA como antes.
  - **El número no queda en claro:** con un contacto propio verificado, `messages.content` guarda "Compartio su numero de telefono" y `raw_payload` (→ `messages.metadata`) lleva el número enmascarado. El número completo solo vive cifrado como identificador `verified_phone` y, de paso, en `verified_phone` durante el tránsito por la cola de Celery.
  - No se responde si un humano tiene la conversación (`waiting_human`/`human_active`), igual que la IA.
- **Consecuencia:** los textos están en español (como `HANDOFF_MESSAGES`); la traducción por tenant llega con el i18n del backend. El comando no está registrado en el menú de BotFather (`setMyCommands`): es un paso manual de configuración del bot.

### ADR-059: Webchat por WebSocket — sesión firmada, entrega por Redis pub/sub y recuperación desde la base
- **Fecha:** 2026-09-20
- **Contexto:** el spec de Sprint 9 propone un `WebchatConnectionManager` en memoria, `validate_signature()` que devuelve `True` "porque el WebSocket ya está autenticado", un `session_id` que elige el cliente, tabla `channel_configs` y `WebchatProvider.__init__(config, manager)`. Nada de eso sirve: el proceso que genera la respuesta (worker de Celery) no es el que tiene el socket (API); un `session_id` elegido por el cliente permite leer la conversación de otro; y el `True` deja inyectar mensajes por HTTP. Además el visitante es anónimo y llega de internet.
- **Decisión:**
  - **Endpoint** `wss://host/api/v1/webchat/{channel_token}` (`app/api/v1/webchat.py`). `WEBCHAT_CHANNEL_TOKEN` identifica el canal y **no es un secreto** (va en el JS del sitio del cliente); vacío = webchat desactivado. Antes de aceptar se comprueba token (tiempo constante), `Origin` (obligatorio; `WEBCHAT_ALLOWED_ORIGINS`, o `CORS_ORIGINS` si está vacío) y que haya tenant (`DEFAULT_CLIENT_ID`, ADR-030); si falla, cierre 4401/4403/1011 sin abrir el socket.
  - **Sesión firmada** (`webchat_session.py`): `visitor_id` aleatorio de 128 bits (es el identificador de canal que se cifra en `contact_identifiers` y que ven los agentes) + token `visitor.caducidad.HMAC(client_id:visitor:caducidad)` con clave derivada de `JWT_SECRET` con etiqueta propia. Conocer un `visitor_id` no da acceso; un token de otro tenant, caducado, alterado o inventado da un visitante **nuevo** (nunca un error que permita sondear), y un `last_message_id` sin sesión válida no recupera nada. La sesión viaja en el **primer frame** (`hello`), no en la URL: una URL queda en los logs de cada proxy y la sesión es un secreto al portador.
  - **Entrada:** frames JSON validados con Pydantic (`extra="forbid"`, `message_id` con `fullmatch` de `[A-Za-z0-9_-]{1,64}`, controles eliminados), tope de frame (`WEBCHAT_MAX_FRAME_BYTES`, y `--ws-max-size 65536` en uvicorn para que el servidor no lea 16 MB antes de que lo compruebe el endpoint), de longitud, 20 mensajes/min por visitante (`SET NX EX` + `INCR`, para que la clave nazca siempre con TTL) y 5 conexiones por visitante y proceso. El visitante lo pone el servidor, nunca el frame. Se encola como cualquier webhook (`process_incoming_message`, `provider="webchat"`), con dedup `webchat:{visitor}:{message_id}` (el id externo lleva el visitante: dos visitantes con el mismo `message_id` no chocan) y liberación de la marca si la cola falla.
  - **`validate_signature()` devuelve siempre `False`:** el endpoint HTTP genérico `/webhooks/webchat/webchat` existe (la factory lo registra) y no puede aceptar nada.
  - **Salida:** `WebchatProvider.send_message()` publica en `webchat:out:{client_id}:{visitor_id}` (Redis); cada conexión está suscrita al de su visitante y reenvía. Publicar sin suscriptores no es un fallo: `deliver_message()` guarda igualmente el mensaje.
  - **Recuperación:** al reconectar se suscribe **antes** de leer la base (nada publicado en medio se pierde) y `webchat_history.mensajes_perdidos()` devuelve los salientes posteriores a `last_message_id` (o los últimos `WEBCHAT_REPLAY_LIMIT`), acotado a las conversaciones de webchat del contacto de ese visitante y al tenant. El `message_id` en vivo y el guardado (`external_message_id`) son el mismo: el cliente deduplica, y el servidor no reenvía en vivo lo ya recuperado.
  - **Sin credenciales por tenant:** `get_channel_config("webchat")` devuelve solo `{"client_id": DEFAULT_CLIENT_ID}` (forma parte del nombre del canal de Redis).
- **Protocolo:** cliente→servidor `hello`, `message`, `button_reply`, `ping`; servidor→cliente `connected`, `message` (con `buttons` opcionales), `ack`, `error`, `pong` (ver `app/schemas/webchat.py`).
- **Límites conocidos:** (1) solo texto: el media exigiría una subida de archivos propia y aceptar URLs del visitante para que luego las descargue Whisper/visión sería un SSRF; (2) una suscripción de Redis por conexión: con decenas de miles de visitantes simultáneos hay que pasar a un suscriptor compartido por proceso; (3) el tope de conexiones por visitante es por proceso (N procesos ⇒ N veces); (4) sin `typing`/acuses de lectura; (5) el widget (JS) no existe: este PR entrega el backend y el protocolo; (6) el limitador de mensajes es de ventana fija por minuto; (7) Traefik y Cloudflare admiten WebSocket, pero hay que revisar los timeouts de inactividad frente al `ping` del cliente (~25 s) y `WEBCHAT_IDLE_TIMEOUT_SECONDS` (120 s).

### ADR-060: La transcripción de audio corre en su propia cola `media`, y los workers del compose declaran las credenciales de los canales
- **Fecha:** 2026-09-21
- **Contexto:** la tarea de Whisper (ADR-056) corría en `ai_inference`, con concurrency 2, la misma cola que ejecuta el grafo del LLM. Una transcripción puede durar hasta 100 s (descarga + Whisper): dos notas de voz largas bloquean las respuestas del LLM de **todos** los tenants. La idea de aislarla en una cola propia es de Dev B (PR #28). Al montar el worker apareció además un fallo de fondo en `docker-compose.yml`: el compose lista las variables de cada servicio una por una y varios workers que **envían** mensajes no declaraban las credenciales de los canales.
- **Decisión:**
  - **Cola `media`** (`celery_config.py`, ruta `app.tasks.media_*`) y worker `celery-media` (concurrency `CELERY_MEDIA_CONCURRENCY`, default 2). La tarea pasa de `app.tasks.ai_transcribe_audio` a **`app.tasks.media_transcribe_audio`** con `queue="media"`. Además, `redis_key_size{key="media"}` se exporta y hay una alerta `ColaDeMediaEstancada` análoga a la de IA. El script de despliegue de la 009 también para y arranca `celery-media`.
  - **El worker de media declara las credenciales de todos los canales:** si no se puede transcribir, la tarea escala a un humano (`_emergency_handoff`), y eso **envía** un mensaje al contacto por su canal.
  - **Arreglo del compose:** `celery-ai` y `celery-notifications` no declaraban `DEFAULT_CLIENT_ID`, `YCLOUD_PHONE_NUMBER_ID`, `TELEGRAM_*`, `EMAIL_*` (el segundo tampoco), así que `deliver_message()` fallaba con «canal sin configurar» en un despliegue con este compose; y la API no declaraba `TELEGRAM_WEBHOOK_SECRET`, `EMAIL_INBOUND_WEBHOOK_SECRET`, `DEFAULT_CLIENT_ID` ni `WEBCHAT_*` (Telegram y email daban siempre 401 y el Webchat quedaba desactivado). Es un descuido de los PRs de Sprint 9 que no se vio en los tests, que sustituyen `Settings`.
  - **Auditoría del resto del compose (mismo fallo, código anterior a Sprint 9):** `celery-documents` no declaraba `SUPABASE_URL`/`SUPABASE_SECRET_KEY`/`SUPABASE_STORAGE_BUCKET` (`document_pipeline` descarga el archivo subido desde Storage: la ingesta de documentos fallaba); `celery-bulk` no declaraba `DEFAULT_CLIENT_ID` (`auto_close` resuelve el tenant con él); `celery-ai` no declaraba `GOOGLE_CALENDAR_*` (agente de agendamiento). El compose pasaba `OPENAI_DEFAULT_MODEL`, que `Settings` ignora (el campo es `OPENAI_CHAT_MODEL`): el modelo por defecto no se podía cambiar por entorno; se renombra también en `.env.example`. La API no exponía los seis límites `WEBCHAT_*` restantes ni `JWT_REFRESH_EXPIRATION_DAYS`.
  - **Defaults en el compose:** `${X}` sin definir llega como cadena vacía. Un `int`/`float`/lista de `Settings` con `""` revienta al arrancar (`JWT_EXPIRATION_MINUTES`, `CORS_ORIGINS`), y un texto con default no vacío (`WHISPER_MODEL`, `OPENAI_EMBEDDING_MODEL`, `LOG_LEVEL`, `REDIS_URL`…) lo pierde. **Todas** las variables de `Settings` que el compose interpola llevan `:-<default de Settings>`; el test lo exige para cualquier variable, no solo las nuevas.
  - **`tests/unit/test_compose_workers.py`** fija lo anterior: toda cola declarada tiene worker y se mide en Redis, la tarea de Whisper se enruta a `media` (contra el router real de Celery), los workers que envían y la API llevan las variables, y las numéricas/lista/texto con default llevan `:-`.
- **Despliegue:** la tarea cambia de nombre. Una tarea `ai_transcribe_audio` ya encolada y no consumida antes del despliegue se pierde con «unregistered task». Con Whisper aún sin configurar en producción no hay tareas en vuelo; si las hubiera, vaciar la cola `ai_inference` antes de desplegar.
- **Límite:** el test vigila lo declarado contra `Settings`; una variable que un worker lee por `os.getenv` fuera de `Settings` (los proveedores de enriquecimiento, `CELERY_*`, OpenTelemetry) queda en una lista de excepciones y no se comprueba que su código la use.

### ADR-062: Los adjuntos de email quedan como metadata, nunca como contenido
- **Fecha:** 2026-09-22
- **Contexto:** un pendiente de Sprint 9 (PROGRESS.md): un email con adjunto desaparecía en silencio total. `_leer_payload()` descartaba todo lo que no fuera texto plano (los adjuntos llegan como `UploadFile` del multipart), y un email que era *solo* un adjunto (sin asunto ni cuerpo) caía en `IgnoredWebhookError("Email sin asunto ni cuerpo")` — se perdía sin dejar ni un log útil.
- **Decisión — alcance deliberadamente acotado:** se registra **nombre, tipo y tamaño**, nunca el contenido. Guardar el archivo (Storage, un endpoint para recuperarlo, tipos permitidos, cuotas por tenant, quién puede leerlo) es una superficie de seguridad propia — un remitente no autenticado podría llenar un bucket, o colar un tipo peligroso — y queda **fuera de esta entrega**, como trabajo aparte.
  - `app/api/v1/webhooks.py::_leer_adjuntos()`: por cada `UploadFile` del formulario (solo si `provider == "email"`, el único que manda multipart), cuenta sus bytes **sin guardarlos** (`_tamano_adjunto_acotado()`, en trozos de 64 KB, cortando apenas se supera `EMAIL_MAX_ATTACHMENT_BYTES`) y sanea el nombre con `app.services.storage.sanitize_filename()` (el mismo que usa el pipeline de documentos: sin separadores de ruta). Hasta `EMAIL_MAX_ATTACHMENTS` (default 5); el resto ni se cuenta.
  - `EmailProvider.parse_webhook()` vuelve a validar `raw_payload["_attachments"]` (defensa en profundidad: no confía en que el único llamador sea siempre el endpoint) y agrega una línea legible al final del texto — `"[Adjunto(s): factura.pdf (240 KB)]"` — para que el agente (humano o LLM) sepa que hubo un adjunto, en vez de silencio. Va también en `raw_payload["attachments"]`.
  - **Un email que es *solo* un adjunto ya no se ignora.** El chequeo pasó de `not cuerpo and not asunto` a `... and not adjuntos`. El `message_id` sintético (cuando el proveedor no manda `Message-Id`) incluye los adjuntos en el hash, para que dos emails vacíos con adjuntos distintos no colisionen.
- **Bug real encontrado escribiendo los tests:** `isinstance(valor, UploadFile)` con `UploadFile` importado de `fastapi` **nunca es cierto** para lo que devuelve `request.form()` — ese es un método de Starlette y entrega instancias de `starlette.datastructures.UploadFile`; `fastapi.UploadFile` es una subclase distinta (`fastapi.datastructures.UploadFile(starlette.datastructures.UploadFile)`), y la comprobación de tipo va en el sentido que no funciona. El primer test (esperando que la metadata llegara) falló con la lista vacía y lo destapó. Corregido importando `UploadFile` de `starlette.datastructures`.
- **Consecuencia:** un adjunto que supera el límite reporta un tamaño aproximado (puede pasarse hasta un trozo de lectura, 64 KB) — la memoria sí queda acotada (nunca se retiene más de un trozo). No hay forma de que el agente ni un humano recuperen el archivo: solo saben que existió.

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

### Revisión de bugs 2026-09-17 (pedida por el usuario, todo el proyecto)

Cuatro agentes en paralelo auditaron el código completo (no solo Sprint 7) por cuatro ángulos: aislamiento multi-tenant/RLS, correctitud async/concurrencia, seguridad/manejo de errores, y lógica del motor de agentes. Los cuatro de `app/agents/tools/calendar_tools.py`/`app/agents/nodes/scheduling.py`/`app/agents/middleware/logging_middleware.py`/`app/services/contact_unifier.py` (código de esa misma sesión, Sprint 7 Dev A) se corrigieron antes de mergear el PR. Los de código preexistente (Sprint 3-6) quedan documentados abajo, abiertos.

### BUG-017: Traceback completo expuesto vía API en `agent_action_logs.details`
- **Descripción:** `logging_middleware.py::logged_node()` guardaba `details={"traceback": traceback.format_exc()[:2000]}` cuando un nodo fallaba. `AgentActionLogResponse.details: dict[str, Any]` (schema de Dev B) devuelve ese JSONB tal cual en `GET /api/v1/agent-logs/conversations/{id}` y `GET /api/v1/agent-logs/errors` — accesibles por cualquier `admin`/`supervisor` del tenant. Viola CLAUDE.md regla 3/5 ("nunca exponer tracebacks al cliente").
- **Estado:** CERRADO. El traceback completo va solo a `logger.exception()` (servidor); `details` guarda únicamente `{"exception_type": type(exc).__name__}`.

### BUG-018: `contact_id` sin fuente legítima para el LLM en las tools de agendamiento
- **Descripción:** `create_appointment`/`list_appointments` pedían `contact_id` como argumento normal del schema que ve GPT-4o, pero ningún dato del prompt ni de la conversación le da al modelo ese UUID interno. En el mejor caso el LLM lo inventaba y `UUID(contact_id)` explotaba con `ValueError` sin control (fuera del `except` de `scheduling_node`, escalaba 2 reintentos antes de llegar a humano); en el peor caso el diseño permitía en teoría operar sobre el contacto equivocado del mismo tenant.
- **Estado:** CERRADO. `contact_id` se inyecta por `config["configurable"]["contact_id"]` (el contacto real de la conversación, ya en `ConversationState`), mismo patrón de seguridad que `client_id` (`_contact_id()` en `calendar_tools.py`, `tool_config` en `scheduling_node`).

### BUG-019: Datetimes naive del LLM guardados en columnas `TIMESTAMPTZ`
- **Descripción:** `create_appointment`/`modify_appointment` parseaban `datetime_iso` con `datetime.fromisoformat()` (naive, hora local del tenant que el LLM quiso decir) y lo guardaban directo en `Appointment.starts_at`/`ends_at` (`TIMESTAMPTZ`). asyncpg interpreta un datetime naive como UTC, no como la hora local — corrimiento silencioso de varias horas entre lo que muestra Google Calendar y lo que queda en la fila. `scheduling_node`'s `current_datetime` del system prompt tenía el mismo problema (hora naive del servidor pese a decirle al LLM una zona horaria distinta).
- **Estado:** CERRADO. `_localizar()` en `calendar_tools.py` adjunta el timezone del tenant (`GoogleCalendarService.timezone`, ya resuelto) a un datetime naive antes de persistirlo o mandarlo a Google; `scheduling_node` arma `current_datetime` con `datetime.now(ZoneInfo(timezone))`.

### BUG-020: Defensa en profundidad RLS faltante en `calendar_tools.py` y `ContactUnifier.merge()`
- **Descripción:** Varias queries de `calendar_tools.py` (service_type, contact, appointment) y las cuatro operaciones de `ContactUnifier.merge()` (identifiers, conversations, notes, tags) filtraban solo por PK/contact_id, dependiendo enteramente de RLS — a diferencia del resto del proyecto (`get_contact_or_404` en `contacts.py`). `appointment_id`/`contact_id` en las tools los rellena el LLM a partir del texto del contacto; `source_id`/`target_id` en el merge salen de la URL de un endpoint HTTP — ninguno es un valor inyectado server-side como sí lo es `client_id`.
- **Estado:** CERRADO. Todas las queries de `calendar_tools.py` agregan `client_id` explícito; `ContactUnifier.merge()` busca el contacto origen primero (no al final) para tener su `client_id` y filtrar con él el resto de las operaciones.

### BUG-021: Cliente Redis async cruza límites de event loop en tareas de Celery
- **Descripción:** `app/services/dedup.py::get_redis()` cachea `_redis_client` a nivel de módulo; `close_redis()` existía pero solo se llamaba desde tests. `run_isolated()` (`app/core/database.py`) solo hacía `engine.dispose()` en su `finally`, nunca tocaba Redis. Cada tarea de Celery corre en su propio `asyncio.run()` vía `run_isolated()`; un worker que procesa el mensaje 1 crea el cliente Redis atado al loop A, ese loop se cierra, y el mensaje 2 (mismo proceso worker, loop B nuevo) reutilizaba el mismo cliente con conexiones atadas al loop A ya muerto — el mismo patrón de BUG-006/BUG-011 pero nunca corregido para Redis.
- **Impacto:** los `except Exception` (fail-open) de `dedup.py`/`token_budget.py` lo atrapan silenciosamente (sin crash visible), pero: la cache de presupuesto de tokens quedaba rota desde el segundo mensaje de cada proceso worker (caía siempre a Postgres), y en `webhook_processor.py::_send_to_dlq()` esto podía convertir "no se pudo escribir en la DLQ" en mensajes que agotaron reintentos y se pierden de verdad en vez de llegar a la Dead Letter Queue.
- **Estado:** CERRADO. `run_isolated()` llama a `close_redis()` (import perezoso, para no crear un ciclo con `dedup.py` importando `tenant_session` de `database.py`) en su `finally`, junto a `engine.dispose()`. Si cerrar Redis falla, se registra y se descarta — no debe tumbar una tarea que ya terminó bien. 3 tests nuevos en `test_database.py`.

### BUG-022: Race condition (lost update) en `TokenBudgetGuard.record_usage`
- **Descripción:** `app/middleware/token_budget.py::record_usage()` hacía `SELECT` de `TokenBudget` y luego `budget.used_tokens = int(budget.used_tokens) + total_tokens` en Python, sin `SELECT ... FOR UPDATE` ni un `UPDATE ... SET used_tokens = used_tokens + :n` atómico. Se llama 1-3 veces por mensaje y la cola `ai_inference` corre con concurrencia 2.
- **Impacto:** dos conversaciones del mismo tenant procesadas en paralelo podían pisarse el contador (el commit que llega último gana), subcontando el consumo real de forma permanente — un tenant podía pasar el umbral `exceeded` sin que `token_budget_check_node` lo detectara.
- **Estado:** CERRADO. Reemplazado por un `UPDATE ... SET used_tokens = used_tokens + :n` atómico (el incremento lo hace Postgres bajo el lock de la fila, no Python), que de paso agrega el `client_id` explícito que le faltaba al WHERE. De paso se agregó el mismo filtro en la consulta hermana de lectura (`app/agents/nodes/token_budget.py::_get_token_usage()`). `test_registra_el_log_y_suma_al_presupuesto` reescrito para inspeccionar los parámetros compilados del UPDATE en vez de mutar un objeto ORM.

### BUG-023: Doble reserva de citas sin lock ni constraint
- **Descripción:** `check_availability` y `create_appointment` (`calendar_tools.py`) eran dos tool-calls separadas sin ninguna sincronización entre "consultar libre" y "crear el evento + insertar la fila". `Appointment` no tiene ningún `UniqueConstraint`/exclusion constraint que impida dos citas superpuestas.
- **Impacto:** dos conversaciones concurrentes del mismo tenant pidiendo el mismo horario casi al mismo tiempo podían generar una doble reserva silenciosa (dos eventos en Google Calendar, dos filas en `appointments`), sin ningún error que lo delate. También sin concurrencia real: podían pasar turnos enteros de conversación entre `check_availability` y `create_appointment`, tiempo de sobra para que el horario se ocupara.
- **Estado:** CERRADO. Dos piezas: `GoogleCalendarService.has_conflict(start, end)` (recheck puntual del rango exacto, no todos los huecos del día como `check_availability`) y `create_appointment` haciendo el recheck + el evento de Calendar + el INSERT dentro de una sola transacción, tomando `pg_advisory_xact_lock(hashtext(...))` por tenant al entrar. Transaccional (`_xact_`), no de sesión: es el único modo de advisory lock compatible con el Transaction Pooler de Supavisor — se libera solo al COMMIT/ROLLBACK, no depende de que el siguiente statement caiga en la misma conexión física (algo que Supavisor no garantiza). 6 tests nuevos.

### BUG-024: Ejecución parcial de tool calls sin dejar constancia en el handoff
- **Descripción:** `scheduling_node` ejecuta todas las `tool_calls` de una misma respuesta del LLM dentro de un único `try`; cada tool hace su propio commit independiente. Si una tool anterior tiene éxito (ej. crea una cita) y una posterior en el mismo turno lanza una de las tres excepciones que disparan handoff, el nodo descartaba `tool_results` y escalaba sin dejar constancia (ni al usuario ni en `conversations.metadata.handoff`) de que la primera acción ya se ejecutó.
- **Estado:** CERRADO. `ConversationState` suma `partial_results: list[str] | None`; `scheduling_node` lo llena con lo que sí se alcanzó a ejecutar antes del error, y `human_handoff_node::_handoff_metadata()` lo copia a `metadata.handoff` cuando está presente — así el agente humano que retoma la conversación sabe que ya pasó algo, no solo "hubo un problema". 3 tests nuevos.

### Nota: defensa en profundidad RLS pendiente en código preexistente (baja prioridad)
El agente de RLS de la revisión 2026-09-17 encontró el mismo patrón de BUG-020 (query sin `client_id` explícito, solo RLS) en código de Sprint 3-6 no tocado hoy: `app/agents/nodes/human_handoff.py:118` y `app/agents/nodes/_delivery.py:77` (`session.get(Conversation, conversation_id)`), `app/agents/nodes/_tenant.py::get_contact_identifier()` y `app/services/document_pipeline.py` (`session.get(Document, document_id)`). (El caso gemelo en `app/middleware/token_budget.py`/`app/agents/nodes/token_budget.py` sí se cerró junto con BUG-022, por tocar el mismo archivo/patrón.) En los casos que quedan, el id en cuestión sale del estado interno del grafo o de la sesión de DB, no de un input directo del LLM/usuario, así que el riesgo real es bajo (contrasta con BUG-020, donde el id sí venía de fuera). No se tocó en esta sesión — no verificado independientemente, queda para una pasada de limpieza aparte si el usuario la prioriza.

### BUG-025: El login no funcionaba contra un rol sujeto a RLS
- **Descripción:** `app/api/v1/auth.py::login` busca al usuario por email con `AsyncSessionLocal()`, **sin** contexto de tenant — y no puede tenerlo: el tenant se deduce del usuario, y el usuario todavía no se conoce. La política de RLS de `users` (migración 002) es `USING (client_id = current_setting('app.current_client_id')::uuid)`, así que esa consulta evalúa un parámetro que en esa transacción no vale nada y la consulta revienta. Contra un rol con `NOBYPASSRLS` —el de CI (`app_user`) y el que debe usar la aplicación en producción— **ningún login funciona**.
- **Error concreto:** `invalid input syntax for type uuid: ""`, no `unrecognized configuration parameter` que sería lo esperable. El motivo es sutil y vale la pena recordarlo: en cuanto **otra** transacción del mismo backend ejecuta `set_config('app.current_client_id', ..., true)`, el parámetro queda definido para la conexión; al revertirse al final de esa transacción vuelve a **cadena vacía**, no a inexistente. Con un pooler por delante reutilizando conexiones, ese es el estado normal.
- **Por qué no lo detectó ningún test:** `tests/unit/test_auth.py` sustituye la sesión entera por un mock, así que nunca ejerció una política de RLS. Ningún test de integración tocaba el login: la suite de integración autentica generando el JWT directamente con `create_access_token()`, sin pasar por el endpoint.
- **Cómo salió:** al decidir si `users` podía llevar trigger de auditoría (Sprint 8, spec §9.2). Se escribió `tests/integration/test_audit_gdpr.py::TestLoginBajoRls` para comprobarlo contra Postgres real en vez de razonarlo sobre el papel, y falló.
- **Impacto:** crítico en producción con un rol sujeto a RLS. No llegó a manifestarse porque ningún entorno desplegado había ejercitado el login contra ese rol.
- **Estado:** CERRADO 2026-09-17, con la salida (a) de las tres que se plantearon — decisión del usuario. `public.auth_lookup_user()` (migración 007) es ahora el único punto del sistema que lee `users`/`clients` sin contexto de tenant, y solo sabe resolver un email exacto. Ver ADR-046 para el razonamiento completo y la precondición de `BYPASSRLS` que la migración comprueba.
- **Las otras dos salidas, y por qué se descartaron:** (b) una política adicional en `users` para el caso sin contexto — más simple, pero abre lectura cross-tenant sobre la tabla de usuarios y hay que acotarla a mano; (c) un rol de autenticación con `BYPASSRLS` — lo que menos código toca y lo que más superficie privilegiada añade.
- **Las tres mitades del bug:** no era solo el `SELECT` de `users`. También estaban bloqueados el `SELECT` de `clients` (tiene su propia RLS) y el `UPDATE` de `last_login_at`. El primero se resuelve devolviendo `client_is_active` en la misma fila; el segundo, ejecutándolo dentro de `tenant_session()` una vez que el tenant ya se conoce.
- **Cobertura:** `tests/integration/test_audit_gdpr.py::TestLoginBajoRls` (login completo bajo `app_user`, token utilizable, tenant suspendido, `last_login_at` escrito) y `::TestFuncionDeBusqueda` (alcance de la función, que sea `SECURITY DEFINER` con `search_path` fijo, y que el resto de la RLS de `users` siga intacta).
- **Ahora desbloqueado:** la migración 006 dejó `users` sin trigger de auditoría por este bug. Con el login arreglado, agregarlo es viable — pero exige antes auditar que **todos** los caminos de escritura sobre `users` pasen por `tenant_session()`, no solo el del login. Queda como tarea aparte, no se coló en el arreglo.

### BUG-026: Ningún worker de Celery podía arrancar con el docker-compose del repo
- **Descripción:** `Settings` (app/core/config.py) declara `DATABASE_URL`, `JWT_SECRET` y `ENCRYPTION_KEY` sin valor por defecto: si falta cualquiera de las tres, `get_settings()` lanza `ValidationError`. En `docker-compose.yml`, **ninguno** de los 7 servicios de Celery pasaba `JWT_SECRET`, y `celery-webhooks`, `celery-notifications` y `celery-beat` tampoco `ENCRYPTION_KEY`. El `.env` no entra en la imagen (está en `.dockerignore`), así que el proceso no tenía de dónde leerlas.
- **Impacto:** cualquier tarea que importara `app.core.config` —es decir, todas— reventaba al cargar. Con `task_acks_late=True`, el mensaje volvía a la cola y se reintentaba en bucle.
- **Por qué no lo detectó nada:** los tests inyectan las variables desde `tests/conftest.py`, y el smoke test de CI levanta el contenedor de la **API**, que sí las declara. Nadie arrancó un worker con este compose en CI.
- **Origen:** Sprint 2 (la definición de los servicios de Celery). Salió a la luz en Sprint 8, al necesitar `ENCRYPTION_KEY` dentro del worker de webhooks para el índice ciego.
- **Estado:** CERRADO 2026-09-17. Los 8 servicios de aplicación (API + 6 workers + beat) declaran ahora las tres variables, más las de observabilidad.
- **Pendiente relacionado:** no hay ningún test que verifique que un servicio de compose declara lo que su proceso necesita. Un chequeo que cruce las variables requeridas por `Settings` contra el `environment` de cada servicio cerraría esta clase entera de fallo; queda anotado, no implementado.

### BUG-003 (cerrado): el dashboard de token budget apuntaba a un datasource inexistente
- **Descripción:** `grafana/dashboards/token-budget-monitoring.json` (Sprint 2) consultaba SQL contra un datasource `supabase-db` que el provisioning nunca declaró — y que no se puede declarar sin meter las credenciales de la base de datos en un fichero del repositorio. Los 11 paneles salían en error.
- **Estado:** CERRADO 2026-09-17. El dashboard se retira y lo reemplaza `grafana/dashboards/tenant_usage.json`, que cubre lo mismo (tokens y costo por tenant, top 10, reparto por canal y por operación) leyendo de Prometheus, donde las métricas ya existen desde este sprint. El JSON viejo sigue en el historial de git si algún día se quiere un datasource SQL de solo lectura contra Supabase.

### Hallazgo: el teléfono quedaba en claro en `contacts.display_name` — CERRADO
- **Descripción:** `app/tasks/webhook_processor.py::_resolve_contact()` creaba el contacto con `display_name=identifier_value`, o sea, el número de teléfono. Desde Sprint 8, `contact_identifiers.identifier_value` está cifrado — pero el mismo dato seguía en claro, y además indexado para búsqueda `ILIKE`, en la columna de al lado, hasta que un agente le ponía un nombre real al contacto.
- **Decisión del usuario:** opción (a) de las tres planteadas — enmascarar al crear, dejando el valor completo solo en el identificador cifrado.
- **Solución:** `app/core/encryption.py::mask_identifier()` conserva los últimos 4 caracteres y reemplaza el resto por `*` (mismo largo que el original, para no delatar la longitud real). `_resolve_contact()` llama a `mask_identifier(identifier_value)` en vez de usar el valor completo. Un identificador de 4 caracteres o menos se enmascara entero.
- **Las otras dos, y por qué no:** (b) cifrar también `display_name` — pierde la búsqueda por `ILIKE` que usa el CRM (`app/api/v1/contacts.py`); (c) dejarlo como está — el teléfono completo queda visible y buscable dentro del tenant.
- **Alcance:** solo aplica al `display_name` que se genera **al crear** el contacto. No toca contactos que ya tienen nombre real (un agente ya los identificó) ni el identificador cifrado, que sigue guardando el valor completo para el matching real.

### Revisión de bugs 2026-09-19 (pedida por el usuario, foco en el código de Sprint 8)

Cuatro agentes en paralelo (RLS/multi-tenancy, async/concurrencia, seguridad, lógica de negocio) sobre lo recién mergeado. 19 hallazgos reales, varios confirmados por más de un agente; se corrigieron los 3 de alto impacto y los de impacto medio. Rama `fix/sprint-08-hallazgos-auditoria`.

### BUG-027: `echo=True` del engine filtraba `ENCRYPTION_KEY` y datos cifrados por stdout
- **Descripción:** `app/core/database.py` creaba el engine con `echo=(APP_ENV == "development")`, y `.env.example` deja `APP_ENV=development` por defecto. Con `echo=True` SQLAlchemy usa `InstanceLogger`, que llama `logger._log()` directo (sin `isEnabledFor()`) y, si el logger no tiene handlers, instala su propio `StreamHandler(stdout)`. Por eso el `setLevel(WARNING)` de `app/core/logging.py` no lo silenciaba y salía fuera del pipeline de Loguru. Con columnas pgcrypto, los bind params incluyen el valor en claro que se cifra y la propia `ENCRYPTION_KEY`.
- **Estado:** CERRADO. `echo=False` siempre; la visibilidad de queries la da `SQLAlchemyInstrumentor` (OpenTelemetry), que no expone bind params.
- **Lección transferible:** silenciar un logger con `setLevel` no funciona si la librería tiene un camino que se salta `isEnabledFor()`; y un flag "solo en desarrollo" que imprime parámetros de queries deja de ser inocuo el día que una columna se cifra.

### BUG-028: El borrado RGPD no borraba nada: `audit_logs` conservaba el dato personal
- **Descripción:** el trigger de la migración 006 audita con `to_jsonb(OLD)`/`to_jsonb(NEW)` completos. El propio UPDATE de anonimización de `gdpr_delete_contact()` quedaba auditado con `old_values` = nombre y contenido reales, y el INSERT original también los tenía. El endpoint respondía "anonimizado" sin serlo. Encontrado por **dos agentes de forma independiente**.
- **Estado:** CERRADO. `_redactar_rastro_de_auditoria()` (`app/api/v1/admin.py`) sobreescribe, al final de la misma transacción y tras el flush, las claves sensibles (`first_name`/`last_name`/`display_name`/`metadata` de `contacts`; `content`/`media_url`/`metadata` de `messages`) en **todas** las filas de `audit_logs` de ese contacto y esos mensajes, sin importar cuándo se escribieron. Se conserva el resto (quién, cuándo, qué tabla): RGPD exige borrar el dato personal, no la prueba de que hubo una operación. Los salientes no se anonimizan y su auditoría tampoco se redacta. Cubierto con 3 tests de integración nuevos (a verificar en CI real).
- **Lección transferible:** un rastro de auditoría que copia filas completas es una segunda copia del dato personal; toda operación de supresión tiene que alcanzarlo o no suprime.

### BUG-029: Un `sender_identifier` vacío mezclaba conversaciones de clientes finales distintos
- **Descripción:** `ycloud.py`/`meta.py` extraen el remitente con `.get(..., "")` y `NormalizedMessage.sender_identifier: str` no validaba nada. `blind_index("")` es un HMAC estable, así que un payload mal formado creaba un contacto "fantasma" y cualquier otro remitente real que también llegara sin identificador (mismo tenant y canal) caía en el mismo contacto.
- **Estado:** CERRADO. Validador en el schema que rechaza vacío o solo-espacios (y recorta bordes). El endpoint ya trata cualquier excepción de `parse_webhook()` como `parse_error` (200, sin reintento del proveedor), así que el mensaje se descarta en vez de crear una fila peligrosa.

### BUG-030: `session.get()` sin `client_id` explícito (9 sitios) y `ContactUnifier.merge()` con defensa circular
- **Descripción:** el proyecto exige el `client_id` en el WHERE además de RLS. Nueve sitios usaban `session.get()` (`webhook_processor` ×2, `document_pipeline` ×2, `document_ingestion`, `human_handoff`, `_delivery`, `calendar_tools`, `contact_unifier`). El de `merge()` era además circular: tomaba `client_id = source.client_id` de la misma fila que buscaba sin filtro, y el **destino no se verificaba** (`merged_into_id` podía apuntar a un contacto de otro tenant si un llamador se saltaba la validación del endpoint). Ninguno era explotable hoy.
- **Estado:** CERRADO. `select()` filtrado por `(id, client_id)` en todos; `merge(source_id, target_id, client_id)` recibe el tenant del llamador autenticado y verifica origen y destino antes de tocar nada. Los `FakeSession` de `test_webhook_processor.py` ya no tienen `get()`: si alguien vuelve a `session.get()`, fallan.

### BUG-031: Tres entradas de `beat_schedule` apuntaban a tareas inexistentes
- **Descripción:** `check-token-budgets`, `recalculate-lead-scores` y `check-stale-leads` (Sprint 2) son de features que no existen. Beat las publicaba en cada ciclo y los workers las rechazaban como "unregistered task", sin ningún error visible.
- **Estado:** CERRADO. Se retiran (se vuelven a agregar con la tarea). `tests/unit/test_celery_config.py` verifica que toda entrada apunte a una tarea registrada y a una cola declarada; se comprobó que falla contra la configuración anterior.

### BUG-032: Observabilidad — DLQ contada como éxito, scrape bloqueante y sinks de Loguru síncronos
- **Descripción:** (a) `process_incoming_message` devuelve `{"status": "dlq"}` en vez de relanzar, y `celery_tasks_total` lo contaba como `SUCCESS`; (b) `/internal/metrics` leía los `.db` del modo multiproceso en el hilo del event loop; (c) los sinks de Loguru escribían a stdout en el hilo llamante (en la API, el único del event loop), y `enqueue=True` obliga a reconfigurar tras el fork en Celery porque el hilo del sink no sobrevive un `fork()` y `_configurado` sí se hereda.
- **Estado:** CERRADO. (a) estado `DLQ` a partir del `retval`, y la alerta `TareasFallando` y el panel de fallos pasan a `state=~"FAILURE|DLQ"`; (b) `asyncio.to_thread`; (c) `enqueue=True` + `setup_logging(force=True)` en `worker_process_init`.
- **Parte del hallazgo que NO aplicaba:** `mark_process_dead()` solo borra archivos de `Gauge`, y el catálogo no tiene ninguno (ADR-049); los archivos de contadores/histogramas de procesos muertos se conservan a propósito (son acumulativos) y `/tmp/prometheus` es tmpfs. No se agregó.

### BUG-033: Jaeger, Prometheus y Grafana publicados en `0.0.0.0`
- **Descripción:** `"16686:16686"` y `"9090:9090"` publican en todas las interfaces del host aunque el comentario dijera "solo localhost". Jaeger y Prometheus no autentican y las series llevan `client_id`.
- **Estado:** CERRADO. `127.0.0.1:puerto:puerto` en los tres, con test (`tests/unit/test_docker_compose.py`).
- **Ampliado después:** `redis` (6379) y el dashboard de Traefik (8080, `api.insecure: true`) también se publicaban en todas las interfaces desde Sprint 2; cerrados. El test ya no nombra servicios: cualquier puerto fuera de 80/443 de Traefik tiene que ir a `127.0.0.1`.

### BUG-034: Dos contactos con los mismos últimos 4 dígitos se veían iguales en la bandeja
- **Descripción:** `mask_identifier()` deja solo los últimos 4 caracteres; dos teléfonos de la misma longitud que terminen igual daban el mismo `display_name` provisional.
- **Estado:** CERRADO. `_resolve_contact()` agrega un sufijo corto del id del contacto (`********4567 #a1b2`); el id se genera en cliente (`uuid4`) para no necesitar un UPDATE extra (que además quedaría auditado).

### BUG-035: El login permitía enumerar emails por tiempo y corría bcrypt en el event loop
- **Descripción:** con un email inexistente `POST /auth/login` respondía 401 sin llamar a bcrypt (~100 ms); con uno existente sí. La diferencia de latencia permitía enumerar los emails registrados. Además `verify_password` (bcrypt, CPU) corría síncrono dentro del endpoint async: cada login congelaba el event loop ~100 ms (CLAUDE.md, regla 4). Lo segundo no lo había señalado ningún agente; salió al arreglar lo primero.
- **Estado:** CERRADO. Se verifica siempre contra un hash bcrypt (el del usuario, o uno de relleno del mismo coste si no existe) y por `asyncio.to_thread`. Tests: el email inexistente también pasa por bcrypt, y bcrypt corre fuera del hilo del event loop.

### BUG-036: El asunto de la conversación no se anonimizaba en RGPD
- **Descripción:** `Conversation.subject` es texto libre que escribe un agente; el export RGPD ya lo entregaba como dato del contacto, pero `gdpr_delete_contact()` no lo tocaba, ni su fila en `audit_logs` (el trigger de `conversations` guarda la fila completa).
- **Estado:** CERRADO. Se reemplaza por `[ELIMINADO]` donde no era NULL y se redacta su rastro de auditoría en la misma transacción. Tests unitarios y de integración.

### BUG-037: `{{date}}` de las respuestas rápidas salía siempre en UTC
- **Descripción:** un tenant en Bogotá (UTC-5) veía "mañana" de 19:00 a medianoche y el agente mandaba al cliente final una fecha equivocada.
- **Estado:** CERRADO. Usa `agent_configs.config.scheduling.timezone` (la misma que el agendamiento), con `America/Bogota` por defecto; una zona inválida no tumba el render. Tests con reloj congelado.

### BUG-038: Los workers de Celery solo instrumentaban Celery
- **Descripción:** `worker_process_init` llamaba solo a `setup_celery_telemetry()`. Queries, Redis y llamadas salientes (YCloud, OpenAI) ocurren casi todas en el worker y no generaban spans: la traza de un mensaje se cortaba en cuanto la tarea empezaba a trabajar, contra lo que decía la descripción del PR #22.
- **Estado:** CERRADO. El hijo prefork llama `setup_telemetry(engine=engine)` (SQLAlchemy, Redis, httpx) antes que la de Celery, post-fork.

### Hallazgos menores de esa revisión que NO se corrigieron, y por qué
- **`users.email` sin cifrar** (CLAUDE.md, regla 3 pide cifrar emails): se corrigió el docstring, que decía "cifrado", pero cifrarlo es una **decisión pendiente**, no un arreglo: `auth_lookup_user()` lo compara por igualdad exacta, así que exige un índice ciego por tenant como el de ADR-052, cambiar esa función `SECURITY DEFINER` y una migración de datos sobre la tabla de login. Riesgo de romper el acceso de todos los usuarios si se hace mal.
- **Índice de `identifier_hash` no compuesto con `client_id`:** no hace falta. El `UNIQUE (client_id, channel, identifier_hash)` ya cubre las búsquedas por ese prefijo; el índice de una columna es redundante pero inocuo, y quitarlo es una migración sin beneficio funcional.

### BUG-039: El `access_token` de Meta (y ahora el token del bot de Telegram) llegaba a los spans de Jaeger
- **Descripción:** la instrumentación de httpx (PR #22) guarda la URL completa en `http.url` de cada span. `meta.py` manda el Page Access Token como **query param** (`params={"access_token": ...}`), así que quedaba en la traza de cada mensaje enviado por Instagram/Facebook desde el Sprint 8. Telegram (Sprint 9) lo empeora: la Bot API exige el token en el **path** (`/bot<TOKEN>/metodo`), sin alternativa.
- **Estado:** CERRADO. `app/core/telemetry.py::redactar_url()` reemplaza por `***` el token del path de Telegram y los query params sensibles (`access_token`, `token`, `api_key`, `key`, `secret`), enganchada como `request_hook` y `async_request_hook` del instrumentador. Probado con el instrumentador real y un exportador en memoria, con cliente síncrono y asíncrono.
- **Complemento en el provider:** `TelegramProvider` reescribe los errores de red sin la URL (`str(httpx.HTTPError)` la incluye) y no encadena la excepción original (`from None`), porque ese texto acaba en los logs de la tarea de Celery.
- **Lección transferible:** una instrumentación automática que registra "la URL" registra también lo que viaja en ella; hay que revisar qué APIs ponen credenciales en la URL antes de activarla.

### BUG-040: El endpoint de webhooks leía el cuerpo entero antes de autenticar, sin tope
- **Descripción:** `receive_webhook()` hace `await request.body()` al empezar (el HMAC lo necesita) y solo después valida la firma. Sin límite, cualquiera puede mandarle cientos de MB a `POST /api/v1/webhooks/...` y cargarlos en memoria sin autenticarse. Con el Inbound Parse de email (que admite adjuntos de hasta 30 MB) el problema deja de ser teórico.
- **Estado:** CERRADO para lo que declara `Content-Length`: se rechaza con 413 (`PAYLOAD_TOO_LARGE`) lo que supere `MAX_WEBHOOK_BODY_BYTES` (32 MB), sin leerlo. **No cubre** un cuerpo con `Transfer-Encoding: chunked` (no trae `Content-Length`); ahí el tope tiene que ponerlo Traefik/Cloudflare.

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
