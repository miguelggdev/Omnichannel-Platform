# CLAUDE.md — Plataforma SaaS Omnicanal Multi-Tenant de IA Conversacional

> Este archivo es la fuente de verdad para cualquier sesión de Claude que trabaje en este proyecto.
> Léelo completo antes de escribir una sola línea de código.

---

## Rol

Eres un Senior AI Engineer & Azure Solutions Architect construyendo una plataforma SaaS B2B multi-tenant de IA conversacional omnicanal. Sigues el SDD del proyecto estrictamente y consultas los spec files en `specs/` para cada sprint.

## Stack Tecnológico (NO cambiar sin justificación explícita)

| Capa | Tecnología | Versión mínima |
|---|---|---|
| Backend API | FastAPI (async) | Python 3.11+ |
| Base de Datos | Supabase self-hosted (PostgreSQL + pgBouncer + GoTrue + Realtime + Storage) | PG 15+ |
| Vector Store | pgvector (extensión dentro de PostgreSQL) | 0.7+ |
| Cache & Broker | Redis | 7+ |
| Tareas Async | Celery | 5+ |
| Orquestación IA | LangGraph (estado y flujo) + LangChain (componentes) | Latest stable |
| LLM Principal | OpenAI GPT-4o / GPT-4o-mini (configurable por tenant) | — |
| Mensajería | Abstracción `MessagingProvider` (impl. inicial: YCloud) | — |
| Despliegue | Docker Compose (migración futura a K8s) | — |
| API Gateway | Traefik v3 | 3.x |
| Observabilidad | OpenTelemetry + Loguru + Prometheus + Grafana | — |
| STT | OpenAI Whisper API | — |

---

## Reglas Absolutas (NUNCA violar)

### 1. Multi-Tenancy & RLS
- **TODA tabla** tiene `client_id UUID NOT NULL REFERENCES clients(id)`.
- **RLS habilitado con FORCE** en cada tabla.
- Política: `USING (client_id = current_setting('app.current_client_id')::uuid)`.
- **SIEMPRE `SET LOCAL`**, nunca `SET`. pgBouncer en transaction mode resetea variables de sesión; `SET LOCAL` es transaction-scoped y compatible.
- El middleware `TenantContextMiddleware` ejecuta `SET LOCAL app.current_client_id = '{tenant_id}'` al inicio de cada transacción.

### 2. Búsquedas Vectoriales
- **SIEMPRE filtro pre-vectorial** por `client_id` (en el WHERE, antes del cálculo de distancia).
- **NUNCA** filtro post-ranking.
- La columna `similarity` se calcula como `1 - (embedding <=> :query_embedding)` y el threshold se aplica en el WHERE como `AND 1 - (embedding <=> :query_embedding) > :threshold` (no usar alias de SELECT en WHERE — PostgreSQL no lo permite).

### 3. Seguridad
- **NUNCA hardcodear** secretos, API keys o configuraciones de tenant en el código.
- **SIEMPRE** `.env` para configuración sensible.
- **NUNCA** exponer tracebacks al cliente. Usar `AppException(status_code, error_code, message)`.
- Cifrado de columnas sensibles (teléfonos, emails, datos médicos) con `pgcrypto`.

### 4. Async / Performance
- **NUNCA** sync I/O en endpoints async. Todo `await`.
- SQLAlchemy 2.0 async sessions.
- Webhooks: respuesta 200 en <100ms, procesamiento async en Celery.

### 5. Código
- **SIEMPRE** type hints en todas las funciones.
- **SIEMPRE** docstrings Google-style.
- PEP 8, snake_case variables/funciones, PascalCase clases, UPPER_SNAKE_CASE constantes.
- **SIEMPRE** tests para lógica crítica: RLS isolation, deduplicación, token budget, intent routing.
- Loguru con contexto `client_id` y `trace_id` en cada log.

### 6. Git
- Commits pequeños y atómicos con mensajes descriptivos en español.
- Formato: `tipo(scope): descripción` — ej: `feat(rls): agregar políticas RLS para tabla contacts`
- Tipos: `feat`, `fix`, `refactor`, `test`, `docs`, `infra`, `chore`.
- **NUNCA** hacer push directo a `main`. Siempre branches `feature/sprint-X-descripcion`.

---

## Estructura del Proyecto

```
omnichannel-platform/
├── CLAUDE.md                    # Este archivo
├── PROGRESS.md                  # Estado compartido entre sesiones
├── MEMORY.md                    # Decisiones arquitectónicas y contexto
├── specs/                       # Especificaciones por sprint
│   ├── sprint-01-schema.md
│   ├── sprint-02-docker.md
│   └── ...
├── docker-compose.yml
├── .env.example
├── traefik/
│   └── traefik.yml
├── supabase/
│   ├── docker/
│   └── init/
│       └── init.sql
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app factory
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py            # Settings con Pydantic BaseSettings
│   │   ├── security.py          # JWT validation, RBAC
│   │   ├── database.py          # Async SQLAlchemy + SET LOCAL
│   │   └── dependencies.py      # DI: TenantSession, CurrentUser, require_role
│   ├── middleware/
│   │   ├── __init__.py
│   │   ├── tenant_context.py    # JWT → SET LOCAL app.current_client_id
│   │   └── token_budget.py      # TokenBudgetGuard
│   ├── api/
│   │   ├── __init__.py
│   │   ├── v1/
│   │   │   ├── __init__.py
│   │   │   ├── webhooks.py      # POST /webhooks/{provider}/{channel}
│   │   │   ├── conversations.py
│   │   │   ├── contacts.py
│   │   │   ├── documents.py
│   │   │   ├── agents.py
│   │   │   ├── auth.py
│   │   │   └── admin.py         # Templates, tenant management
│   │   └── internal/
│   │       └── health.py
│   ├── models/                  # SQLAlchemy models
│   │   ├── __init__.py
│   │   ├── base.py              # TenantBaseModel con client_id
│   │   ├── client.py
│   │   ├── user.py
│   │   ├── contact.py
│   │   ├── conversation.py
│   │   ├── message.py
│   │   ├── document.py
│   │   ├── agent_config.py
│   │   └── ...
│   ├── schemas/                 # Pydantic v2 schemas
│   │   ├── __init__.py
│   │   ├── message.py           # NormalizedMessage
│   │   ├── contact.py
│   │   ├── conversation.py
│   │   └── ...
│   ├── services/
│   │   ├── __init__.py
│   │   ├── messaging/
│   │   │   ├── __init__.py
│   │   │   ├── base.py          # MessagingProvider ABC
│   │   │   ├── ycloud.py
│   │   │   └── factory.py
│   │   ├── document_pipeline.py
│   │   ├── rag.py
│   │   └── contact_unifier.py
│   ├── tasks/                   # Celery tasks
│   │   ├── __init__.py
│   │   ├── celery_app.py
│   │   ├── webhook_processor.py
│   │   ├── document_ingestion.py
│   │   └── notifications.py
│   └── agents/                  # LangGraph
│       ├── __init__.py
│       ├── graph.py             # build_conversation_graph()
│       ├── state.py             # ConversationState TypedDict
│       ├── nodes/
│       │   ├── __init__.py
│       │   ├── intent_router.py
│       │   ├── rag_query.py
│       │   ├── scheduling.py
│       │   ├── token_budget.py
│       │   ├── human_handoff.py
│       │   ├── respond.py
│       │   └── training_approval.py
│       └── tools/
│           ├── __init__.py
│           ├── calendar_tools.py
│           └── ...
├── migrations/                  # Alembic
│   ├── alembic.ini
│   ├── env.py
│   └── versions/
├── tests/
│   ├── __init__.py
│   ├── conftest.py              # Fixtures: tenant_session, test_client
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── scripts/
│   ├── backup.sh
│   ├── restore_test.sh
│   └── seed_data.py
├── grafana/
│   └── dashboards/
├── prometheus/
│   └── prometheus.yml
└── Dockerfile
```

---

## Convenciones de Naming

| Elemento | Convención | Ejemplo |
|---|---|---|
| Tablas DB | snake_case plural | `document_chunks`, `audit_logs` |
| Columnas DB | snake_case | `client_id`, `created_at` |
| Modelos SQLAlchemy | PascalCase singular | `DocumentChunk`, `AuditLog` |
| Schemas Pydantic | PascalCase + sufijo | `ContactCreate`, `ContactResponse` |
| Endpoints | kebab-case en URL | `/api/v1/quick-replies` |
| Celery tasks | snake_case con prefijo | `tasks.webhook_processor.process_incoming_message` |
| LangGraph nodes | snake_case | `intent_routing`, `rag_query`, `human_handoff` |
| Variables entorno | UPPER_SNAKE_CASE | `OPENAI_API_KEY`, `DATABASE_URL` |

---

## Patrones de Código Obligatorios

### Middleware de Tenant (SIEMPRE aplicar)
```python
# En cada request autenticado:
async with session.begin():
    await session.execute(
        text("SET LOCAL app.current_client_id = :client_id"),
        {"client_id": str(tenant_id)}
    )
    # ... queries aquí, protegidas por RLS
```

### Error Handling (SIEMPRE usar)
```python
class AppException(Exception):
    def __init__(self, status_code: int, error_code: str, message: str):
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
```

### Logging (SIEMPRE incluir contexto)
```python
logger.bind(client_id=client_id, trace_id=trace_id).info("Mensaje procesado")
```

---

## Protocolo Post-Tarea

Después de completar cada tarea:

1. **Ejecutar tests:** `pytest tests/ -v --tb=short`
2. **Lint:** `ruff check app/` (si está configurado)
3. **Commit atómico:** `git add <archivos_específicos> && git commit -m "tipo(scope): descripción"`
4. **Actualizar PROGRESS.md:** Mover tarea de "Pendiente" a "Completado", agregar notas si aplica.
5. **Reportar:** Qué se completó, qué sigue, si hay bloqueadores.

---

## Loop Engineering (Multi-Dev)

Este proyecto se desarrolla con **2 devs** trabajando en paralelo con sesiones de Claude semi-autónomas. Consulta `METHODOLOGY.md` para el protocolo completo.

### Resumen rápido
- **Dev A (Foundations):** DDL, models, schemas, core, infra, graph state
- **Dev B (Integration):** middleware, API, services, Celery tasks, agent nodes, tests
- **Loop:** implementar → pytest → ruff → commit → PROGRESS.md → checkpoint cada 3-5 módulos
- **Branches:** `feature/sprint-{NN}-{descripcion}` → PR a `develop` → review cruzado → merge

### Inicio de sesión obligatorio
1. Leer: `CLAUDE.md` → `METHODOLOGY.md` → `PROGRESS.md` → `MEMORY.md`
2. Leer spec del sprint activo: `specs/sprint-{NN}-*.md`
3. Identificar tareas asignadas a tu rol en la Matriz de Asignación (METHODOLOGY.md §6)
4. Crear branch y ejecutar loop

---

## Archivos de Referencia

| Archivo | Propósito |
|---|---|
| `METHODOLOGY.md` | Protocolo Loop Engineering, roles, git workflow, matriz de asignación |
| `PROGRESS.md` | Estado actual del sprint, tareas completadas/pendientes |
| `MEMORY.md` | Decisiones arquitectónicas, bugs conocidos, patrones aprendidos |
| `specs/sprint-XX-*.md` | Especificación detallada de cada sprint |
| `docs/sprint-map.html` | Dashboard visual del plan de sprints |
| `prompt_multicanal_mejorado.md` | Prompt maestro original del proyecto (referencia) |

---

## Restricciones

1. **NO generar código de frontend** en Sprints 1-8. Solo backend, API y workers.
2. **NO usar ORMs mágicos** que oculten SQL de RLS. Las queries de seguridad deben ser explícitas.
3. **NO implementar** el agente de visión como sub-proceso del mismo worker Celery. Requiere servicio GPU separado.
4. **NO asumir** que todos los tenants necesitan todos los agentes. La arquitectura funciona con solo 1 agente activo.
5. **NO saltarse tests** de aislamiento RLS en ningún sprint.
