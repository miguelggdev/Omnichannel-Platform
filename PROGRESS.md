# PROGRESS.md — Estado del Proyecto

> Cada sesión de Claude lee este archivo al inicio y lo actualiza al terminar.
> Es el "cerebro compartido" entre sesiones.

---

## Estado Actual

- **Fase:** 1 — MVP Core
- **Sprint Activo:** Sprint 4 — Webhook Receiver & MessagingProvider
- **Última actualización:** 2026-09-10
- **Última sesión:** Sesión 10 — Merge de PR #2 (Sprint 3 FastAPI Core, Dev A) y PR #3 (Sprint 2 infra Dev B). Housekeeping pre-Sprint 4: limpieza de branches, actualización de PROGRESS.md

---

## Sprint 1: Schema DDL & Arquitectura

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

---

## Resumen por Sprint

| Sprint | Nombre | Estado | Notas |
|---|---|---|---|
| 1 | Schema DDL & Arquitectura | ✅ Completado | 24 tablas, RLS verificado, DDL idempotente |
| 2 | Infraestructura Docker | ✅ Completado | 12 servicios (ADR-020), Dockerfile multi-stage, Traefik v3, 6 colas Celery |
| 3 | FastAPI Core & Auth | ✅ Completado | 61 archivos, +3460 líneas. Auth JWT, middleware multi-tenant, modelos SQLAlchemy, Alembic, CI 8/8 green |
| 4 | Webhook Receiver & MessagingProvider | 🔄 En progreso | YCloud (WhatsApp) + Meta (Instagram DM + Facebook Messenger) |
| 5 | Pipeline de Documentos & RAG | ⬜ Pendiente | |
| 6 | LangGraph — Grafo de Agentes | ⬜ Pendiente | |
| 7 | Agente de Agendamiento & CRM API | ⬜ Pendiente | |
| 8 | Observabilidad, Backup & Hardening | ⬜ Pendiente | **Hito MVP** |
| 9 | Canales Adicionales | ⬜ Pendiente | Fase 2 — Telegram, Webchat, Email, Audio (Instagram/Facebook movidos a Sprint 4) |
| 10 | Templates, Clonación & Sentimiento | ⬜ Pendiente | Fase 2 |
| 11 | Webhooks Salientes & CSAT | ⬜ Pendiente | Fase 2 |
| 12 | Agentes Financiero & Marketing | ⬜ Pendiente | Fase 2 |
| 13 | Canal de Voz & Agente Clínico | ⬜ Pendiente | Fase 3 |
| 14 | Sandbox, Multi-idioma & Feature Flags | ⬜ Pendiente | Fase 3 — i18n expandido a 6 idiomas |
| 15 | Frontend Foundation & Panel Admin | ⬜ Pendiente | Fase 4 — Next.js, theme toggle, responsive, i18n UI |

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
