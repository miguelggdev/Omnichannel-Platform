# Plataforma SaaS Omnicanal Multi-Tenant de IA Conversacional

Plataforma B2B que permite a empresas gestionar comunicaciones con sus clientes a través de múltiples canales (WhatsApp, Telegram, Instagram, Email, Webchat, Voz) con agentes de IA conversacional, RAG sobre base de conocimiento, agendamiento automático y CRM integrado.

## Stack Tecnológico

| Capa | Tecnología |
|---|---|
| Backend API | FastAPI (Python 3.11+, async) |
| Base de Datos | Supabase self-hosted (PostgreSQL 15 + pgvector 0.7+) |
| Cache & Broker | Redis 7+ |
| Tareas Async | Celery 5+ |
| Orquestación IA | LangGraph + LangChain |
| LLM | OpenAI GPT-4o / GPT-4o-mini |
| Mensajería | Abstracción MessagingProvider (YCloud, Telegram, Meta, etc.) |
| API Gateway | Traefik v3 |
| Observabilidad | OpenTelemetry + Loguru + Prometheus + Grafana |
| Despliegue | Docker Compose |

## Arquitectura

- **Multi-Tenancy**: Aislamiento por Row-Level Security (RLS) en PostgreSQL con `SET LOCAL` para compatibilidad con pgBouncer en transaction mode.
- **RAG**: Pipeline de documentos con chunking, embeddings (pgvector) y retrieval con strict grounding.
- **Agentes IA**: Grafo LangGraph con nodos especializados (intent routing, RAG, agendamiento, handoff humano, control de presupuesto).
- **Webhooks**: Recepción idempotente con deduplicación y procesamiento async en Celery.

## Inicio Rápido

```bash
# 1. Clonar el repositorio
git clone <repo-url>
cd omnichannel-platform

# 2. Copiar y configurar variables de entorno
cp .env.example .env
# Editar .env con tus valores

# 3. Levantar servicios
docker-compose up -d

# 4. Verificar
curl http://localhost/internal/health
```

## Estructura del Proyecto

```
├── CLAUDE.md            # Instrucciones para sesiones de Claude
├── PROGRESS.md          # Estado actual del sprint
├── MEMORY.md            # Decisiones arquitectónicas
├── specs/               # Especificaciones por sprint (14 sprints)
├── supabase/init/       # DDL y scripts de inicialización
├── app/                 # Código de la aplicación FastAPI
│   ├── core/            # Config, security, database
│   ├── middleware/       # Tenant context, token budget
│   ├── api/v1/          # Endpoints REST
│   ├── models/          # SQLAlchemy models
│   ├── schemas/         # Pydantic v2 schemas
│   ├── services/        # Lógica de negocio
│   ├── tasks/           # Celery tasks
│   └── agents/          # LangGraph (grafo, nodos, tools)
├── tests/               # Unit, integration, e2e
├── scripts/             # Backup, restore, seed
├── traefik/             # Config de API Gateway
├── grafana/             # Dashboards
├── prometheus/          # Config de métricas
└── docker-compose.yml   # Orquestación
```

## Plan de Desarrollo

El proyecto se desarrolla en 14 sprints organizados en 3 fases:

- **Fase 1 (Sprints 1-8)**: MVP Core — Schema, Docker, FastAPI, Webhooks, RAG, LangGraph, CRM, Observabilidad
- **Fase 2 (Sprints 9-12)**: Expansión — Canales adicionales, Templates, CSAT, Agentes avanzados
- **Fase 3 (Sprints 13-14)**: Módulos Avanzados — Voz, Agente clínico, Sandbox, Multi-idioma

Ver `specs/` para las especificaciones detalladas de cada sprint.

## Documentación

| Archivo | Propósito |
|---|---|
| `CLAUDE.md` | Reglas y patrones para desarrollo con Claude |
| `PROGRESS.md` | Estado actual, tareas completadas/pendientes |
| `MEMORY.md` | ADRs, bugs conocidos, patrones aprendidos |
| `specs/sprint-XX-*.md` | Especificación detallada de cada sprint |

## Convenciones

- **Commits**: `tipo(scope): descripción` en español (feat, fix, refactor, test, docs, infra, chore)
- **Código**: PEP 8, type hints, docstrings Google-style, async everywhere
- **Tests**: pytest con fixtures para tenant isolation
- **Branches**: `feature/sprint-X-descripcion`

## Licencia

Propietario — Todos los derechos reservados.
