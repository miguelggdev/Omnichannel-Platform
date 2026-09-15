# PROGRESS.md — Estado del Proyecto

> Cada sesión de Claude lee este archivo al inicio y lo actualiza al terminar.
> Es el "cerebro compartido" entre sesiones.

---

## Estado Actual

- **Fase:** 1 — MVP Core
- **Sprint Activo:** Sprint 6 — LangGraph, Grafo de Agentes
- **Última actualización:** 2026-09-15
- **Última sesión:** Sesión 14 — Revisión general de bugs sobre `main` post-Sprint 5 (pedida por el usuario). Encontrados y arreglados: **BUG-010** (`RAGService` rompía contra Postgres real por falta de cast `::vector`, ver MEMORY.md — [PR #11](https://github.com/miguelggdev/Omnichannel-Platform/pull/11) mergeado, verificado en CI real con 5 tests de integración nuevos: 46 passed, 6 skipped). También se identificaron dos riesgos que quedan pendientes de decisión del usuario, no arreglados aún: el engine async de `app/core/database.py` es un singleton de módulo reusado por `asyncio.run()` en cada tarea de Celery (`webhook_processor.py`, `document_ingestion.py`) — mismo root cause que BUG-006, sin mitigar en producción — y `DocumentPipeline` marca `completed` con `chunk_count=0` cuando OCR no extrae texto, sin señalizarlo como fallo. Una sospecha inicial de bug en el pin de `redis` (mypy fallaba en un venv local sin `types-redis`) se descartó: es un falso positivo, CI ya instala `types-redis` aparte y con eso mypy queda limpio.
- **Sesión 13** — **Entrega de Dev A del Sprint 5** ([PR #10](https://github.com/miguelggdev/Omnichannel-Platform/pull/10)): `app/services/{chunker,embedding,ocr,document_pipeline,rag}.py`. Sprint 5 completo (Dev A + Dev B). Verificado en CI real: 41 passed contra el rol `app_user` (no-superusuario, `NOBYPASSRLS`) — confirma que la RLS de BUG-009 sigue genuina en este PR también. 173 tests unitarios, `ruff`/`mypy` limpios.
- **Sesión 12** —
  1. Revisión y fix de [PR #5](https://github.com/miguelggdev/Omnichannel-Platform/pull/5) (Sprint 4, Dev B): bug de `channel` en `_enqueue_ai_processing`, `retry_backoff` sin efecto, env vars faltantes en `docker-compose.yml`.
  2. BUG-005 / issue [#6](https://github.com/miguelggdev/Omnichannel-Platform/issues/6) resuelto de raíz: migración `002_rls_policies.py` con RLS en las 18 tablas, CI corregido para sembrar el schema vía `alembic upgrade head` (no `init.sql`) y correr `tests/integration/` con `--run-db` (antes se saltaba entero, silenciosamente, desde Sprint 1). De paso salieron a la luz y se arreglaron 4 bugs más que ningún test había ejecutado nunca contra Postgres real: `SET LOCAL` con bind params, `db_engine` de test con scope de sesión vs. event loop por test, casts `::vector` pegados a un bind param, y doble consumo de un `Result` de SQLAlchemy.
  3. [PR #4](https://github.com/miguelggdev/Omnichannel-Platform/pull/4) (deuda de lint) revisado y mergeado.
  4. PR #5 actualizado contra `main` y verificado: 2 bugs más en `test_webhook_flow.py` (mismo patrón de `clients.max_agents` inexistente, y el engine singleton de `app.core.database` reusado entre tests con loops distintos). PR #4 y #5 mergeados.
  5. **Entrega de Dev A del Sprint 4** ([PR #8](https://github.com/miguelggdev/Omnichannel-Platform/pull/8)): `app/services/messaging/` (ABC, `YCloudProvider`, `MetaProvider`, factory) y `NormalizedMessage`. Sprint 4 completo.
  6. **Corrección importante (ver BUG-009 en MEMORY.md):** lo que en el punto 2 llamé "RLS real activado" en CI **era un falso positivo**. Dev B encontró, en [PR #9](https://github.com/miguelggdev/Omnichannel-Platform/pull/9) (Sprint 5), que el rol de Postgres en CI era superusuario (bypassa RLS siempre, incluso con `FORCE`) y que además `assert_rls_isolation()` estaba probando aislamiento MVCC, no RLS, desde Sprint 1 — los dos defectos se enmascaraban entre sí. Revisé el fix a fondo (diff, logs reales de CI, verificación local) antes de mergear: `ci.yml` ahora crea un rol `app_user` no-superusuario para correr la suite, y el helper de aislamiento cambia el tenant activo dentro de la misma transacción no commiteada en vez de cruzar conexiones. CI real: 41 passed (antes 20, con el falso positivo). **Esta vez sí es una verificación genuina de RLS.**
  7. Revisado y mergeado [PR #9](https://github.com/miguelggdev/Omnichannel-Platform/pull/9) (Sprint 5, Dev B): CRUD de documentos, Supabase Storage, worker de ingesta, BUG-007 (fixture de auth roto desde Sprint 3) y BUG-008 (`created_at` sin refresh en async). 156 tests unitarios + 41 de integración, `ruff`/`mypy` limpios. Falta la entrega de Dev A (`document_pipeline`, `chunker`, `embedding`, `ocr`, `rag`).

---

## Sprint 1: Schema DDL & Arquitectura

> **Corregido 2026-09-10 (BUG-005, issue #6):** la migración baseline de Alembic
> (`migrations/versions/001_baseline.py`) no habilitaba RLS en ninguna tabla —
> las líneas de abajo que dan por hecho "RLS aislamiento verificado" se referían
> únicamente a `supabase/init/init.sql`, que ADR-020 dejó de ejecutar contra
> Supabase Cloud. Una base creada solo con Alembic quedaba sin aislamiento entre
> tenants. Fix: `migrations/versions/002_rls_policies.py` agrega
> ENABLE/FORCE/CREATE POLICY a las 18 tablas reales, y `.github/workflows/ci.yml`
> ahora siembra el schema de test vía `alembic upgrade head` (no `init.sql`) y
> corre `tests/integration/` con `--run-db` (antes se saltaba entero, sin que CI
> avisara). `tests/unit/test_rls_isolation.py` se eliminó: sus 9 tests tenían
> `@pytest.mark.skip` individual desde Sprint 1 ("activar cuando init.sql esté
> ejecutado", algo que su propio `--run-db` nunca disparaba) y probaban columnas
> que ya no existen (`contacts.phone_number`, `users.full_name`); quedó
> completamente superado por `tests/integration/test_rls_all_tables.py`.

### Completado
- [x] Diseño de arquitectura general (SDD publicado como artifact)
- [x] Definición de 24 tablas (18 MVP + 6 Fase 2)
- [x] Definición de patrones RLS con SET LOCAL
- [x] Especificaciones detalladas en `specs/sprint-01-schema.md`
- [x] DDL ejecutable completo (`supabase/init/init.sql`) — 520+ líneas
- [x] Extensiones: pgvector, pgcrypto, uuid-ossp
- [x] 18 tablas MVP con constraints, FKs, ENUMs
- [x] 6 tablas Fase 2 en archivo separado (`supabase/init/phase2_tables.sql`)
- [x] Políticas RLS con FORCE en las 24 tablas (patrón `current_setting('app.current_client_id')::uuid`)
- [x] Índices: 44 B-tree, 8 GIN (JSONB), 2 HNSW (embeddings m=16 ef=200)
- [x] Roles app_user y app_admin con ALTER DEFAULT PRIVILEGES
- [x] Funciones helper: update_updated_at_column(), encrypt_identifier(), decrypt_identifier()
- [x] DDL verificado contra PostgreSQL 16 + pgvector 0.6.0 — ejecución sin errores
- [x] Idempotencia verificada (segunda ejecución sin errores)
- [x] RLS aislamiento verificado con rol no-superuser (test_app_user)
- [x] Test de aislamiento RLS en Python (`tests/unit/test_rls_isolation.py`) — 9 tests (skip hasta Sprint 2 DB)
- [x] Contract file Sprint 1 (`contracts/sprint-01.json`)
- [x] Harness Engineering completo: hooks, smoke-test, contracts, loop-failure template

### En Progreso
_(nada en progreso)_

### Pendiente
- [ ] Diagrama Mermaid de arquitectura en repo
- [ ] Commit inicial y push a GitHub (usuario debe ejecutar manualmente)
- [ ] Crear rama `feature/sprint-01-ddl` desde `main`

### Bloqueadores
- Push a GitHub requiere ejecución manual desde terminal del usuario (credenciales no disponibles en sesión cloud)

### Infraestructura CI/CD & QA (Sesión 6)
- [x] `.pre-commit-config.yaml` — ruff, mypy, detect-secrets, sqlfluff, commitizen
- [x] `.github/workflows/ci.yml` — 8 stages: lint → typecheck → test-unit → test-integration → migration-check → security → docker-build → frontend
- [x] `tests/conftest.py` — Fixtures expandidas con rls_harness, tenant sessions, API client, TestDataFactory
- [x] `tests/integration/test_rls_all_tables.py` — 25 tests de aislamiento RLS (todas las tablas)
- [x] `grafana/dashboards/token-budget-monitoring.json` — 10 paneles: tokens/tenant, tokens/nodo, costos, eficiencia, alertas
- [x] `pyproject.toml` — Actualizado: reglas de seguridad, coverage, commitizen, bandit
- [x] `.secrets.baseline` — Baseline para detect-secrets
- [x] `docs/dev-playbook.html` — Dev Playbook con 8 agentes + 6 roles secundarios

### Notas para la Próxima Sesión
- Recordar el bug del alias SQL en WHERE: usar `1 - (embedding <=> :query_embedding) > :threshold` en lugar de `similarity > :threshold`
- Superusers bypasean RLS incluso con FORCE — siempre testear con rol app_user
- `conversations` tiene los 7 estados: new, active, waiting, resolved, escalated, bot, snoozed
- Sprint 2 (Docker) puede comenzar inmediatamente

---

## Sprint 2: Infraestructura Docker

> **Corregido 2026-09-09:** este apartado describía 16 servicios con `supabase-db`,
> `supabase-auth`, `supabase-storage`, `supabase-realtime` y `pgbouncer`, y notas con
> `PGBOUNCER_URL`. Eso quedó obsoleto con ADR-020 (Supabase Cloud). El
> `docker-compose.yml` real en `main` ya tenía 12 servicios desde el commit `33f62eb`;
> era este archivo el que estaba desactualizado.

### Completado — Dev A
- [x] `docker-compose.yml` — 12 servicios orquestados con health checks (sin Postgres/Auth/Storage: ADR-020)
- [x] `Dockerfile` — Multi-stage (builder + runner) con tesseract-ocr, usuario no-root
- [x] `app/tasks/celery_config.py` — 6 colas (webhooks, ai_inference, documents, notifications, bulk, lead_enrichment) + beat schedule
- [x] `.dockerignore` — 45 reglas de exclusion del contexto Docker
- [x] `.env.example` — `DATABASE_URL` (pooler Supavisor 6543) + `DATABASE_URL_DIRECT` (migraciones Alembic)

### Completado — Dev B (branch `feature/sprint-02-traefik`, sesión 9)
- [x] `traefik/traefik.yml` — Configuracion estatica Traefik v3 (entrypoints, providers, logging)
- [x] `traefik/traefik.yml` — `ping` habilitado: sin él el healthcheck `traefik healthcheck` de compose nunca pasaba a `healthy` (ADR-027)
- [x] `traefik/traefik.yml` — `metrics.prometheus` sobre el entryPoint interno `traefik` (8080)
- [x] `traefik/dynamic/middlewares.yml` — Rate limiting, security headers, compresion, CORS
- [x] `traefik/dynamic/tls.yml` — Certificados autofirmados para desarrollo
- [x] `prometheus/prometheus.yml` — Jobs `prometheus`, `traefik`, `api`. El archivo faltaba y compose ya lo montaba, así que el contenedor no arrancaba
- [x] `grafana/provisioning/datasources/prometheus.yml` — Datasource Prometheus (`uid: prometheus`)
- [x] `grafana/provisioning/dashboards/dashboards.yml` — Provider de dashboards tipo file
- [x] `docker-compose.yml` — Montaje de Grafana corregido para que el provisioning se aplique (archivo de Dev A, tocado con autorización explícita y aislado en el commit `005dfb6` — **revisar en el PR**)
- [x] `scripts/wait-for-it.sh` — Script de espera TCP para dependencias
- [x] `scripts/healthcheck.sh` — Verifica los 12 servicios, espera hasta 120s, exit 0/1/2/3

### Servicios Docker (12) — ADR-020
1. traefik (API Gateway v3)
2. api (FastAPI x2 replicas)
3. redis (Cache + Broker, 3 DBs separadas)
4. celery-webhooks (c=4)
5. celery-ai (c=2)
6. celery-documents (c=2)
7. celery-notifications (c=2)
8. celery-bulk (c=1)
9. celery-lead-enrichment (c=2, ADR-022)
10. celery-beat (Scheduler)
11. prometheus (Metricas)
12. grafana (Dashboards)

> PostgreSQL, Auth, Storage y Realtime los provee **Supabase Cloud**. El pooling lo
> maneja Supavisor (puerto 6543). No hay `pgbouncer` ni contenedores `supabase-*`.

### Pendiente
- [ ] Validacion funcional con `docker compose up -d` (requiere entorno Docker del usuario) — `scripts/healthcheck.sh` automatiza la verificación
- [ ] Verificar `SET LOCAL` contra el Transaction Pooler de Supabase Cloud (sustituye a la vieja verificación de `SHOW pools` de pgBouncer)
- [ ] Correr la migración inicial de Alembic contra `DATABASE_URL_DIRECT` (Dev A) — sustituye a la vieja verificación de `init.sql`
- [ ] BUG-003: `grafana/dashboards/token-budget-monitoring.json` apunta al datasource `supabase-db`, inexistente con ADR-020 — asignado a Dev A en Sprint 8

### Bloqueadores
- **`mypy` no está instalado en el entorno** (`No module named mypy` en todos los intérpretes). El Gate 2 pre-PR de METHODOLOGY §7 lo exige. Solución: `pip install mypy`
- Deuda de lint preexistente en `main`: `ruff check .` reporta 8 errores en `tests/conftest.py` (UP035, 3×SIM117) y `tests/integration/test_rls_all_tables.py` (4×S608). No los introduce Sprint 2; el Gate 1 (`ruff check app/`) está verde

### Notas para la Proxima Sesion
- celery-beat no tiene healthcheck (es scheduler, no endpoint): `scripts/healthcheck.sh` lo evalúa como `running`, no como `healthy`
- Redis separado en 3 DBs: 0=broker, 1=results, 2=cache
- Traefik redirecciona HTTP→HTTPS automaticamente
- Workers Celery y API usan `DATABASE_URL` (pooler Supavisor). `PGBOUNCER_URL` ya no existe
- El puerto 8080 de Traefik sirve dashboard, `/ping` y `/metrics`: cerrarlo al exterior en el hardening del Sprint 8 (ADR-027)
- `accessLog` de Traefik va a stdout, desviación consciente de la spec justificada en ADR-026
- Los tests marcados `db` (34) siguen en `skipped` hasta tener la BD accesible con `--run-db`
- Sprint 3 (FastAPI Core & Auth) completado — PR #2 mergeado
- Sprint 4 (Webhook Receiver & MessagingProvider) puede comenzar inmediatamente

---

## Sprint 3: FastAPI Core & Auth

### Completado — Dev A (PR #2, 61 archivos, +3460 líneas)
- [x] `app/main.py` — App factory `create_app()` con lifespan, CORS, exception handlers
- [x] `app/core/config.py` — `Settings` (Pydantic BaseSettings) con validación de JWT_SECRET y ENCRYPTION_KEY
- [x] `app/core/database.py` — AsyncEngine + async sessionmaker con pool pre-ping
- [x] `app/core/security.py` — JWT encode/decode, password hashing (bcrypt), token creation
- [x] `app/core/dependencies.py` — `get_db`, `get_current_user`, `get_current_active_user`
- [x] `app/middleware/tenant_context.py` — `TenantContextMiddleware` con `SET LOCAL app.current_client_id`
- [x] `app/middleware/logging_middleware.py` — Request/response logging con correlation ID
- [x] `app/models/` — SQLAlchemy 2.0 models: User, Client, Conversation, Message, Contact, KnowledgeDocument, etc.
- [x] `app/schemas/` — Pydantic v2 schemas para auth, users, tenants, health
- [x] `app/api/v1/auth.py` — Login, register, refresh, me endpoints
- [x] `app/api/v1/tenants.py` — CRUD tenants (admin)
- [x] `app/api/v1/health.py` — Health check endpoint
- [x] `app/api/deps.py` — Dependency injection helpers
- [x] `migrations/env.py` — Alembic async config con `run_async_migrations()`
- [x] `migrations/versions/001_initial_schema.py` — Initial migration (all MVP tables)
- [x] `tests/unit/` — 27 tests pasando (auth, middleware, health, config, schemas)
- [x] `.github/workflows/ci.yml` — CI pipeline 8 stages, todas pasando green
- [x] Docker Build smoke test con env vars dummy

### Notas para la Próxima Sesión
- `get_settings()` se ejecuta al importar `app/main.py` — requiere env vars incluso para smoke tests
- Los modelos SQLAlchemy usan `Mapped[]` (SQLAlchemy 2.0 style)
- Alembic migration corre contra `DATABASE_URL_DIRECT`, no el pooler
- CI pipeline: lint → typecheck → unit-test → integration → migration-check → security → docker-build → frontend

## Sprint 4: Webhook Receiver & MessagingProvider

### Completado — Dev B (branch `feature/sprint-04-webhooks`, sesión 11)
- [x] `app/api/v1/webhooks.py` — `POST /api/v1/webhooks/{provider}/{channel}`: firma HMAC → parseo → dedup Redis → encolar → 200. No toca la base de datos (presupuesto <100ms)
- [x] `app/api/v1/webhooks.py` — `GET` de verificación: Meta (`hub.mode=subscribe` + `hub.verify_token` → `hub.challenge` en text/plain) y YCloud (challenge simple)
- [x] `app/main.py` — router montado en `/api/v1/webhooks`, que coincide con `WEBHOOK_PATHS_PREFIX` del middleware (los webhooks no pasan por `TenantContextMiddleware`)
- [x] `app/services/dedup.py` — dedup en dos niveles (PAT-001): Redis `SET NX EX 24h` + `webhook_dedup` en PostgreSQL. Fail-open ante caída de Redis (ADR-028) y `release_mark()` si falla el encolado (ADR-029)
- [x] `app/tasks/webhook_processor.py` — tarea `app.tasks.webhook_process_incoming` (cola `webhooks`, retry 5s/25s/125s, DLQ en `dlq:webhook_messages`). Todo el flujo en una transacción con `SET LOCAL`
- [x] `app/tasks/celery_app.py` + `app/tasks/__init__.py` — registro de tareas vía `imports`. Sin esto el worker levantaba las colas pero no conocía ninguna tarea
- [x] `app/core/config.py` + `.env.example` — vars de Meta/YCloud, `DEFAULT_CLIENT_ID` y `extra="ignore"` (BUG-004)
- [x] `app/main.py` + `app/api/internal/health.py` — tipado de `redis.ping()`; `mypy app/ --ignore-missing-imports` pasa de 2 errores a 0
- [x] `tests/unit/test_webhooks.py` (23 tests) y `tests/unit/test_webhook_processor.py` (20 tests) — verdes sin DB
- [x] `tests/unit/test_meta_provider.py` — contrato de MetaProvider, con `importorskip` hasta que Dev A entregue `meta.py`
- [x] `tests/integration/test_webhook_flow.py` — flujo completo contra PostgreSQL (marker `db`) para los 3 canales
- [x] `tests/fixtures/meta_payloads.py` — payloads de Instagram, Facebook y YCloud

### Completado — Dev A (branch `feature/sprint-04-messaging`, sesión 12)
- [x] `app/services/messaging/base.py` — `MessagingProvider` ABC (5 métodos: `parse_webhook`, `validate_signature`, `send_message`, `send_template`, `get_channel_constraints`) + `ChannelConstraints`/`MessageContent`/`TemplateMessage`
- [x] `app/schemas/message.py` — `NormalizedMessage`, `ChannelEnum`, `MessageTypeEnum` (usando `enum.StrEnum`, no `(str, Enum)` — `ruff` UP042 en Python 3.11+)
- [x] `app/services/messaging/ycloud.py` — `YCloudProvider`: parseo texto/media/ubicación, firma HMAC-SHA256 (`X-Ycloud-Signature`, hex sin prefijo), `send_message`/`send_template` contra `settings.YCLOUD_BASE_URL`
- [x] `app/services/messaging/meta.py` — `MetaProvider`: un solo provider para Instagram DM y Facebook Messenger (subcanal en `provider_config["channel"]`), firma `x-hub-signature-256` (`sha256=<hex>`), `send_template` en Instagram lanza `NotImplementedError`
- [x] `app/services/messaging/factory.py` — `get_messaging_provider(provider_name, provider_config=None)`, `_PROVIDERS = {"ycloud": ..., "meta": ...}`
- [x] Activa los 33 tests de Dev B que esperaban esta entrega con `importorskip`: `tests/unit/test_messaging_provider.py` (20) y `tests/unit/test_meta_provider.py` (13). Suite completa: 100/100, `ruff check`/`format` y `mypy app/ --config-file=pyproject.toml` limpios

### Ajuste sobre la spec (`specs/sprint-04-webhooks.md` §5 y §4)
- La factory de la spec construye `MetaProvider()` sin argumentos cuando `provider_config` es falsy — pero `_resolve_provider()` en `webhooks.py` (Dev B, ya en `main`) **siempre** pasa `{"channel": channel}`, nunca `None` ni `{}`. Se ajustó a `provider_class(provider_config or {})` sin la condición `and provider_config`.
- El `__init__` de `MetaProvider` en la spec exige `page_access_token`/`app_secret` en el dict (`provider_config["page_access_token"]`, indexación directa) — pero el endpoint real solo pasa `{"channel": channel}` al recibir un webhook, así que con la spec tal cual el constructor reventaría con `KeyError` en **todo** mensaje entrante de Meta. Se cambió a `.get(..., "")`: ninguno de los dos se usa desde `self` en `send_message`/`send_template` (llegan por `channel_config` en cada llamada) ni en `validate_signature` (el secreto llega por parámetro desde `settings.META_APP_SECRET`), así que no hacía falta que fueran obligatorios.

### Bloqueadores
- ~~**BUG-005 (CRÍTICO):** `migrations/versions/001_baseline.py` no crea RLS~~ — **Resuelto 2026-09-14** (issue [#6](https://github.com/miguelggdev/Omnichannel-Platform/issues/6)): `migrations/versions/002_rls_policies.py` agrega RLS a las 18 tablas, y CI ahora lo verifica de verdad (`alembic upgrade head` + `pytest --run-db`, en vez de `init.sql` + tests silenciosamente saltados). Ver MEMORY.md (BUG-005 y NOTA-002) y la nota en Sprint 1 arriba.
- ~~**Sin la entrega de Dev A, el POST respondía 400**~~ — **Resuelto 2026-09-14**: con `app/services/messaging/` completo, la factory resuelve `ycloud`/`meta` y el POST ya no depende de un import perezoso que fallaba.

### Notas
- La spec asume campos que los modelos de Dev A no tienen: `Message.contact_id` (se usa `sender_type="contact"` + `sender_id`), `ContactIdentifier.is_primary`, `WebhookDedup.processed`
- Los identifiers se guardan en claro: `ContactIdentifier.identifier_value` es `String(255)` con unique en `(client_id, channel, identifier_value)`. El cifrado con pgcrypto que pide CLAUDE.md §3 necesita una columna de hash para poder buscar — pendiente de decidir entre Dev A y Dev B
- Cobertura de los archivos nuevos sin `--run-db`: webhooks.py 93%, dedup.py 78%, webhook_processor.py 85%, celery_app.py 100% (total 86%)

---

## Sprint 5: Pipeline de Documentos & RAG

### Completado — Dev B (branch `feature/sprint-05-rag`, sesión 13)
- [x] `app/api/v1/documents.py` — CRUD completo: subida (valida tipo y 50 MB), listado paginado con filtro por status, detalle con recuento real de chunks, borrado (documento + chunks + archivo) y reprocesado
- [x] `app/services/storage.py` — cliente de Supabase Storage (upload/download/delete) con aislamiento por prefijo de ruta (ADR-032). `download_from_storage()` es la que consumirá el pipeline de Dev A
- [x] `app/tasks/document_ingestion.py` — tarea `app.tasks.document_ingest` (cola documents, 2 reintentos, time_limit 600s / soft 540s). Timeout y ausencia del pipeline no se reintentan: dejan el documento en `failed` con el motivo visible
- [x] `app/main.py` — router montado en `/api/v1/documents`
- [x] `app/core/config.py` + `.env.example` — `SUPABASE_STORAGE_BUCKET`
- [x] `tests/unit/test_documents.py` — 56 tests verdes sin DB (endpoint, permisos por rol, Storage con httpx sustituido, worker)
- [x] `tests/unit/test_rag.py` — contrato del retrieval, con `importorskip` hasta que Dev A entregue `rag.py`
- [x] `tests/integration/test_document_pipeline.py` — flujo contra PostgreSQL real con RLS: aislamiento entre dos tenants a través de los endpoints, borrado de chunks, reprocesado, filtros y paginación
- [x] Deuda de Sprint 4 cerrada: el worker de webhooks usa `NormalizedMessage(**message_data)` y `app.services.messaging.*` sale del override de mypy
- [x] BUG-007 y BUG-008 (ver MEMORY.md)

### Completado — Dev A (branch `feature/sprint-05-pipeline`, sesión 13, [PR #10](https://github.com/miguelggdev/Omnichannel-Platform/pull/10))
- [x] `app/services/chunker.py` — `DocumentChunker`: FAQ (par pregunta/respuesta), tablas (bloques con "|"/tab preservados enteros) y texto general vía `RecursiveCharacterTextSplitter` (chunk_size/overlap en **caracteres**, no tokens — ver desviación de spec abajo)
- [x] `app/services/embedding.py` — `EmbeddingService` (OpenAI `text-embedding-3-small` por defecto), `embed_batch()` con pausa de 0.5s entre lotes de 100
- [x] `app/services/ocr.py` — `OCRService` (Tesseract `spa+eng`), preprocesa a escala de grises + binarización + filtro de mediana; PDFs se rasterizan a 300dpi con PyMuPDF antes de OCR
- [x] `app/services/document_pipeline.py` — `DocumentPipeline.process()`: descarga → detecta si necesita OCR (imagen siempre; PDF con <50 caracteres extraídos) → extrae/OCR → chunkea → embebe → guarda. No atrapa excepciones (las deja subir al manejador de `document_ingestion.py`, que decide reintento o `failed`)
- [x] `app/services/rag.py` — `RAGService.retrieve()` con filtro pre-vectorial de `client_id` y umbral de similitud repetidos en el WHERE (BUG-001-safe, sin alias de SELECT), `retrieve_few_shot_examples()`, `build_grounded_prompt()`
- [x] Activa los 18 tests de contrato de Dev B (`tests/unit/test_rag.py`, `importorskip`). Suite completa: 173 tests unitarios, `ruff`/`mypy` limpios
- [x] CI real verificado: `Integration Tests (RLS + DB)` corrió 41 passed contra el rol `app_user` (no-superusuario, `NOBYPASSRLS`) — no es un checkmark ciego, se leyó el log

### Ajuste sobre la spec (`specs/sprint-05-rag.md`)
- **Chunking por caracteres, no por tokens:** el spec sugiere un `length_function` basado en tokens; el test de contrato de Dev B (`test_respeta_el_tamano_maximo`) verifica `len(chunk.content) <= chunk_size * 1.2` en caracteres. Se dejó el default de `RecursiveCharacterTextSplitter` (`len`); `token_count` se calcula aparte con `tiktoken`, solo como metadata para `document_chunks.token_count`.
- **PyMuPDF en vez de `pdf2image`** para rasterizar PDFs a imagen antes de OCR: `pdf2image` depende del binario de sistema `poppler-utils`, ausente del `Dockerfile`; PyMuPDF es una dependencia de pip autocontenida (motor MuPDF embebido).

### Notas
- Mientras el pipeline de Dev A no estuvo, los documentos subidos quedaban en `failed` con el motivo explícito en `metadata.error`; se recuperaban con `POST /documents/{id}/reprocess` sin volver a subir el archivo
- La spec asume campos que el modelo no tiene (`file_path`, `file_size_bytes`, `uploaded_by`): se usan `file_url`, `file_size` y `metadata.uploaded_by`
- `document_chunks` no declara `ON DELETE CASCADE`, así que el borrado los elimina explícitamente
- Cobertura de los archivos nuevos: `documents.py` 97%, `storage.py` 97%, `document_ingestion.py` 87% (total 95%)
- Se eliminó `test_pipeline_ausente_se_detecta` de `tests/unit/test_documents.py`: su premisa ("Dev A no entregó, el import falla") dejó de ser cierta y el test empezaba a intentar una conexión real a Postgres inexistente en CI unitario

---

## Resumen por Sprint

| Sprint | Nombre | Estado | Notas |
|---|---|---|---|
| 1 | Schema DDL & Arquitectura | ✅ Completado | 24 tablas, RLS verificado, DDL idempotente |
| 2 | Infraestructura Docker | ✅ Completado | 12 servicios (ADR-020), Dockerfile multi-stage, Traefik v3, 6 colas Celery |
| 3 | FastAPI Core & Auth | ✅ Completado | 61 archivos, +3460 líneas. Auth JWT, middleware multi-tenant, modelos SQLAlchemy, Alembic, CI 8/8 green |
| 4 | Webhook Receiver & MessagingProvider | ✅ Completado | Dev B (PR #5, mergeado) + Dev A (branch `feature/sprint-04-messaging`, pendiente de PR/merge): endpoint, dedup, worker, MessagingProvider ABC, YCloudProvider, MetaProvider, factory, `NormalizedMessage`. 100/100 tests, RLS verificado en CI real |
| 5 | Pipeline de Documentos & RAG | ✅ Completado | Dev B (PR #9): CRUD de documentos, Storage, worker de ingesta. Dev A (PR #10): chunker, embedding, OCR, DocumentPipeline, RAGService. 173 tests, RLS verificado en CI real (`app_user`, 41 passed) |
| 6 | LangGraph — Grafo de Agentes | 🔄 En progreso | |
| 7 | Agente de Agendamiento & CRM API | ⬜ Pendiente | |
| 8 | Observabilidad, Backup & Hardening | ⬜ Pendiente | **Hito MVP** |
| 9 | Canales Adicionales | ⬜ Pendiente | Fase 2 — Telegram, Webchat, Email, Audio (Instagram/Facebook movidos a Sprint 4) |
| 10 | Templates, Clonación & Sentimiento | ⬜ Pendiente | Fase 2 |
| 11 | Webhooks Salientes & CSAT | ⬜ Pendiente | Fase 2 |
| 12 | Agentes Financiero & Marketing | ⬜ Pendiente | Fase 2 |
| 13 | Canal de Voz & Agente Clínico | ⬜ Pendiente | Fase 3 |
| 14 | Sandbox, Multi-idioma & Feature Flags | ⬜ Pendiente | Fase 3 — i18n expandido a 6 idiomas |
| 15 | Frontend Foundation & Panel Admin | ⬜ Pendiente | Fase 4 — Next.js, theme toggle, responsive, i18n UI |
| 16-19 | Módulo de Lead Management con IA | ⬜ Pendiente | Fase 5 — captura, enriquecimiento, calificación, asignación, follow-up, agenda (`specs/sprint-16-19-lead-management.md`, ADRs 021-025, tablas #27-36). **No listado en `docs/sprint-map.html` ni `METHODOLOGY.md`** — esos dos quedaron desactualizados (dicen "15 sprints"), la spec ya existe |

---

## Features Integradas (Sesión 5)

Las siguientes 11 features fueron diseñadas e integradas en los sprints existentes:

| # | Feature | Sprint(s) | Spec File |
|---|---|---|---|
| 1 | Client Onboarding Flow | Sprint 3 | `specs/sprint-03-addendum-onboarding.md` |
| 2 | Business Personalization | Sprint 3 | `specs/sprint-03-addendum-onboarding.md` |
| 3 | Dark/Light Theme Toggle | Sprint 15 | `specs/sprint-15-frontend.md` |
| 4 | Responsive Design | Sprint 15 | `specs/sprint-15-frontend.md` |
| 5 | Multi-idioma (6 idiomas) | Sprint 14 + 15 | `specs/sprint-14-sandbox-i18n.md` (backend) + `specs/sprint-15-frontend.md` (UI) |
| 6 | Celery/Redis Admin Panel | Sprint 8 | `specs/sprint-08-addendum-ops.md` |
| 7 | Super Admin Telegram Bot | Sprint 8 | `specs/sprint-08-addendum-ops.md` |
| 8 | Agent Activity Logging | Sprint 6 | `specs/sprint-06-addendum-agent-logging.md` |
| 9 | Client Management (deactivation/alerts) | Sprint 3 | `specs/sprint-03-addendum-onboarding.md` |
| 10 | Backup & Replication (VPS) | Sprint 8 | `specs/sprint-08-addendum-ops.md` |
| 11 | Security Policies (Cloudflare, firewall) | Sprint 8 | `specs/sprint-08-addendum-ops.md` |
| 12 | Admin Assistant (voz/chat) | Sprint 3 + 8 + 15 | `specs/sprint-03-addendum-admin-assistant.md` |

---

## Métricas de Progreso

- **Tests pasando:** 27 / 27 (unit + security; 34 RLS/DB tests skip hasta conexión BD)
- **Tablas creadas:** 26 / 26 (18 MVP + 6 Fase 2 + 1 agent_action_logs + 1 admin_assistant_history)
- **Endpoints implementados:** 8 (health, auth x4, tenants x3)
- **Agentes LangGraph:** 0 / 7 (nodos)
- **Proveedores de mensajería:** 0 / 2 (YCloud MVP + Meta MVP: Instagram DM + Facebook Messenger)
- **Cobertura de tests:** N/A
- **DDL verificado:** ✅ PostgreSQL 16 + pgvector 0.6.0 (pendiente re-verificar con tabla 26)
- **RLS verificado:** ✅ 24 tablas con aislamiento confirmado (pendiente tabla 26)
- **Sprints especificados:** 15 / 15 (14 originales + 1 Frontend)
- **Features nuevas integradas:** 12 / 12
