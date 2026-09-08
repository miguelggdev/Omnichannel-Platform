# Sprint 2 — Infraestructura Docker

## Objetivo
Todos los servicios del sistema levantados, comunicados y saludables con Docker Compose. Al finalizar este sprint, un solo comando `docker-compose up -d` debe levantar toda la infraestructura necesaria para desarrollo.

> **ADR-020**: PostgreSQL, Auth, Storage y Realtime son provistos por **Supabase Cloud**.
> No se levantan contenedores locales para estos componentes.
> La app se conecta via el Transaction Pooler de Supabase Cloud (Supavisor, puerto 6543).

## Prerequisitos
- Sprint 1 completado: esquema DDL ejecutable y validado en Supabase Cloud
- Docker Engine 24+ y Docker Compose v2 instalados
- Puertos disponibles: 80, 443, 6379, 8080, 3000, 9090
- Proyecto de Supabase Cloud configurado con API keys y connection strings

## Archivos a Crear
- `docker-compose.yml` — Orquestacion de 12 servicios
- `.env.example` — Variables documentadas agrupadas por servicio
- `traefik/traefik.yml` — Configuracion estatica de Traefik v3
- `traefik/dynamic/middlewares.yml` — Configuracion dinamica de middlewares
- `traefik/dynamic/tls.yml` — Configuracion TLS (dev con certificados autofirmados)
- `Dockerfile` — Multi-stage para la aplicacion FastAPI
- `app/tasks/celery_config.py` — Configuracion de 6 colas y routing Celery
- `scripts/wait-for-it.sh` — Script de espera para dependencias
- `.dockerignore` — Exclusiones del contexto Docker

## Tareas Detalladas

### 1. Docker Compose — 12 servicios

> **Nota ADR-020**: Los servicios supabase-db, supabase-auth, supabase-storage,
> supabase-realtime y pgbouncer ya **NO** se levantan localmente. PostgreSQL, Auth,
> Storage y Realtime los provee Supabase Cloud. El connection pooling lo maneja
> Supavisor (transaction pooler de Supabase Cloud, puerto 6543). DATABASE_URL apunta
> directamente al pooler de Supabase Cloud.

Definir todos los servicios en `docker-compose.yml`:

#### 1.1 `traefik` — API Gateway
```yaml
traefik:
  image: traefik:v3.1
  ports:
    - "80:80"
    - "443:443"
    - "8080:8080"  # Dashboard
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock:ro
    - ./traefik/traefik.yml:/etc/traefik/traefik.yml:ro
    - ./traefik/dynamic/:/etc/traefik/dynamic/:ro
  networks:
    - frontend
    - backend
  healthcheck:
    test: ["CMD", "traefik", "healthcheck"]
    interval: 30s
    timeout: 10s
    retries: 3
```

#### 1.2 `api` — FastAPI (2 replicas)
```yaml
api:
  build:
    context: .
    dockerfile: Dockerfile
    target: runner
  deploy:
    replicas: 2
  environment:
    - DATABASE_URL=${DATABASE_URL}
    - REDIS_URL=${REDIS_URL}
    - SUPABASE_URL=${SUPABASE_URL}
    - SUPABASE_PUBLISHABLE_KEY=${SUPABASE_PUBLISHABLE_KEY}
    - SUPABASE_SECRET_KEY=${SUPABASE_SECRET_KEY}
  depends_on:
    redis:
      condition: service_healthy
  labels:
    - "traefik.enable=true"
    - "traefik.http.routers.api.rule=Host(`api.localhost`)"
    - "traefik.http.services.api.loadbalancer.server.port=8000"
  networks:
    - backend
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8000/internal/health"]
    interval: 30s
    timeout: 10s
    retries: 3
```

> **Cambio ADR-020**: `depends_on` solo incluye `redis`. La BD es externa (Supabase Cloud)
> y no hay pgBouncer local. `DATABASE_URL` apunta al Transaction Pooler de Supabase Cloud.
> Las keys usan el formato nuevo: `SUPABASE_PUBLISHABLE_KEY` y `SUPABASE_SECRET_KEY`.

#### 1.3 `redis` — Cache + Broker Celery
```yaml
redis:
  image: redis:7.4-alpine
  command: >
    redis-server
    --appendonly yes
    --maxmemory 256mb
    --maxmemory-policy allkeys-lru
    --requirepass ${REDIS_PASSWORD}
  volumes:
    - redis_data:/data
  ports:
    - "6379:6379"
  networks:
    - backend
  healthcheck:
    test: ["CMD", "redis-cli", "-a", "${REDIS_PASSWORD}", "ping"]
    interval: 30s
    timeout: 10s
    retries: 3
```

#### 1.4-1.8 Workers Celery (5 colas originales)

Cada worker es un servicio separado con su propia cola y concurrencia:

```yaml
celery-webhooks:
  build:
    context: .
    dockerfile: Dockerfile
    target: runner
  command: >
    celery -A app.tasks.celery_config worker
    -Q webhooks
    -c ${CELERY_WEBHOOK_CONCURRENCY:-4}
    --loglevel=${LOG_LEVEL:-info}
    -n webhooks@%h
  environment:
    - DATABASE_URL=${DATABASE_URL}
    - REDIS_URL=${REDIS_URL}
    - OPENAI_API_KEY=${OPENAI_API_KEY}
  depends_on:
    redis:
      condition: service_healthy
  networks:
    - backend
  healthcheck:
    test: ["CMD-SHELL", "celery -A app.tasks.celery_config inspect ping -d webhooks@$$HOSTNAME"]
    interval: 60s
    timeout: 30s
    retries: 3

celery-ai:
  # Igual estructura, command: ... -Q ai_inference -c 2 -n ai@%h
  # NOTA: Este worker puede necesitar mas memoria por modelo cargado

celery-documents:
  # command: ... -Q documents -c 2 -n documents@%h
  # NOTA: Necesita tesseract-ocr instalado en la imagen

celery-notifications:
  # command: ... -Q notifications -c 2 -n notifications@%h

celery-bulk:
  # command: ... -Q bulk -c 1 -n bulk@%h
  # NOTA: Concurrencia 1 para operaciones secuenciales pesadas
```

> **Cambio ADR-020**: Todos los workers usan `DATABASE_URL` (que apunta al pooler de
> Supabase Cloud). Ya no existe `PGBOUNCER_URL`. `depends_on` solo incluye `redis`.

#### 1.9 `celery-lead-enrichment` — Enriquecimiento de Leads (ADR-022)
```yaml
celery-lead-enrichment:
  build:
    context: .
    dockerfile: Dockerfile
    target: runner
  command: >
    celery -A app.tasks.celery_config worker
    -Q lead_enrichment
    -c ${CELERY_ENRICHMENT_CONCURRENCY:-2}
    --loglevel=${LOG_LEVEL:-info}
    -n lead-enrichment@%h
  environment:
    - DATABASE_URL=${DATABASE_URL}
    - REDIS_URL=${REDIS_URL}
    - OPENAI_API_KEY=${OPENAI_API_KEY}
    - CLEARBIT_API_KEY=${CLEARBIT_API_KEY}
    - HUNTER_API_KEY=${HUNTER_API_KEY}
    - VAPI_API_KEY=${VAPI_API_KEY}
    - VAPI_PHONE_NUMBER=${VAPI_PHONE_NUMBER}
    - BLAND_AI_API_KEY=${BLAND_AI_API_KEY}
    - BLAND_AI_PHONE_NUMBER=${BLAND_AI_PHONE_NUMBER}
    - ENCRYPTION_KEY=${ENCRYPTION_KEY}
  depends_on:
    redis:
      condition: service_healthy
  networks:
    - backend
  healthcheck:
    test: ["CMD-SHELL", "celery -A app.tasks.celery_config inspect ping -d lead-enrichment@$$HOSTNAME"]
    interval: 60s
    timeout: 30s
    retries: 3
```

> **Nuevo (ADR-022)**: Worker dedicado al pipeline de enriquecimiento asincrono de leads.
> Concurrencia 2: llamadas I/O-bound a APIs externas (Clearbit, Hunter, Vapi, Bland.ai).

#### 1.10 `celery-beat` — Scheduler
```yaml
celery-beat:
  build:
    context: .
    dockerfile: Dockerfile
    target: runner
  command: >
    celery -A app.tasks.celery_config beat
    --loglevel=${LOG_LEVEL:-info}
    --schedule=/tmp/celerybeat-schedule
  environment:
    - DATABASE_URL=${DATABASE_URL}
    - REDIS_URL=${REDIS_URL}
    - CELERY_BROKER_URL=${CELERY_BROKER_URL}
    - CELERY_RESULT_BACKEND=${CELERY_RESULT_BACKEND}
    - LOG_LEVEL=${LOG_LEVEL}
  depends_on:
    redis:
      condition: service_healthy
  networks:
    - backend
```

#### 1.11 `prometheus` — Metricas
```yaml
prometheus:
  image: prom/prometheus:v2.54.1
  volumes:
    - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
    - prometheus_data:/prometheus
  ports:
    - "9090:9090"
  networks:
    - backend
  healthcheck:
    test: ["CMD", "wget", "--no-verbose", "--tries=1", "--spider", "http://localhost:9090/-/healthy"]
    interval: 30s
    timeout: 10s
    retries: 3
```

#### 1.12 `grafana` — Dashboards
```yaml
grafana:
  image: grafana/grafana:11.2.0
  environment:
    GF_SECURITY_ADMIN_USER: ${GF_SECURITY_ADMIN_USER:-admin}
    GF_SECURITY_ADMIN_PASSWORD: ${GF_SECURITY_ADMIN_PASSWORD}
  volumes:
    - grafana_data:/var/lib/grafana
  ports:
    - "3000:3000"
  depends_on:
    prometheus:
      condition: service_healthy
  networks:
    - backend
  healthcheck:
    test: ["CMD", "wget", "--no-verbose", "--tries=1", "--spider", "http://localhost:3000/api/health"]
    interval: 30s
    timeout: 10s
    retries: 3
```

#### Redes y Volumenes
```yaml
networks:
  frontend:
    driver: bridge
  backend:
    driver: bridge

volumes:
  redis_data:
    driver: local
  prometheus_data:
    driver: local
  grafana_data:
    driver: local
```

> **Cambio ADR-020**: Se eliminaron `postgres_data` y `storage_data`. PostgreSQL y Storage
> son servicios gestionados por Supabase Cloud; no hay datos locales que persistir.

### 2. Health Checks

Cada servicio DEBE tener un healthcheck con los parametros estandar:
- `interval: 30s`
- `timeout: 10s`
- `retries: 3`
- `start_period: 40s` (para servicios que tardan en iniciar)

Todos los `depends_on` deben usar `condition: service_healthy` para garantizar el orden de inicio correcto.

### 3. Traefik v3 — Configuracion Estatica

Archivo `traefik/traefik.yml`:

```yaml
api:
  dashboard: true
  insecure: true  # Solo para desarrollo

entryPoints:
  web:
    address: ":80"
    http:
      redirections:
        entryPoint:
          to: websecure
          scheme: https
  websecure:
    address: ":443"

providers:
  docker:
    exposedByDefault: false
    network: backend
  file:
    directory: /etc/traefik/dynamic/
    watch: true

log:
  level: INFO

accessLog:
  filePath: "/var/log/traefik/access.log"
  format: json
```

### 4. Traefik v3 — Configuracion Dinamica

Archivo `traefik/dynamic/middlewares.yml`:

```yaml
http:
  middlewares:
    rate-limit:
      rateLimit:
        average: 100
        burst: 200
        period: 1s

    security-headers:
      headers:
        browserXssFilter: true
        contentTypeNosniff: true
        frameDeny: true
        stsIncludeSubdomains: true
        stsPreload: true
        stsSeconds: 31536000
        customFrameOptionsValue: "SAMEORIGIN"
        referrerPolicy: "strict-origin-when-cross-origin"

    compress:
      compress:
        excludedContentTypes:
          - "text/event-stream"
```

Archivo `traefik/dynamic/tls.yml` (desarrollo con certificados autofirmados):
```yaml
tls:
  stores:
    default:
      defaultGeneratedCert:
        resolver: default
        domain:
          main: "localhost"
          sans:
            - "*.localhost"
```

### 5. Dockerfile Multi-Stage

```dockerfile
# ============ BUILDER ============
FROM python:3.12-slim AS builder

WORKDIR /build

# Instalar dependencias del sistema para compilacion
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ============ RUNNER ============
FROM python:3.12-slim AS runner

WORKDIR /app

# Instalar dependencias de runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    tesseract-ocr \
    tesseract-ocr-spa \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copiar dependencias instaladas
COPY --from=builder /install /usr/local

# Crear usuario no-root
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

# Copiar codigo de la aplicacion
COPY ./app ./app
COPY ./migrations ./migrations
COPY ./alembic.ini .

# Cambiar permisos y usuario
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=40s \
    CMD curl -f http://localhost:8000/internal/health || exit 1

# Uvicorn con app factory pattern
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
```

Notas del Dockerfile:
- `tesseract-ocr` y `tesseract-ocr-spa` se instalan para el pipeline de OCR del Sprint 5
- El usuario `appuser` no tiene privilegios de root
- El HEALTHCHECK interno verifica el endpoint `/internal/health`
- `--factory` permite usar el patron app factory de FastAPI

### 6. Celery Config

Archivo `app/tasks/celery_config.py` — **6 colas** (incluyendo `lead_enrichment`):

```python
from celery import Celery
from kombu import Queue, Exchange
import os

celery_app = Celery("omnichannel")

celery_app.conf.update(
    broker_url=os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"),
    result_backend=os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/1"),

    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,

    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_retry_delay=60,
    task_max_retries=3,
    result_expires=3600,

    # 6 colas especializadas
    task_queues=(
        Queue("webhooks", Exchange("webhooks", type="direct"), routing_key="webhooks"),
        Queue("ai_inference", Exchange("ai_inference", type="direct"), routing_key="ai_inference"),
        Queue("documents", Exchange("documents", type="direct"), routing_key="documents"),
        Queue("notifications", Exchange("notifications", type="direct"), routing_key="notifications"),
        Queue("bulk", Exchange("bulk", type="direct"), routing_key="bulk"),
        Queue("lead_enrichment", Exchange("lead_enrichment", type="direct"), routing_key="lead_enrichment"),
    ),

    task_routes={
        "app.tasks.webhook_*": {"queue": "webhooks"},
        "app.tasks.ai_*": {"queue": "ai_inference"},
        "app.tasks.document_*": {"queue": "documents"},
        "app.tasks.notification_*": {"queue": "notifications"},
        "app.tasks.bulk_*": {"queue": "bulk"},
        "app.tasks.enrichment_*": {"queue": "lead_enrichment"},
    },

    task_default_queue="webhooks",

    beat_schedule={
        "auto-close-conversations": {
            "task": "app.tasks.bulk_auto_close_conversations",
            "schedule": 900.0,
            "options": {"queue": "bulk"},
        },
        "check-token-budgets": {
            "task": "app.tasks.notification_check_token_budgets",
            "schedule": 3600.0,
            "options": {"queue": "notifications"},
        },
        "recalculate-lead-scores": {
            "task": "app.tasks.enrichment_recalculate_scores",
            "schedule": 1800.0,
            "options": {"queue": "lead_enrichment"},
        },
        "check-stale-leads": {
            "task": "app.tasks.enrichment_check_stale_leads",
            "schedule": 3600.0,
            "options": {"queue": "lead_enrichment"},
        },
    },
)
```

Notas de la configuracion:
- `task_acks_late=True`: el worker confirma la tarea DESPUES de ejecutarla, no antes. Si el worker muere, la tarea se re-encola.
- `task_reject_on_worker_lost=True`: si el worker se pierde, la tarea se rechaza y re-encola.
- `worker_prefetch_multiplier=1`: cada worker toma solo 1 tarea a la vez, importante para tareas largas de IA.
- El routing usa patrones glob: `webhook_*` captura todas las tareas que empiecen con ese prefijo.
- `enrichment_*` enruta tareas de enriquecimiento de leads a la cola `lead_enrichment` (ADR-022).

### 7. .dockerignore

```
.git
.gitignore
.env
.env.*
!.env.example
__pycache__
*.pyc
*.pyo
.pytest_cache
.mypy_cache
.ruff_cache
node_modules
.vscode
.idea
*.md
docs/
tests/
docker-compose*.yml
Makefile
```

## Criterios de Aceptacion
- [ ] `docker-compose up -d` levanta todos los 12 servicios sin errores
- [ ] Todos los servicios alcanzan estado `healthy` en menos de 2 minutos
- [ ] El endpoint `/internal/health` del servicio `api` responde HTTP 200
- [ ] `DATABASE_URL` apunta al Transaction Pooler de Supabase Cloud (Supavisor, puerto 6543)
- [ ] Workers Celery se registran con sus 6 colas respectivas (verificar con `celery inspect active_queues`)
- [ ] Redis es accesible tanto por `api` como por los workers Celery en la red `backend`
- [ ] Traefik enruta correctamente al servicio `api` (verificar dashboard en localhost:8080)
- [ ] Grafana accesible en localhost:3000 con credenciales del .env
- [ ] Las 2 replicas del servicio `api` reciben trafico via load balancer de Traefik
- [ ] El worker `celery-lead-enrichment` se registra con la cola `lead_enrichment`

## Notas Tecnicas

### Supavisor (Transaction Pooler de Supabase Cloud) — ADR-020
Supabase Cloud provee Supavisor como transaction pooler (puerto 6543), reemplazando
al pgBouncer self-hosted. El comportamiento es equivalente para nuestra app:
- Transaction mode: cada transaccion puede usar una conexion diferente del pool
- `SET LOCAL` tiene scope solo dentro de la transaccion actual (compatible con RLS multi-tenant)
- `DATABASE_URL` apunta siempre al pooler (puerto 6543), nunca conexion directa
- `DATABASE_URL_DIRECT` (puerto 5432) se usa SOLO para migraciones de Alembic
- Los workers Celery y la API usan `DATABASE_URL` (via Supavisor)

### Redis: Broker y Backend
Redis se usa como:
- DB 0: Broker de Celery (cola de tareas)
- DB 1: Backend de resultados Celery
- DB 2: Cache de la aplicacion (webhook dedup, token budgets)

Separar en databases distintos evita colisiones de keys.

### Volumenes Persistentes
Los volumenes nombrados garantizan persistencia:
- `redis_data`: datos de Redis (AOF habilitado)
- `grafana_data`: dashboards y configuracion de Grafana
- `prometheus_data`: metricas historicas

> **Cambio ADR-020**: `postgres_data` y `storage_data` fueron eliminados.
> PostgreSQL y Storage son gestionados por Supabase Cloud.

### Orden de Inicio
El grafo de dependencias es:
```
redis → api
redis → celery-webhooks
redis → celery-ai
redis → celery-documents
redis → celery-notifications
redis → celery-bulk
redis → celery-lead-enrichment
redis → celery-beat
prometheus → grafana
```

> **Cambio ADR-020**: El grafo es mas simple. Ya no hay cadena
> `supabase-db → pgbouncer → api/workers`. Solo Redis es dependencia local.

## Dependencias para Sprint 3
- El servicio `api` debe estar accesible via Traefik en `api.localhost` (o la URL configurada)
- `DATABASE_URL` debe apuntar correctamente al Transaction Pooler de Supabase Cloud
- Redis debe ser accesible por `api` y todos los workers Celery en la red `backend`
- El endpoint `/internal/health` debe existir como placeholder (puede retornar `{"status": "ok"}`)
- La imagen Docker debe tener `tesseract-ocr` instalado para el Sprint 5
