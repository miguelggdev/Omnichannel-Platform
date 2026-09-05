# METHODOLOGY.md — Loop Engineering para Desarrollo Multi-Dev con Claude

> Protocolo de desarrollo para 2 desarrolladores trabajando en paralelo con Claude (Cowork/Code).
> Cada sesión de Claude DEBE leer este archivo junto con CLAUDE.md antes de escribir código.

---

## 1. Filosofía

Este proyecto se desarrolla con **Loop Engineering**: cada desarrollador trabaja con su propia sesión de Claude en modo semi-autónomo. Claude ejecuta tareas dentro de un loop (implementar → tests → lint → commit), pausando en checkpoints definidos para revisión humana. El resultado es velocidad de IA con control humano en los puntos críticos.

**Principios:**
- **Spec-driven**: Claude NO inventa. Implementa lo que dicta `specs/sprint-XX-*.md`.
- **Atomic commits**: Cada iteración del loop produce exactamente UN commit funcional.
- **Shared state**: `PROGRESS.md` es la cola de tareas compartida. `MEMORY.md` es el log de decisiones. Ambos devs los leen y escriben.
- **Interface-first**: Dev A produce modelos/schemas (el contrato). Dev B los consume en servicios/API.
- **Zero merge conflicts by design**: Cada dev trabaja en archivos distintos dentro del mismo sprint.

---

## 2. Roles

### Dev A — Foundations (Cimientos)
**Responsable de:** Capa de datos, modelos, schemas, infraestructura core, servicios fundacionales.

| Capa | Archivos típicos |
|---|---|
| DDL / Migrations | `supabase/init/`, `migrations/versions/` |
| SQLAlchemy Models | `app/models/*.py` |
| Pydantic Schemas | `app/schemas/*.py` |
| Core | `app/core/config.py`, `database.py`, `security.py`, `dependencies.py`, `exceptions.py` |
| Infra | `docker-compose.yml`, `Dockerfile` |
| AI State & Graph | `app/agents/state.py`, `app/agents/graph.py` |
| Servicios fundacionales | `app/services/messaging/base.py`, `app/services/messaging/ycloud.py`, `app/services/messaging/factory.py`, `app/services/document_pipeline.py`, `app/services/rag.py` |

### Dev B — Integration (Integración)
**Responsable de:** Middleware, endpoints, workers, agent nodes, tests de integración.

| Capa | Archivos típicos |
|---|---|
| Middleware | `app/middleware/*.py` |
| API Endpoints | `app/api/v1/*.py`, `app/api/internal/*.py` |
| Services de integración | `app/services/dedup.py`, `app/services/csat.py`, `app/services/crm_scoring.py` |
| Celery Tasks | `app/tasks/*.py` |
| Agent Nodes | `app/agents/nodes/*.py` (excepto los asignados a Dev A en la matriz) |
| Tests | `tests/**/*.py` |
| Scripts | `scripts/*.py`, `scripts/*.sh` |

> **Nota:** La Matriz de Asignación (§6) es la fuente de verdad para la propiedad de archivos en cada sprint. Las tablas anteriores muestran la tendencia general de cada rol, pero cuando la matriz asigna un archivo específico a un dev, esa asignación prevalece.

### Regla de propiedad
- **Si un archivo está en la columna de tu rol, es TUYO.** No lo toques si no eres el owner.
- **Excepción**: `PROGRESS.md` y `MEMORY.md` los escriben ambos devs. Los merges de estos archivos son intencionales y se resuelven manualmente (conservar ambas entradas).
- **Archivos compartidos**: `app/__init__.py`, `app/main.py` — Dev A crea la estructura base, Dev B agrega middleware y routers. Coordinar vía PR.

---

## 3. Git Workflow (GitHub)

### Branch Strategy

```
main (protegida — PR aprobado + CI verde)
  ├── feature/sprint-01-ddl          (Dev A)
  ├── feature/sprint-01-tests-rls    (Dev B)
  ├── feature/sprint-02-docker       (Dev A)
  ├── feature/sprint-02-traefik      (Dev B)
  ├── feature/sprint-03-models       (Dev A)
  ├── feature/sprint-03-api          (Dev B)
  └── ...
```

### Convenciones de branches
- Formato: `feature/sprint-{NN}-{descripción-corta}`
- Cada dev crea su branch ANTES de iniciar el loop
- Nunca push directo a `main`

### PR Protocol
1. Dev completa su slice del sprint → crea PR a `main`
2. El OTRO dev (o su Claude) hace code review
3. CI debe pasar: `ruff check`, `mypy`, `pytest`
4. Aprobación requerida: 1 reviewer
5. Merge strategy: **Squash and merge** (un commit limpio por slice)

### Branch Protection Rules (configurar en GitHub)
```yaml
# main
- Require PR before merging
- Require 1 approval
- Require status checks: [lint, typecheck, test]
- Require branches up to date before merge
- No force pushes
- No deletions
- Sin excepciones para admins (bypass list vacía)
```

### CI Pipeline (GitHub Actions)

```yaml
# .github/workflows/ci.yml
name: CI
on:
  pull_request:
    branches: [main]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install ruff
      - run: ruff check app/ tests/

  typecheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r requirements.txt mypy
      - run: mypy app/ --ignore-missing-imports

  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg15
        env:
          POSTGRES_DB: test_omnichannel
          POSTGRES_USER: postgres
          POSTGRES_PASSWORD: test
        ports: ['5432:5432']
      redis:
        image: redis:7-alpine
        ports: ['6379:6379']
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r requirements.txt
      - run: pytest tests/ -v --tb=short --cov=app
        env:
          DATABASE_URL: postgresql+asyncpg://postgres:test@localhost:5432/test_omnichannel
          REDIS_URL: redis://localhost:6379/0
          JWT_SECRET: test-secret-key-minimum-32-chars!!
          APP_ENV: testing
```

---

## 4. Protocolo de Loop Engineering

### 4.1 Inicio de Sesión (OBLIGATORIO)

Cada sesión de Claude DEBE comenzar con este prompt (adaptar el rol):

```
Soy Dev {A|B} ({Foundations|Integration}).

1. Lee estos archivos en orden:
   - CLAUDE.md (reglas del proyecto)
   - METHODOLOGY.md (protocolo de trabajo)
   - PROGRESS.md (estado actual)
   - MEMORY.md (decisiones y bugs conocidos)

2. Lee la spec del sprint activo: specs/sprint-{NN}-*.md

3. Identifica las tareas asignadas a mi rol (Dev {A|B}) en la
   sección "Matriz de Asignación" de METHODOLOGY.md.

4. Ejecuta las tareas pendientes usando el protocolo de loop.
   Pausa en cada checkpoint para mi revisión.

Branch de trabajo: feature/sprint-{NN}-{descripcion}
```

### 4.2 Loop de Implementación (Semi-Autónomo)

```
┌─────────────────────────────────────────────────────┐
│                   LOOP START                         │
│                                                      │
│  1. Leer spec del módulo/archivo a implementar       │
│  2. Implementar UN archivo/módulo completo           │
│  3. Ejecutar tests relevantes: pytest tests/ -v      │
│  4. Ejecutar lint: ruff check app/                   │
│  5. Si tests + lint pasan:                           │
│     a. git add <archivos_específicos>                │
│     b. git commit -m "tipo(scope): descripción"      │
│     c. Actualizar PROGRESS.md                        │
│     d. Continuar al siguiente módulo                 │
│  6. Si tests fallan:                                 │
│     a. Debug automático (máximo 3 intentos)          │
│     b. Si falla 3 veces → PAUSA + reportar al dev   │
│  7. CHECKPOINT cada 3-5 módulos:                     │
│     → Resumen de lo completado                       │
│     → Diff acumulado                                 │
│     → Preguntas o decisiones pendientes              │
│     → Esperar aprobación del dev para continuar      │
│                                                      │
│  8. Al completar el slice del sprint:                │
│     a. Ejecutar test suite completo                  │
│     b. git push origin feature/sprint-XX-desc        │
│     c. Crear PR a main                            │
│     d. Notificar al otro dev para review             │
│                                                      │
└─────────────────────────────────────────────────────┘
```

### 4.3 Loop de Code Review (Claude como Reviewer)

Cuando el otro dev abre un PR, tu Claude puede revisarlo:

```
Revisa el PR #{número} en la rama feature/sprint-XX-YYY.

Verifica:
1. ¿Cumple con la spec de specs/sprint-XX-*.md?
2. ¿Tiene SET LOCAL en todas las queries? (NUNCA SET)
3. ¿Tiene type hints y docstrings Google-style?
4. ¿Los tests cubren el happy path Y al menos 2 edge cases?
5. ¿RLS está protegiendo todas las tablas con client_id?
6. ¿Hay sync I/O en contexto async? (PROHIBIDO)
7. ¿Los errores usan AppException? (NUNCA tracebacks al cliente)
8. ¿Loguru con client_id y trace_id en cada log?
```

### 4.4 Loop de Debug

Cuando un test falla persistentemente:

```
LOOP DE DEBUG (máx 3 iteraciones):
  1. Leer el error/traceback completo
  2. Identificar el archivo y línea del fallo
  3. Formular hipótesis (max 2)
  4. Aplicar fix más probable
  5. Ejecutar solo el test que falla: pytest tests/path/test_file.py::test_name -v
  6. Si pasa → commit fix + continuar
  7. Si falla → intentar hipótesis 2
  8. Si 3 intentos fallan → PAUSA
     → Escribir en MEMORY.md como BUG-XXX
     → Notificar al dev con contexto completo
```

### 4.5 Reglas de Autonomía

| Situación | Acción de Claude |
|---|---|
| Tarea clara en la spec | Implementar sin preguntar |
| Ambigüedad en la spec | PAUSA — preguntar al dev |
| Decisión arquitectónica nueva | PAUSA — proponer ADR en MEMORY.md, esperar aprobación |
| Test falla por dependencia del otro dev | Crear mock/stub temporal, anotar en PROGRESS.md como bloqueador |
| Conflicto de merge en PROGRESS.md | Conservar AMBAS entradas, resolver manualmente |
| Nuevo patrón no documentado | Agregar a MEMORY.md como PAT-XXX antes de continuar |

---

## 5. Sprint Execution Model

### 5.1 Cadencia

Cada sprint sigue esta secuencia temporal para los 2 devs:

```
Día 1-2:  Dev A implementa Foundations (modelos, schemas, core)
Día 1:    Dev B prepara tests de integración, stubs, conftest
Día 2-3:  Dev B implementa Integration (services, API, tasks)
          (consume los modelos/schemas que Dev A ya commiteó)
Día 3-4:  Ambos: PRs, code review cruzado, fix de issues
Día 4:    Merge a main, integration tests, cerrar sprint
```

**Overlap:** Dev B puede empezar su slice en cuanto Dev A pushee los archivos de `models/` y `schemas/`. No necesita esperar al PR completo. Dev A pushea incrementalmente.

### 5.2 Dependencias entre devs

```
Dev A: models/ ──→ schemas/ ──→ core/ ──→ push
                                              │
Dev B: conftest/ + stubs ─────────────────────┘──→ services/ ──→ api/ ──→ tests/ ──→ push
```

Dev B observa los pushes de Dev A en la branch. Si Dev A no ha pusheado aún, Dev B trabaja con stubs/mocks basados en la spec.

### 5.3 Stubs temporales

Si Dev B necesita un modelo que Dev A aún no ha creado:

```python
# tests/conftest.py — stub temporal basado en la spec
# TODO: Reemplazar por import real cuando Dev A complete models/contact.py
class ContactStub:
    """Stub temporal — ver specs/sprint-03-fastapi.md, Tabla contacts"""
    id: str = "test-uuid"
    client_id: str = "tenant-uuid"
    full_name: str = "Test Contact"
```

El stub se reemplaza por el import real en cuanto Dev A pushee el modelo. Claude debe verificar en cada loop si los stubs ya pueden reemplazarse.

---

## 6. Matriz de Asignación por Sprint

### Sprint 1 — Schema DDL & Arquitectura
| Dev | Tareas | Archivos |
|---|---|---|
| **A** | DDL completo: extensiones, enums, 18 tablas MVP, FKs, constraints, índices (B-tree, GIN, HNSW), RLS policies con FORCE, roles | `supabase/init/init.sql` |
| **A** | Tablas Fase 2 (6 tablas) en archivo separado | `supabase/init/phase2_tables.sql` |
| **B** | Diagrama de arquitectura Mermaid (flujo webhook → respuesta) | `docs/architecture.mermaid` |
| **B** | Test de aislamiento RLS (crear 2 tenants, verificar que no se ven datos cruzados) | `tests/test_rls_isolation.sql` |

### Sprint 2 — Infraestructura Docker
| Dev | Tareas | Archivos |
|---|---|---|
| **A** | docker-compose.yml (16 servicios), Dockerfile multi-stage, .dockerignore | `docker-compose.yml`, `Dockerfile`, `.dockerignore` |
| **A** | Configs de Supabase self-hosted | `supabase/docker/` |
| **B** | Traefik v3 config (entrypoints, middlewares, rate limiting) | `traefik/traefik.yml`, `traefik/dynamic/` |
| **B** | Prometheus config + Grafana provisioning | `prometheus/prometheus.yml`, `grafana/` |
| **B** | Scripts de healthcheck y wait-for-it | `scripts/wait-for-it.sh`, `scripts/healthcheck.sh` |

### Sprint 3 — FastAPI Core & Auth
| Dev | Tareas | Archivos |
|---|---|---|
| **A** | Pydantic Settings (config.py) | `app/core/config.py` |
| **A** | Async SQLAlchemy engine + SET LOCAL helper | `app/core/database.py` |
| **A** | JWT validation + password hashing | `app/core/security.py` |
| **A** | AppException + error handlers | `app/core/exceptions.py` |
| **A** | DI: TenantSession, CurrentUser, require_role | `app/core/dependencies.py` |
| **A** | TenantBaseModel + 18 SQLAlchemy models | `app/models/*.py` |
| **A** | Pydantic v2 schemas para todas las entidades | `app/schemas/*.py` |
| **B** | App factory (main.py) con lifespan | `app/main.py` |
| **B** | TenantContextMiddleware (JWT → SET LOCAL) | `app/middleware/tenant_context.py` |
| **B** | TokenBudgetGuard middleware (placeholder) | `app/middleware/token_budget.py` |
| **B** | Auth endpoints (login, refresh) | `app/api/v1/auth.py` |
| **B** | Health check endpoint | `app/api/internal/health.py` |
| **B** | Test fixtures: async client, tenant session | `tests/conftest.py` |
| **B** | Tests: RLS isolation, auth, security | `tests/unit/test_*.py` |

### Sprint 4 — Webhook Receiver & MessagingProvider
| Dev | Tareas | Archivos |
|---|---|---|
| **A** | MessagingProvider ABC (5 métodos) | `app/services/messaging/base.py` |
| **A** | YCloudProvider (implementación completa) | `app/services/messaging/ycloud.py` |
| **A** | MetaProvider (Instagram DM + Facebook Messenger) | `app/services/messaging/meta.py` |
| **A** | ProviderFactory (resolve por channel + config) | `app/services/messaging/factory.py` |
| **A** | NormalizedMessage schema | `app/schemas/message.py` |
| **B** | Webhook endpoint (POST /webhooks/{provider}/{channel}) — YCloud + Meta | `app/api/v1/webhooks.py` |
| **B** | Meta webhook verification (GET hub.challenge) | `app/api/v1/webhooks.py` |
| **B** | Deduplicación por (channel, external_message_id) | `app/services/dedup.py` |
| **B** | Celery app config + webhook_processor task | `app/tasks/celery_app.py`, `app/tasks/webhook_processor.py` |
| **B** | Tests: webhook idempotency, provider factory, MetaProvider | `tests/unit/test_webhooks.py`, `tests/unit/test_meta_provider.py`, `tests/integration/test_webhook_flow.py` |

### Sprint 5 — Pipeline de Documentos & RAG
| Dev | Tareas | Archivos |
|---|---|---|
| **A** | Document pipeline: upload, extract, chunk, embed | `app/services/document_pipeline.py` |
| **A** | RAG retriever con pre-filtro client_id + cosine threshold | `app/services/rag.py` |
| **A** | OCR con pytesseract para imágenes/PDFs escaneados | `app/services/ocr.py` |
| **B** | Document CRUD endpoints | `app/api/v1/documents.py` |
| **B** | Celery task de ingestion (async processing) | `app/tasks/document_ingestion.py` |
| **B** | Tests: chunking, embedding, retrieval, citation format | `tests/unit/test_rag.py`, `tests/integration/test_document_pipeline.py` |

### Sprint 6 — LangGraph: Grafo de Agentes
| Dev | Tareas | Archivos |
|---|---|---|
| **A** | ConversationState TypedDict | `app/agents/state.py` |
| **A** | build_conversation_graph() — StateGraph completo | `app/agents/graph.py` |
| **A** | Schemas de agent_config | `app/schemas/agent_config.py` |
| **B** | intent_router node (GPT-4o-mini, 7 intents) | `app/agents/nodes/intent_router.py` |
| **B** | rag_query node (strict grounding + citations) | `app/agents/nodes/rag_query.py` |
| **B** | token_budget node (3 umbrales: ok/degraded/exceeded) | `app/agents/nodes/token_budget.py` |
| **B** | respond node (send via MessagingProvider) | `app/agents/nodes/respond.py` |
| **B** | human_handoff node (escalation logic) | `app/agents/nodes/human_handoff.py` |
| **B** | training_approval node (few-shot dynamic) | `app/agents/nodes/training_approval.py` |
| **B** | Celery task: ai_processor (invoca el grafo) | `app/tasks/ai_processor.py` |
| **B** | Tests: intent routing, RAG node, token budget, graph flow | `tests/unit/test_intent_routing.py`, `tests/integration/test_graph_flow.py` |

### Sprint 7 — Agente de Agendamiento & CRM API
| Dev | Tareas | Archivos |
|---|---|---|
| **A** | Google Calendar tools (LangGraph tools) | `app/agents/tools/calendar_tools.py` |
| **A** | Scheduling node (nuevo nodo en el grafo) | `app/agents/nodes/scheduling.py` |
| **A** | Contact unifier service (merge duplicados) | `app/services/contact_unifier.py` |
| **B** | Contacts CRUD endpoints | `app/api/v1/contacts.py` |
| **B** | Conversations CRUD + lifecycle (7 estados) | `app/api/v1/conversations.py` |
| **B** | Auto-close worker (Celery beat) | `app/tasks/auto_close.py` |
| **B** | Tests: scheduling, contact merge, conversation lifecycle | `tests/` |

### Sprint 8 — Observabilidad, Backup & Hardening (HITO MVP)
| Dev | Tareas | Archivos |
|---|---|---|
| **A** | OpenTelemetry instrumentation (FastAPI, Celery, SQLAlchemy) | `app/core/telemetry.py` |
| **A** | Prometheus custom metrics | `app/core/metrics.py` |
| **A** | Grafana dashboards (JSON provisioning) | `grafana/dashboards/*.json` |
| **A** | pgcrypto: cifrado de columnas sensibles | `app/core/encryption.py` |
| **B** | Audit log middleware | `app/middleware/audit.py` |
| **B** | Backup/restore scripts | `scripts/backup.sh`, `scripts/restore_test.sh` |
| **B** | Quick replies CRUD | `app/api/v1/quick_replies.py` |
| **B** | E2E test: webhook → grafo → respuesta | `tests/e2e/test_full_flow.py` |
| **B** | RGPD: endpoint de export/delete de datos personales | `app/api/v1/admin.py` (parcial) |

### Sprints 9-12 — Fase 2: Expansión
| Sprint | Dev A | Dev B |
|---|---|---|
| 9 (Canales adicionales) | TelegramProvider, EmailProvider | WebchatProvider (WebSocket), Whisper integration, tests (Meta movido a Sprint 4) |
| 10 (Templates & Sentimiento) | Template cloning con re-embedding | Sentiment analysis node, CRUD templates |
| 11 (Webhooks salientes & CSAT) | Outgoing webhook engine (HMAC-SHA256) | CSAT surveys post-resolution, retry/auto-disable |
| 12 (Agentes Financiero & Marketing) | Financial agent (DIAN), Marketing agent | CRM scoring, RAG re-ranking cross-encoder |

### Sprints 13-14 — Fase 3: Módulos Avanzados
| Sprint | Dev A | Dev B |
|---|---|---|
| 13 (Canal de Voz & Agente Clínico) | Voice channel (Twilio/Vonage STT/TTS) | Clinical agent (RIPS, CIE-10, CUPS) |
| 14 (Sandbox, Multi-idioma & Feature Flags) | Sandbox mode per tenant, feature flags | Multi-language 6 idiomas (langdetect + GPT), API preferencias UI |

### Sprint 15 — Fase 4: Frontend Foundation & Panel Admin
| Dev | Tareas | Archivos |
|---|---|---|
| **A** | Next.js 14+ project scaffolding, App Router, TypeScript config | `frontend/`, `frontend/next.config.ts`, `frontend/tsconfig.json` |
| **A** | shadcn/ui + Tailwind CSS setup, design tokens | `frontend/tailwind.config.ts`, `frontend/components/ui/` |
| **A** | next-themes (dark/light/system toggle) | `frontend/components/theme-provider.tsx`, `frontend/components/theme-toggle.tsx` |
| **A** | next-intl (6 idiomas: es, en, pt, it, de, fr) | `frontend/i18n/`, `frontend/messages/*.json` |
| **A** | Auth context + Zustand stores | `frontend/stores/`, `frontend/lib/auth.ts` |
| **A** | Supabase Realtime client para conversaciones | `frontend/lib/supabase.ts`, `frontend/hooks/use-realtime.ts` |
| **A** | Dockerfile multi-stage para Next.js (standalone) | `frontend/Dockerfile` |
| **B** | Onboarding UI: multi-step form (`/onboarding`) | `frontend/app/onboarding/` |
| **B** | Admin Dashboard: stats, charts (Recharts), activity feed | `frontend/app/dashboard/` |
| **B** | Conversations view con mensajes real-time | `frontend/app/conversations/` |
| **B** | Business personalization UI (logo, colors, hours) | `frontend/app/settings/` |
| **B** | Super Admin: client management, Celery/Redis dashboard | `frontend/app/admin/` |
| **B** | Responsive layout (mobile-first, Tailwind breakpoints) | `frontend/components/layout/` |
| **B** | Tests: Vitest + React Testing Library | `frontend/__tests__/` |

---

## 7. Quality Gates

### Gate 1: Pre-Commit (automático en cada loop)
- [ ] `ruff check app/` — 0 errores
- [ ] `pytest tests/ -v --tb=short` — 0 fallos
- [ ] Type hints en toda función nueva
- [ ] Docstring Google-style en toda función pública
- [ ] Loguru con `client_id` y `trace_id`

### Gate 2: Pre-PR (antes de abrir el PR)
- [ ] Test suite completo pasa
- [ ] `mypy app/ --ignore-missing-imports` — 0 errores
- [ ] Cobertura de tests > 70% para archivos nuevos
- [ ] PROGRESS.md actualizado
- [ ] MEMORY.md actualizado si hay decisiones nuevas
- [ ] Branch rebased sobre main (sin conflictos)

### Gate 3: PR Review (por el otro dev o su Claude)
- [ ] Cumple spec de `specs/sprint-XX-*.md`
- [ ] SET LOCAL en TODAS las queries (nunca SET)
- [ ] RLS policies verificadas en tablas nuevas
- [ ] Sin sync I/O en contexto async
- [ ] AppException para errores (nunca tracebacks)
- [ ] Sin secretos hardcodeados
- [ ] Tests cubren happy path + 2 edge cases mínimo

### Gate 4: Sprint Milestone (ambos devs)
- [ ] Ambos PRs del sprint mergeados a main
- [ ] Integration tests pasan en main
- [ ] PROGRESS.md muestra sprint como completado
- [ ] Demo funcional (solo para Sprint 8 = MVP)

---

## 8. Comunicación y Coordinación

### Canal primario
- **GitHub Issues**: Para bugs y tareas que emergen durante el sprint
- **GitHub PR comments**: Para discusión de código durante review
- **PROGRESS.md**: Estado compartido entre sesiones de Claude

### Protocolo de sincronización diario
1. **Antes de empezar**: `git pull origin main` en tu feature branch
2. **Cada commit**: Push a tu feature branch (el otro dev puede ver progreso)
3. **Al terminar el día**: Actualizar PROGRESS.md con lo completado
4. **Bloqueadores**: Crear GitHub Issue con label `blocker` e informar al otro dev

### Protocolo de dependencias
Si Dev B necesita algo que Dev A aún no ha completado:

```
1. Verificar PROGRESS.md — ¿Dev A ya lo completó?
   → SI: git pull y usar el código real
   → NO: Continuar al paso 2

2. ¿Puedo crear un stub/mock temporal basado en la spec?
   → SI: Crear stub, marcar con TODO, seguir implementando
   → NO: Crear Issue "BLOCKED: Necesito X de Dev A", trabajar en otra tarea

3. Cuando Dev A complete el módulo:
   → Dev B reemplaza stubs por imports reales
   → Dev B ejecuta tests para verificar integración
```

---

## 9. Manejo de Errores y Recovery

### Si un loop de Claude se queda atascado (3 intentos fallidos)
1. Claude escribe el bug en MEMORY.md como `BUG-XXX`
2. Claude reporta: qué falló, qué intentó, hipótesis restantes
3. Dev decide: fix manual, cambiar approach, o escalar al otro dev

### Si hay conflicto de merge
1. `PROGRESS.md` / `MEMORY.md`: conservar AMBAS entradas, reordenar cronológicamente
2. Código: el dev que hace merge resuelve, priorizando la implementación más completa
3. Tests: si ambos devs crearon tests para lo mismo, conservar ambos (más cobertura)

### Si el otro dev introdujo un bug que rompe tus tests
1. NO fix el código del otro dev (no es tu ownership)
2. Crear Issue con reproducción: test name, error, commit que lo introdujo
3. Si es blocker: revert el commit en tu branch local y seguir trabajando

### Si una decisión arquitectónica nueva contradice MEMORY.md
1. PAUSA — no implementar
2. Proponer nueva ADR en MEMORY.md como `ADR-XXX (PROPUESTA)`
3. Discutir con el otro dev
4. Solo implementar cuando ambos aprueben (cambiar status a `APROBADA`)

---

## 10. Prompt Templates

### Template: Inicio de Sprint (copiar y pegar en Claude)

```
# INICIO DE SPRINT

Soy Dev {A|B} ({Foundations|Integration}).
Sprint activo: {NN} — {nombre del sprint}
Branch: feature/sprint-{NN}-{descripcion}

## Instrucciones

1. Lee en orden: CLAUDE.md → METHODOLOGY.md → PROGRESS.md → MEMORY.md
2. Lee la spec: specs/sprint-{NN}-*.md
3. Consulta la Matriz de Asignación (sección 6 de METHODOLOGY.md) para mis tareas
4. Crea la branch: git checkout -b feature/sprint-{NN}-{descripcion}
5. Ejecuta mis tareas con el protocolo de loop (sección 4.2)
6. Pausa en cada checkpoint para mi revisión

## Reglas
- Solo modifica archivos asignados a mi rol
- Cada commit: tipo(scope): descripción en español
- Actualiza PROGRESS.md después de cada tarea completada
- Si hay decisión nueva → agregar a MEMORY.md
- Si un test falla 3 veces → PAUSA y reportar
```

### Template: Code Review (copiar y pegar en Claude)

```
# CODE REVIEW

Soy Dev {A|B}. Revisa el PR del otro dev.
PR: #{número} — feature/sprint-{NN}-{descripcion}

1. Lee CLAUDE.md para las reglas del proyecto
2. Lee specs/sprint-{NN}-*.md para verificar cumplimiento
3. Haz git diff main...feature/sprint-{NN}-{descripcion}
4. Verifica los Quality Gates de la sección 7 de METHODOLOGY.md
5. Reporta: ✅ Aprobado, 🔄 Cambios requeridos, o ❌ Rechazado
```

### Template: Continuar sesión (después de pausa)

```
# CONTINUACIÓN

Soy Dev {A|B}. Continúa desde donde quedó la sesión anterior.

1. Lee PROGRESS.md para ver el estado actual
2. Lee MEMORY.md para bugs o decisiones nuevas
3. Identifica la siguiente tarea pendiente de mi rol
4. Continúa el loop de implementación
```

---

## 11. Estimación de Tiempo

Con Loop Engineering semi-autónomo para 2 devs:

| Fase | Sprints | Estimación | Notas |
|---|---|---|---|
| MVP Core | 1-2 | 1 semana | Foundation, mayormente secuencial |
| MVP Core | 3-4 | 1 semana | Alta paralelización entre devs |
| MVP Core | 5-6 | 1.5 semanas | RAG y LangGraph son los más complejos |
| MVP Core | 7-8 | 1 semana | Módulos más independientes |
| **Total MVP** | **1-8** | **~4-5 semanas** | **Hito: demo end-to-end** |
| Expansión | 9-12 | 3-4 semanas | Módulos independientes, alta paralelización |
| Avanzados | 13-14 | 2 semanas | Requiere APIs externas (Twilio, etc.) |
| Frontend | 15 | 1.5-2 semanas | Next.js, i18n, theme, responsive, admin panels |
| **Total Proyecto** | **1-15** | **~10-13 semanas** | Con 2 devs + Claude semi-autónomo |

---

## 12. Checklist de Setup Inicial

Antes de empezar el Sprint 1, ambos devs deben:

- [ ] Clonar el repo y verificar la estructura
- [ ] Copiar `.env.example` a `.env` y configurar valores locales
- [ ] Instalar dependencias: `pip install -r requirements.txt`
- [ ] Verificar que `ruff`, `mypy` y `pytest` funcionan
- [ ] Configurar su sesión de Claude con acceso al repo
- [ ] Leer CLAUDE.md + METHODOLOGY.md completos
- [ ] Configurar branch protection en GitHub
- [ ] Crear labels en GitHub: `sprint-1` a `sprint-15`, `blocker`, `bug`, `dev-a`, `dev-b`
- [ ] Crear GitHub Actions CI (copiar el YAML de la sección 3)
- [ ] Instalar git hooks: `bash scripts/install-hooks.sh`
- [ ] Ejecutar pre-flight check: `bash scripts/preflight.sh --dev-{a|b}`

---

## 13. Harness Engineering

El proyecto implementa **Harness Engineering**: infraestructura determinística que envuelve a los agentes no-determinísticos (Claude, LLMs) para garantizar calidad y consistencia.

### Los 6 Pilares

#### Pilar 1: Prompt Harness (Guía de comportamiento)
**Archivos:** `CLAUDE.md`, `METHODOLOGY.md`, `MEMORY.md`, `specs/sprint-*.md`

El "prompt harness" no se refiere al prompt del LLM en la app, sino a las instrucciones que guían a Claude como desarrollador. Cada sesión de Claude recibe instrucciones determinísticas que delimitan qué puede hacer, qué no, y cómo debe hacerlo.

- `CLAUDE.md` define reglas absolutas (nunca violar)
- `METHODOLOGY.md` define el protocolo de trabajo (loop, roles, gates)
- `MEMORY.md` acumula decisiones y bugs para persistir entre sesiones
- `specs/sprint-*.md` definen exactamente qué implementar

#### Pilar 2: State Harness (Estado compartido)
**Archivos:** `PROGRESS.md`, `MEMORY.md`, `contracts/sprint-*.json`

Estado compartido entre sesiones de Claude y entre devs. Cada sesión lee el estado al inicio y lo actualiza al terminar.

- `PROGRESS.md`: cola de tareas compartida (qué está hecho, qué falta)
- `MEMORY.md`: log de decisiones arquitectónicas y bugs conocidos
- `contracts/`: definiciones de interfaces entre Dev A y Dev B

#### Pilar 3: Validation Harness (Tests determinísticos)
**Archivos:** `tests/conftest.py`, `tests/unit/test_rls_isolation.py`, `scripts/smoke-test.sh`

Tests automatizados que validan invariantes del sistema, independientes del LLM.

- **RLS Test Harness:** fixture `rls_harness` que crea 2 tenants y verifica aislamiento completo (SELECT, UPDATE, DELETE bloqueados entre tenants)
- **Contract Smoke Tests:** `scripts/smoke-test.sh` valida que los módulos-contrato son importables y exportan los símbolos esperados
- **Vector Search Isolation:** test específico que verifica pre-filtro por `client_id` en búsquedas de embeddings

#### Pilar 4: Constraint Harness (Prevención automática)
**Archivos:** `scripts/hooks/pre-commit`, `scripts/install-hooks.sh`

Git hooks que detectan violaciones de reglas absolutas ANTES del commit.

Checks bloqueantes (impiden commit):
1. `SET` sin `LOCAL` (violación de compatibilidad con pgBouncer)
2. Secretos hardcodeados (API keys, passwords, private keys)
3. Archivos `.env` en staging

Checks informativos (warning pero no bloquean):
4. Sync I/O en contexto async
5. Tracebacks expuestos (no usar `AppException`)
6. Funciones sin return type hints
7. Ruff lint
8. `PROGRESS.md` no actualizado cuando hay cambios `.py`

#### Pilar 5: Feedback Harness (Aprendizaje de errores)
**Archivos:** `docs/loop-failures/TEMPLATE.md`, `MEMORY.md`

Registro sistemático de fallos en el loop de desarrollo para evitar repetirlos.

- Cada fallo persistente (3 intentos en debug loop) genera un reporte en `docs/loop-failures/`
- Los patrones se escalan a `MEMORY.md` como `PAT-XXX` o `BUG-XXX`
- Los hooks se actualizan si un patrón de error es prevenible automáticamente

#### Pilar 6: Orchestration Harness (Coordinación multi-dev)
**Archivos:** `METHODOLOGY.md` §4-6, `contracts/`, `scripts/preflight.sh`

Coordinación determinística entre 2 sesiones de Claude trabajando en paralelo.

- **Pre-flight check:** valida que el entorno está listo antes de empezar (`scripts/preflight.sh`)
- **Matriz de asignación:** asignación estática de archivos (§6) — zero conflicts by design
- **Contract files:** interfaces explícitas entre devs (`contracts/sprint-*.json`)
- **Stubs temporales:** patrón para desbloquear a Dev B cuando Dev A no ha pusheado
- **Checkpoints:** pausas obligatorias cada 3-5 módulos para revisión humana
- **Quality Gates:** 4 niveles de validación (§7) antes de que código llegue a `main`

### Uso práctico

```
# Al inicio de sesión:
bash scripts/preflight.sh --dev-{a|b}

# Antes de cada commit (automático si hooks instalados):
git commit ...  # pre-commit hook ejecuta Constraint Harness

# Al integrar stubs con código real:
bash scripts/smoke-test.sh [sprint-number]

# Cuando un debug loop falla 3 veces:
cp docs/loop-failures/TEMPLATE.md docs/loop-failures/$(date +%Y-%m-%d)-sprint-XX-descripcion.md
# Llenar el reporte y agregar patrón a MEMORY.md
```
