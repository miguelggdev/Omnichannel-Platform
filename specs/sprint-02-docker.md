# Sprint 2 — Infraestructura Docker

## Objetivo
Todos los servicios del sistema levantados, comunicados y saludables con Docker Compose. Al finalizar este sprint, un solo comando `docker-compose up -d` debe levantar toda la infraestructura necesaria para desarrollo.

## Prerequisitos
- Sprint 1 completado: `supabase/init/init.sql` ejecutable y validado
- Docker Engine 24+ y Docker Compose v2 instalados
- Puertos disponibles: 80, 443, 5432, 6379, 8080, 3000, 9090

## Archivos a Crear
- `docker-compose.yml` — Orquestacion de 16 servicios
- `.env.example` — Variables documentadas agrupadas por servicio
- `traefik/traefik.yml` — Configuracion estatica de Traefik v3
- `traefik/dynamic/middlewares.yml` — Configuracion dinamica de middlewares
- `traefik/dynamic/tls.yml` — Configuracion TLS (dev con certificados autofirmados)
- `Dockerfile` — Multi-stage para la aplicacion FastAPI
- `app/tasks/celery_config.py` — Configuracion de colas y routing Celery
- `scripts/wait-for-it.sh` — Script de espera para dependencias
- `.dockerignore` — Exclusiones del contexto Docker

## Tareas Detalladas

### 1. Docker Compose — 16 servicios

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
    - PGBOUNCER_URL=${PGBOUNCER_URL}
    - REDIS_URL=${REDIS_URL}
  depends_on:
    pgbouncer:
      condition: service_healthy
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

#### 1.3 `supabase-db` — PostgreSQL 15 + pgvector
```yaml
supabase-db:
  image: supabase/postgres:15.6.1.143
  # Alternativa si pgvector no esta incluido:
  # build desde imagen base con: apt-get install postgresql-15-pgvector
  volumes:
    - postgres_data:/var/lib/postgresql/data
    - ./supabase/init/init.sql:/docker-entrypoint-initdb.d/01-init.sql:ro
  environment:
    POSTGRES_USER: ${POSTGRES_USER}
    POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    POSTGRES_DB: ${POSTGRES_DB}
  ports:
    - "5432:5432"
  networks:
    - backend
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
    interval: 30s
    timeout: 10s
    retries: 5
```

#### 1.4 `supabase-auth` — GoTrue
```yaml
supabase-auth:
  image: supabase/gotrue:v2.158.1
  environment:
    GOTRUE_API_HOST: "0.0.0.0"
    GOTRUE_API_PORT: "9999"
    GOTRUE_DB_DRIVER: postgres
    GOTRUE_DB_DATABASE_URL: ${DATABASE_URL}
    GOTRUE_SITE_URL: ${SITE_URL}
    GOTRUE_JWT_SECRET: ${JWT_SECRET}
    GOTRUE_JWT_EXP: 3600
    GOTRUE_DISABLE_SIGNUP: "false"
  depends_on:
    supabase-db:
      condition: service_healthy
  networks:
    - backend
  healthcheck:
    test: ["CMD", "wget", "--no-verbose", "--tries=1", "--spider", "http://localhost:9999/health"]
    interval: 30s
    timeout: 10s
    retries: 3
```

#### 1.5 `supabase-storage` — Storage de archivos
```yaml
supabase-storage:
  image: supabase/storage-api:v1.11.13
  environment:
    ANON_KEY: ${SUPABASE_ANON_KEY}
    SERVICE_KEY: ${SUPABASE_SERVICE_KEY}
    DATABASE_URL: ${DATABASE_URL}
    STORAGE_BACKEND: file
    FILE_STORAGE_BACKEND_PATH: /var/lib/storage
  volumes:
    - storage_data:/var/lib/storage
  depends_on:
    supabase-db:
      condition: service_healthy
  networks:
    - backend
  healthcheck:
    test: ["CMD", "wget", "--no-verbose", "--tries=1", "--spider", "http://localhost:5000/status"]
    interval: 30s
    timeout: 10s
    retries: 3
```

#### 1.6 `supabase-realtime` — WebSockets
```yaml
supabase-realtime:
  image: supabase/realtime:v2.30.34
  environment:
    DB_HOST: supabase-db
    DB_PORT: 5432
    DB_USER: ${POSTGRES_USER}
    DB_PASSWORD: ${POSTGRES_PASSWORD}
    DB_NAME: ${POSTGRES_DB}
    PORT: 4000
    SECRET_KEY_BASE: ${REALTIME_SECRET_KEY_BASE}
  depends_on:
    supabase-db:
      condition: service_healthy
  networks:
    - backend
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:4000/api/health"]
    interval: 30s
    timeout: 10s
    retries: 3
```

#### 1.7 `pgbouncer` — Connection Pooling
```yaml
pgbouncer:
  image: edoburu/pgbouncer:1.22.0
  environment:
    DATABASE_URL: "postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@supabase-db:5432/${POSTGRES_DB}"
    POOL_MODE: transaction
    MAX_CLIENT_CONN: 200
    DEFAULT_POOL_SIZE: 20
    MIN_POOL_SIZE: 5
    RESERVE_POOL_SIZE: 5
    RESERVE_POOL_TIMEOUT: 3
    SERVER_RESET_QUERY: "DISCARD ALL"
    AUTH_TYPE: scram-sha-256
  depends_on:
    supabase-db:
      condition: service_healthy
  ports:
    - "6432:6432"
  networks:
    - backend
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -h localhost -p 6432"]
    interval: 30s
    timeout: 10s
    retries: 3
```

**CRITICO**: `POOL_MODE: transaction` es obligatorio. Esto garantiza que `SET LOCAL` (usado para RLS multi-tenant) tenga scope solo dentro de la transaccion actual y no afecte otras conexiones reutilizadas por pgBouncer. `SERVER_RESET_QUERY: "DISCARD ALL"` limpia cualquier estado residual al devolver la conexion al pool.

#### 1.8 `redis` — Cache + Broker Celery
```yaml
redis:
  image: redis:7.4-alpine
  command: redis-server --appendonly yes --maxmemory 256mb --maxmemory-policy allkeys-lru
  volumes:
    - redis_data:/data
  ports:
    - "6379:6379"
  networks:
    - backend
  healthcheck:
    test: ["CMD", "redis-cli", "ping"]
    interval: 30s
    timeout: 10s
    retries: 3
```

#### 1.9-1.13 Workers Celery (5 colas)

Cada worker es un servicio separado con su propia cola y concurrencia:

```yaml
celery-webhooks:
  build:
    context: .
    dockerfile: Dockerfile
    target: runner
  command: celery -A app.tasks.celery_config worker -Q webhooks -c 4 --loglevel=info -n webhooks@%h
  environment:
    - PGBOUNCER_URL=${PGBOUNCER_URL}
    - REDIS_URL=${REDIS_URL}
    - OPENAI_API_KEY=${OPENAI_API_KEY}
  depends_on:
    pgbouncer:
      condition: service_healthy
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

#### 1.14 `celery-beat` — Scheduler
```yaml
celery-beat:
  build:
    context: .
    dockerfile: Dockerfile
    target: runner
  command: celery -A app.tasks.celery_config beat --loglevel=info --schedule=/tmp/celerybeat-schedule
  environment:
    - PGBOUNCER_URL=${PGBOUNCER_URL}
    - REDIS_URL=${REDIS_URL}
  depends_on:
    redis:
      condition: service_healthy
  networks:
    - backend
```

#### 1.15 `prometheus` — Metricas
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

#### 1.16 `grafana` — Dashboards
```yaml
grafana:
  image: grafana/grafana:11.2.0
  environment:
    GF_SECURITY_ADMIN_USER: ${GRAFANA_ADMIN_USER}
    GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD}
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
  postgres_data:
  storage_data:
  redis_data:
  prometheus_data:
  grafana_data:
```

### 2. Health Checks

Cada servicio DEBE tener un healthcheck con los parametros estandar:
- `interval: 30s`
- `timeout: 10s`
- `retries: 3`
- `start_period: 40s` (para servicios que tardan en iniciar como supabase-db)

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
COPY ./alembic ./alembic
COPY ./alembic.ini .

# Cambiar permisos y usuario
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8000/internal/health || exit 1

# Uvicorn con workers basados en CPU cores
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
```

Notas del Dockerfile:
- `tesseract-ocr` y `tesseract-ocr-spa` se instalan para el pipeline de OCR del Sprint 5
- El usuario `appuser` no tiene privilegios de root
- El HEALTHCHECK interno verifica el endpoint `/internal/health`
- `--factory` permite usar el patron app factory de FastAPI

### 6. Celery Config

Archivo `app/tasks/celery_config.py`:

```python
from celery import Celery
from kombu import Queue, Exchange

# Crear instancia de Celery
celery_app = Celery("omnichannel")

# Configuracion base
celery_app.conf.update(
    # Broker y backend
    broker_url="redis://redis:6379/0",
    result_backend="redis://redis:6379/1",

    # Serializacion
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,

    # Retry global
    task_acks_late=True,
    task_reject_on_worker_lost=True,

    # Definicion de colas
    task_queues=(
        Queue("webhooks", Exchange("webhooks"), routing_key="webhooks"),
        Queue("ai_inference", Exchange("ai_inference"), routing_key="ai_inference"),
        Queue("documents", Exchange("documents"), routing_key="documents"),
        Queue("notifications", Exchange("notifications"), routing_key="notifications"),
        Queue("bulk", Exchange("bulk"), routing_key="bulk"),
    ),

    # Routing automatico por nombre de task
    task_routes={
        "app.tasks.webhook_*": {"queue": "webhooks"},
        "app.tasks.ai_*": {"queue": "ai_inference"},
        "app.tasks.document_*": {"queue": "documents"},
        "app.tasks.notification_*": {"queue": "notifications"},
        "app.tasks.bulk_*": {"queue": "bulk"},
    },

    # Cola por defecto
    task_default_queue="webhooks",

    # Concurrencia y prefetch
    worker_prefetch_multiplier=1,

    # Beat schedule (tareas periodicas)
    beat_schedule={
        "auto-close-conversations": {
            "task": "app.tasks.bulk_auto_close_conversations",
            "schedule": 900.0,  # cada 15 minutos
        },
        "check-token-budgets": {
            "task": "app.tasks.notification_check_token_budgets",
            "schedule": 3600.0,  # cada hora
        },
    },
)
```

Notas de la configuracion:
- `task_acks_late=True`: el worker confirma la tarea DESPUES de ejecutarla, no antes. Si el worker muere, la tarea se re-encola.
- `task_reject_on_worker_lost=True`: si el worker se pierde, la tarea se rechaza y re-encola.
- `worker_prefetch_multiplier=1`: cada worker toma solo 1 tarea a la vez, importante para tareas largas de IA.
- El routing usa patrones glob: `webhook_*` captura todas las tareas que empiecen con ese prefijo.

### 7. .env.example

```bash
# ================================================
# POSTGRESQL / SUPABASE DB
# ================================================
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_secure_password_here
POSTGRES_DB=omnichannel
DATABASE_URL=postgres://postgres:your_secure_password_here@supabase-db:5432/omnichannel

# ================================================
# PGBOUNCER
# ================================================
PGBOUNCER_URL=postgres://postgres:your_secure_password_here@pgbouncer:6432/omnichannel

# ================================================
# REDIS
# ================================================
REDIS_URL=redis://redis:6379/0

# ================================================
# JWT / AUTENTICACION
# ================================================
JWT_SECRET=your_jwt_secret_min_32_chars_here
JWT_ALGORITHM=HS256
JWT_EXPIRATION_MINUTES=30
JWT_REFRESH_EXPIRATION_DAYS=7

# ================================================
# CIFRADO
# ================================================
ENCRYPTION_KEY=your_encryption_key_min_32_chars_here

# ================================================
# OPENAI
# ================================================
OPENAI_API_KEY=sk-your_openai_api_key_here
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
OPENAI_CHAT_MODEL=gpt-4o

# ================================================
# YCLOUD (MESSAGING PROVIDER)
# ================================================
YCLOUD_API_KEY=your_ycloud_api_key_here
YCLOUD_WEBHOOK_SECRET=your_ycloud_webhook_secret_here

# ================================================
# SUPABASE
# ================================================
SUPABASE_ANON_KEY=your_supabase_anon_key_here
SUPABASE_SERVICE_KEY=your_supabase_service_key_here
SITE_URL=http://localhost:3000

# ================================================
# SUPABASE REALTIME
# ================================================
REALTIME_SECRET_KEY_BASE=your_secret_key_base_min_64_chars_here

# ================================================
# TRAEFIK
# ================================================
TRAEFIK_DASHBOARD_USER=admin
TRAEFIK_DASHBOARD_PASSWORD=admin

# ================================================
# GRAFANA
# ================================================
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=admin_password_here

# ================================================
# GOOGLE CALENDAR (Sprint 7)
# ================================================
# GOOGLE_CLIENT_ID=your_google_client_id
# GOOGLE_CLIENT_SECRET=your_google_client_secret
# GOOGLE_REDIRECT_URI=http://localhost:8000/api/v1/auth/google/callback

# ================================================
# APLICACION
# ================================================
APP_ENV=development
LOG_LEVEL=INFO
CORS_ORIGINS=http://localhost:3000,http://localhost:8080
```

### 8. .dockerignore

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
- [ ] `docker-compose up -d` levanta todos los 16 servicios sin errores
- [ ] Todos los servicios alcanzan estado `healthy` en menos de 2 minutos
- [ ] El endpoint `/internal/health` del servicio `api` responde HTTP 200
- [ ] pgBouncer esta en `transaction mode` (verificar con `SHOW pools` via pgBouncer)
- [ ] `init.sql` se ejecuta automaticamente al crear el contenedor de PostgreSQL (verificar tablas con `\dt`)
- [ ] Workers Celery se registran con sus colas respectivas (verificar con `celery inspect active_queues`)
- [ ] Redis es accesible tanto por `api` como por los workers Celery
- [ ] Traefik enruta correctamente al servicio `api` (verificar dashboard en localhost:8080)
- [ ] Grafana accesible en localhost:3000 con credenciales del .env
- [ ] Las 2 replicas del servicio `api` reciben trafico via load balancer de Traefik
- [ ] `docker-compose down && docker-compose up -d` no pierde datos de PostgreSQL (volumen persistente)

## Notas Tecnicas

### pgBouncer en Transaction Mode
pgBouncer DEBE estar en `transaction mode` para compatibilidad con `SET LOCAL`. En este modo:
- Cada transaccion puede usar una conexion diferente del pool
- `SET LOCAL` tiene scope solo dentro de la transaccion actual
- `SERVER_RESET_QUERY = "DISCARD ALL"` limpia estado residual
- Los workers Celery deben usar `PGBOUNCER_URL`, nunca `DATABASE_URL` directo

### Redis: Broker y Backend
Redis se usa como:
- DB 0: Broker de Celery (cola de tareas)
- DB 1: Backend de resultados Celery
- DB 2: Cache de la aplicacion (webhook dedup, token budgets)

Separar en databases distintos evita colisiones de keys.

### Volumenes Persistentes
Los volumenes nombrados garantizan persistencia:
- `postgres_data`: datos de PostgreSQL
- `storage_data`: archivos subidos via Supabase Storage
- `redis_data`: datos de Redis (AOF habilitado)
- `grafana_data`: dashboards y configuracion de Grafana
- `prometheus_data`: metricas historicas

### Orden de Inicio
El grafo de dependencias es:
```
supabase-db → pgbouncer → api
supabase-db → supabase-auth
supabase-db → supabase-storage
supabase-db → supabase-realtime
redis → api
redis → celery-* (todos los workers)
pgbouncer + redis → celery-* (todos los workers)
prometheus → grafana
```

## Dependencias para Sprint 3
- El servicio `api` debe estar accesible via Traefik en `api.localhost` (o la URL configurada)
- pgBouncer debe aceptar conexiones del servicio `api` y de los workers Celery
- Redis debe ser accesible por `api` y todos los workers Celery en la red `backend`
- El endpoint `/internal/health` debe existir como placeholder (puede retornar `{"status": "ok"}`)
- La imagen Docker debe tener `tesseract-ocr` instalado para el Sprint 5
