# PROGRESS.md — Estado del Proyecto

> Cada sesión de Claude lee este archivo al inicio y lo actualiza al terminar.
> Es el "cerebro compartido" entre sesiones.

---

## Estado Actual

- **Fase:** 1 — MVP Core
- **Sprint Activo:** Sprint 1 — Schema DDL & Arquitectura
- **Última actualización:** 2026-09-05
- **Última sesión:** Sesión 6 — Git simplificado (sin develop), Dev Playbook, CI/CD pipeline, pre-commit hooks, RLS tests expandidos, Token Budget dashboard

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

## Resumen por Sprint

| Sprint | Nombre | Estado | Notas |
|---|---|---|---|
| 1 | Schema DDL & Arquitectura | ✅ Completado | 24 tablas, RLS verificado, DDL idempotente |
| 2 | Infraestructura Docker | ⬜ Pendiente | Depende de Sprint 1 |
| 3 | FastAPI Core & Auth | ⬜ Pendiente | |
| 4 | Webhook Receiver & MessagingProvider | ⬜ Pendiente | YCloud (WhatsApp) + Meta (Instagram DM + Facebook Messenger) |
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

---

## Métricas de Progreso

- **Tests pasando:** 0 / 9 (RLS tests skip hasta Sprint 2 DB)
- **Tablas creadas:** 25 / 25 (18 MVP + 6 Fase 2 + 1 agent_action_logs)
- **Endpoints implementados:** 0
- **Agentes LangGraph:** 0 / 7 (nodos)
- **Proveedores de mensajería:** 0 / 2 (YCloud MVP + Meta MVP: Instagram DM + Facebook Messenger)
- **Cobertura de tests:** N/A
- **DDL verificado:** ✅ PostgreSQL 16 + pgvector 0.6.0
- **RLS verificado:** ✅ 24 tablas con aislamiento confirmado
- **Sprints especificados:** 15 / 15 (14 originales + 1 Frontend)
- **Features nuevas integradas:** 11 / 11
