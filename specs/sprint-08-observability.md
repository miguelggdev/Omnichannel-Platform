# Sprint 8 — Observabilidad, Backup & Hardening

> **Hito MVP** — Al completar este sprint, un tenant puede operar en producción.

## Objetivo

El MVP es operable en producción: monitoreo end-to-end con OpenTelemetry, logging estructurado con Loguru, métricas en Prometheus, dashboards en Grafana, backups automatizados, cifrado de datos sensibles con pgcrypto, auditoría a nivel de base de datos y cumplimiento RGPD.

## Prerequisitos

- Sprints 1–7 completados y funcionales.
- Prometheus y Grafana ya desplegados en Docker Compose (configurados en Sprint 2).
- Supabase self-hosted con PostgreSQL 15+ y extensión pgcrypto habilitada.
- Redis y Celery Beat operativos.
- Variable de entorno `ENCRYPTION_KEY` definida (AES-256, 32 bytes base64).
- Acceso a un almacenamiento S3-compatible (MinIO en desarrollo, S3/R2 en producción).

## Archivos a Crear/Modificar

| Archivo | Acción | Descripción |
|---|---|---|
| `app/core/telemetry.py` | Crear | Setup del SDK de OpenTelemetry (traces, metrics, context propagation) |
| `app/core/logging.py` | Crear | Configuración de Loguru estructurado con integración OTel |
| `app/api/v1/admin.py` | Modificar | Agregar endpoints RGPD y Quick Replies |
| `app/models/audit_log.py` | Crear | Modelo SQLAlchemy para audit_logs |
| `app/models/quick_reply.py` | Crear | Modelo SQLAlchemy para quick_replies |
| `scripts/backup.sh` | Crear | Script de backup diario con pg_dump |
| `scripts/restore_test.sh` | Crear | Script de test de restauración |
| `grafana/dashboards/system_health.json` | Crear | Dashboard de salud del sistema |
| `grafana/dashboards/tenant_usage.json` | Crear | Dashboard de uso por tenant |
| `grafana/dashboards/agent_performance.json` | Crear | Dashboard de rendimiento de agentes |
| `grafana/dashboards/celery_queues.json` | Crear | Dashboard de colas Celery |
| `grafana/provisioning/dashboards.yml` | Crear | Provisioning automático de dashboards |
| `prometheus/prometheus.yml` | Modificar | Agregar scrape targets |
| `prometheus/alerts.yml` | Crear | Reglas de alertas |
| `docker-compose.yml` | Modificar | Agregar Jaeger/OTLP collector |
| `migrations/versions/xxx_audit_logs.py` | Crear | Migración para tabla audit_logs |
| `migrations/versions/xxx_quick_replies.py` | Crear | Migración para tabla quick_replies |
| `tests/e2e/test_full_flow.py` | Crear | Test end-to-end completo |
| `tests/unit/test_quick_replies.py` | Crear | Tests unitarios de Quick Replies |

## Tareas Detalladas

### 1. OpenTelemetry — `app/core/telemetry.py`

**1.1 Instalación de dependencias**

Agregar a `requirements.txt`:
```
opentelemetry-api>=1.24.0
opentelemetry-sdk>=1.24.0
opentelemetry-instrumentation-fastapi>=0.45b0
opentelemetry-instrumentation-celery>=0.45b0
opentelemetry-instrumentation-sqlalchemy>=0.45b0
opentelemetry-instrumentation-httpx>=0.45b0
opentelemetry-instrumentation-redis>=0.45b0
opentelemetry-exporter-otlp>=1.24.0
opentelemetry-exporter-prometheus>=0.45b0
```

**1.2 Implementación del módulo telemetry.py**

```python
# app/core/telemetry.py
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.celery import CeleryInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositeTextMapPropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator

def setup_telemetry(app, engine, settings):
    """Inicializar OpenTelemetry con todas las instrumentaciones."""
    resource = Resource.create({
        SERVICE_NAME: "conversational-ai-platform",
        "service.version": settings.APP_VERSION,
        "deployment.environment": settings.ENVIRONMENT,
    })

    # Traces
    tracer_provider = TracerProvider(resource=resource)
    otlp_exporter = OTLPSpanExporter(
        endpoint=settings.OTLP_ENDPOINT,  # e.g. "http://jaeger:4317"
        insecure=settings.ENVIRONMENT == "development",
    )
    tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    trace.set_tracer_provider(tracer_provider)

    # Metrics (exportar a Prometheus)
    prometheus_reader = PrometheusMetricReader()
    meter_provider = MeterProvider(resource=resource, metric_readers=[prometheus_reader])
    metrics.set_meter_provider(meter_provider)

    # Propagación W3C TraceContext
    set_global_textmap(CompositeTextMapPropagator([
        TraceContextTextMapPropagator(),
        W3CBaggagePropagator(),
    ]))

    # Instrumentaciones
    FastAPIInstrumentor.instrument_app(app)
    CeleryInstrumentor().instrument()
    SQLAlchemyInstrumentor().instrument(engine=engine)
    RedisInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()

    return tracer_provider, meter_provider
```

**1.3 Propagación de Trace ID**

- El middleware de FastAPI recibe `traceparent` header y genera span padre.
- El Celery task recibe el trace context via headers de Celery (CeleryInstrumentor lo maneja automáticamente).
- Las queries SQL aparecen como child spans del request/task.
- Implementar middleware custom para inyectar `trace_id` en el contexto de logging:

```python
# En el middleware de FastAPI (app/api/middleware.py)
from opentelemetry import trace

@app.middleware("http")
async def trace_context_middleware(request, call_next):
    span = trace.get_current_span()
    trace_id = format(span.get_span_context().trace_id, '032x') if span else "no-trace"
    # Inyectar en Loguru context
    with logger.contextualize(trace_id=trace_id):
        response = await call_next(request)
    response.headers["X-Trace-ID"] = trace_id
    return response
```

**1.4 Docker Compose — Agregar Jaeger**

```yaml
# En docker-compose.yml
jaeger:
  image: jaegertracing/all-in-one:1.54
  environment:
    - COLLECTOR_OTLP_ENABLED=true
  ports:
    - "16686:16686"   # UI
    - "4317:4317"     # OTLP gRPC
    - "4318:4318"     # OTLP HTTP
  networks:
    - internal
```

### 2. Loguru Estructurado — `app/core/logging.py`

**2.1 Implementación**

```python
# app/core/logging.py
import sys
import json
from loguru import logger
from app.core.config import settings

def serialize_record(record):
    """Serializar log record a JSON para producción."""
    subset = {
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "message": record["message"],
        "module": record["module"],
        "function": record["function"],
        "line": record["line"],
        # Contexto obligatorio (inyectado via logger.contextualize)
        "trace_id": record["extra"].get("trace_id", ""),
        "client_id": record["extra"].get("client_id", ""),
        "user_id": record["extra"].get("user_id", ""),
    }
    # Agregar exception info si existe
    if record["exception"]:
        subset["exception"] = {
            "type": record["exception"].type.__name__,
            "value": str(record["exception"].value),
            "traceback": record["exception"].traceback,
        }
    return json.dumps(subset, default=str)

def json_sink(message):
    """Sink que escribe JSON a stdout."""
    record = message.record
    serialized = serialize_record(record)
    sys.stdout.write(serialized + "\n")

def setup_logging():
    """Configurar Loguru según el entorno."""
    logger.remove()  # Remover handler por defecto

    if settings.ENVIRONMENT == "production":
        # Producción: JSON a stdout (para log aggregation)
        logger.add(
            json_sink,
            level="INFO",
            serialize=False,
        )
        # Archivo rotado para backup local
        logger.add(
            "/var/log/app/app.log",
            rotation="100 MB",
            retention="30 days",
            compression="gz",
            level="INFO",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {extra[trace_id]} | {extra[client_id]} | {message}",
        )
    else:
        # Desarrollo: formato legible a consola
        logger.add(
            sys.stderr,
            level="DEBUG",
            format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{extra[trace_id]}</cyan> | {message}",
        )

    # Filtros por módulo (ejemplo: silenciar SQLAlchemy en INFO)
    logger.add(
        sys.stderr,
        level="WARNING",
        filter={"sqlalchemy": "WARNING", "httpx": "WARNING"},
    )

    return logger
```

**2.2 Contexto obligatorio**

Cada log DEBE incluir: `client_id`, `trace_id`, `user_id`. Usar `logger.contextualize()` en:
- Middleware HTTP: al extraer client_id del JWT y trace_id del span.
- Celery task: al inicio de cada task, extraer client_id y trace_id del task context.
- Background jobs: propagado manualmente.

**2.3 Integración OTel + Loguru**

El `trace_id` de OpenTelemetry se inyecta en Loguru context. Esto permite correlacionar logs con traces en Jaeger/Grafana Tempo.

### 3. Prometheus Metrics

**3.1 Definición de métricas custom**

```python
# En app/core/telemetry.py (agregar después del setup)
from opentelemetry import metrics

meter = metrics.get_meter("conversational_ai")

# Histogramas
endpoint_latency = meter.create_histogram(
    name="http_request_duration_seconds",
    description="Latencia por endpoint",
    unit="s",
)
rag_retrieval_latency = meter.create_histogram(
    name="rag_retrieval_latency_seconds",
    description="Latencia de retrieval RAG",
    unit="s",
)

# Contadores
tokens_consumed = meter.create_counter(
    name="llm_tokens_consumed_total",
    description="Tokens consumidos por tenant",
)
handoff_total = meter.create_counter(
    name="handoff_total",
    description="Tasa de handoff por tenant",
)
messages_processed = meter.create_counter(
    name="messages_processed_total",
    description="Mensajes procesados por canal",
)

# Gauges (via UpDownCounter en OTel)
celery_queue_size = meter.create_up_down_counter(
    name="celery_queue_size",
    description="Tamaño actual de la cola Celery",
)

intent_routing_confidence = meter.create_histogram(
    name="intent_routing_confidence",
    description="Confianza del intent routing",
)
```

**3.2 Instrumentación en código**

- En cada endpoint de FastAPI, registrar latencia con labels `{endpoint, method, status_code}`.
- En el servicio LLM, registrar tokens con labels `{client_id, model, agent_type}`.
- En el nodo handoff, incrementar `handoff_total` con labels `{client_id, reason}`.
- En el webhook handler, incrementar `messages_processed` con labels `{channel, client_id}`.
- En el servicio RAG, registrar `rag_retrieval_latency` con labels `{client_id, collection}`.
- En intent routing, registrar `intent_routing_confidence` con labels `{intent, client_id}`.

**3.3 Endpoint /metrics**

Exponer endpoint `/metrics` para que Prometheus haga scrape:
```python
from prometheus_client import make_asgi_app

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)
```

### 4. Prometheus Config — `prometheus/prometheus.yml`

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

rule_files:
  - "alerts.yml"

scrape_configs:
  - job_name: "fastapi"
    static_configs:
      - targets: ["api:8000"]
    metrics_path: /metrics

  - job_name: "celery-exporter"
    static_configs:
      - targets: ["celery-exporter:9808"]

  - job_name: "postgres-exporter"
    static_configs:
      - targets: ["postgres-exporter:9187"]

  - job_name: "redis-exporter"
    static_configs:
      - targets: ["redis-exporter:9121"]

  - job_name: "traefik"
    static_configs:
      - targets: ["traefik:8082"]

alerting:
  alertmanagers:
    - static_configs:
        - targets: ["alertmanager:9093"]
```

### 5. Alertas — `prometheus/alerts.yml`

```yaml
groups:
  - name: platform_alerts
    rules:
      - alert: HighLatencyP95
        expr: histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m])) > 5
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Latencia p95 > 5s"
          description: "El percentil 95 de latencia supera los 5 segundos."

      - alert: HighErrorRate
        expr: rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m]) > 0.05
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "Error rate > 5%"
          description: "La tasa de errores HTTP 5xx supera el 5%."

      - alert: TokenBudgetWarning
        expr: llm_tokens_consumed_total / on(client_id) token_budget_limit > 0.8
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "Token budget > 80% para tenant {{ $labels.client_id }}"

      - alert: CeleryQueueDepth
        expr: celery_queue_size > 1000
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Cola Celery con más de 1000 tareas pendientes"

      - alert: DBConnectionPoolHigh
        expr: pg_stat_activity_count / pg_settings_max_connections > 0.8
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Pool de conexiones DB > 80%"
```

### 6. Grafana Dashboards

Crear 4 dashboards como archivos JSON provisioned. Cada archivo debe seguir el formato estándar de Grafana dashboard JSON.

**6.1 Dashboard "System Health" — `grafana/dashboards/system_health.json`**

Paneles requeridos:
- **Request Rate**: `rate(http_requests_total[5m])` — gráfica de líneas
- **Error Rate**: `rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m]) * 100` — gauge con umbrales (verde < 1%, amarillo < 5%, rojo >= 5%)
- **Latency p50/p95/p99**: `histogram_quantile(0.5/0.95/0.99, rate(http_request_duration_seconds_bucket[5m]))` — gráfica de líneas con 3 series
- **CPU Usage**: panel de node_exporter si disponible
- **Memory Usage**: panel de node_exporter si disponible
- **Active DB Connections**: `pg_stat_activity_count` — gauge

**6.2 Dashboard "Tenant Usage" — `grafana/dashboards/tenant_usage.json`**

Paneles requeridos:
- **Tokens por Tenant**: `sum by (client_id) (llm_tokens_consumed_total)` — bar chart
- **Mensajes por Canal**: `sum by (channel) (messages_processed_total)` — pie chart
- **Conversaciones Activas**: `sum by (client_id) (active_conversations)` — stat panel
- **Top 10 Tenants por Tokens**: tabla ordenada

**6.3 Dashboard "Agent Performance" — `grafana/dashboards/agent_performance.json`**

Paneles requeridos:
- **Tasa de Resolución**: porcentaje de conversaciones resueltas sin handoff — stat panel
- **Handoff Rate**: `rate(handoff_total[1h])` por tenant — gráfica
- **Avg Response Time**: `avg(http_request_duration_seconds)` por agent_type — gráfica
- **RAG Confidence**: `histogram_quantile(0.5, rag_retrieval_latency_seconds_bucket)` — gráfica
- **Intent Distribution**: distribución de intents — pie chart

**6.4 Dashboard "Celery Queues" — `grafana/dashboards/celery_queues.json`**

Paneles requeridos:
- **Queue Size por Cola**: `celery_queue_size` por queue_name — gráfica
- **Throughput**: tareas completadas por minuto — gráfica
- **Failures**: tareas fallidas — gráfica con alerta visual
- **Processing Time**: histograma de tiempo de procesamiento por cola

**6.5 Provisioning — `grafana/provisioning/dashboards.yml`**

```yaml
apiVersion: 1
providers:
  - name: "default"
    orgId: 1
    type: file
    disableDeletion: false
    editable: true
    updateIntervalSeconds: 30
    options:
      path: /var/lib/grafana/dashboards
      foldersFromFilesStructure: false
```

### 7. Backup & Restore

**7.1 Script de Backup — `scripts/backup.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

# Variables de entorno requeridas:
# PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE
# S3_BUCKET, S3_ENDPOINT (para MinIO), AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY

DATE=$(date +%Y-%m-%d_%H-%M-%S)
DAY_OF_WEEK=$(date +%u)  # 1=lunes, 7=domingo
BACKUP_DIR="/tmp/backups"
BACKUP_FILE="backup_${PGDATABASE}_${DATE}.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "[$(date)] Iniciando backup de ${PGDATABASE}..."

# pg_dump con compresión
pg_dump \
  -h "$PGHOST" \
  -p "$PGPORT" \
  -U "$PGUSER" \
  -d "$PGDATABASE" \
  --format=custom \
  --compress=9 \
  --verbose \
  --file="${BACKUP_DIR}/${BACKUP_FILE}"

echo "[$(date)] Backup local completado: ${BACKUP_FILE}"

# Upload a S3
aws s3 cp \
  "${BACKUP_DIR}/${BACKUP_FILE}" \
  "s3://${S3_BUCKET}/daily/${BACKUP_FILE}" \
  --endpoint-url "${S3_ENDPOINT:-}"

echo "[$(date)] Upload a S3 completado."

# Copia semanal (domingos)
if [ "$DAY_OF_WEEK" -eq 7 ]; then
  aws s3 cp \
    "${BACKUP_DIR}/${BACKUP_FILE}" \
    "s3://${S3_BUCKET}/weekly/${BACKUP_FILE}" \
    --endpoint-url "${S3_ENDPOINT:-}"
  echo "[$(date)] Copia semanal creada."
fi

# Limpieza de backups locales (mantener últimos 3)
ls -t "${BACKUP_DIR}"/backup_*.sql.gz | tail -n +4 | xargs -r rm

# Limpieza S3: retención 30 días diarios, 12 semanas semanales
aws s3 ls "s3://${S3_BUCKET}/daily/" --endpoint-url "${S3_ENDPOINT:-}" \
  | awk '{print $4}' \
  | while read -r file; do
      file_date=$(echo "$file" | grep -oP '\d{4}-\d{2}-\d{2}')
      if [ -n "$file_date" ]; then
        days_old=$(( ($(date +%s) - $(date -d "$file_date" +%s)) / 86400 ))
        if [ "$days_old" -gt 30 ]; then
          aws s3 rm "s3://${S3_BUCKET}/daily/${file}" --endpoint-url "${S3_ENDPOINT:-}"
          echo "[$(date)] Eliminado backup diario antiguo: ${file}"
        fi
      fi
    done

echo "[$(date)] Backup completado exitosamente."
```

**7.2 Script de Restore Test — `scripts/restore_test.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

# Descargar último backup
LATEST=$(aws s3 ls "s3://${S3_BUCKET}/daily/" --endpoint-url "${S3_ENDPOINT:-}" \
  | sort | tail -1 | awk '{print $4}')

if [ -z "$LATEST" ]; then
  echo "ERROR: No se encontraron backups."
  exit 1
fi

RESTORE_DIR="/tmp/restore_test"
TEMP_DB="restore_test_$(date +%s)"
mkdir -p "$RESTORE_DIR"

echo "[$(date)] Descargando backup: ${LATEST}"
aws s3 cp "s3://${S3_BUCKET}/daily/${LATEST}" "${RESTORE_DIR}/${LATEST}" \
  --endpoint-url "${S3_ENDPOINT:-}"

echo "[$(date)] Creando base de datos temporal: ${TEMP_DB}"
createdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$TEMP_DB"

echo "[$(date)] Restaurando backup..."
pg_restore \
  -h "$PGHOST" \
  -p "$PGPORT" \
  -U "$PGUSER" \
  -d "$TEMP_DB" \
  --verbose \
  --no-owner \
  "${RESTORE_DIR}/${LATEST}"

echo "[$(date)] Verificando integridad..."
# Verificar que las tablas principales existen y tienen datos
for table in clients contacts conversations messages users; do
  count=$(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$TEMP_DB" -t -c \
    "SELECT COUNT(*) FROM ${table};")
  echo "  Tabla ${table}: ${count} registros"
done

# Verificar RLS policies
rls_count=$(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$TEMP_DB" -t -c \
  "SELECT COUNT(*) FROM pg_policies;")
echo "  Políticas RLS: ${rls_count}"

echo "[$(date)] Limpiando base de datos temporal..."
dropdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$TEMP_DB"
rm -rf "$RESTORE_DIR"

echo "[$(date)] Restore test EXITOSO."
```

**7.3 Celery Beat schedule**

Agregar en la configuración de Celery Beat:
```python
# En app/core/celery_config.py
beat_schedule = {
    "daily-backup": {
        "task": "app.tasks.maintenance.run_backup",
        "schedule": crontab(hour=3, minute=0),  # 3 AM UTC
    },
    "monthly-restore-test": {
        "task": "app.tasks.maintenance.run_restore_test",
        "schedule": crontab(day_of_month=1, hour=4, minute=0),  # 1ro del mes, 4 AM UTC
    },
}
```

**7.4 Tasks de mantenimiento**

```python
# app/tasks/maintenance.py
import subprocess
from app.core.celery_app import celery_app

@celery_app.task(queue="maintenance")
def run_backup():
    result = subprocess.run(
        ["bash", "/app/scripts/backup.sh"],
        capture_output=True, text=True, timeout=3600
    )
    if result.returncode != 0:
        raise RuntimeError(f"Backup falló: {result.stderr}")
    return result.stdout

@celery_app.task(queue="maintenance")
def run_restore_test():
    result = subprocess.run(
        ["bash", "/app/scripts/restore_test.sh"],
        capture_output=True, text=True, timeout=7200
    )
    if result.returncode != 0:
        raise RuntimeError(f"Restore test falló: {result.stderr}")
    return result.stdout
```

### 8. Cifrado pgcrypto

**8.1 Habilitar extensión**

```sql
-- En migración
CREATE EXTENSION IF NOT EXISTS pgcrypto;
```

**8.2 Custom type SQLAlchemy**

```python
# app/core/encryption.py
from sqlalchemy import TypeDecorator, Text, func
from app.core.config import settings

class EncryptedString(TypeDecorator):
    """Tipo SQLAlchemy que cifra/descifra transparentemente con pgcrypto."""
    impl = Text
    cache_ok = True

    def bind_expression(self, bindvalue):
        """Cifrar al insertar/actualizar."""
        return func.pgp_sym_encrypt(
            bindvalue,
            settings.ENCRYPTION_KEY,
            type_=self.impl
        )

    def column_expression(self, col):
        """Descifrar al leer."""
        return func.pgp_sym_decrypt(
            col,
            settings.ENCRYPTION_KEY,
            type_=Text
        )
```

**8.3 Aplicar a modelos**

```python
# En app/models/contact.py
from app.core.encryption import EncryptedString

class ContactIdentifier(Base):
    __tablename__ = "contact_identifiers"
    # ... otros campos
    identifier_value = Column(EncryptedString)  # Cifrado siempre

class Contact(Base):
    __tablename__ = "contacts"
    # ... otros campos
    first_name = Column(EncryptedString)  # Cifrado opcional por tenant
    last_name = Column(EncryptedString)   # Cifrado opcional por tenant
```

**8.4 Notas sobre cifrado**

- El `ENCRYPTION_KEY` NUNCA se almacena en la base de datos; viene de variable de entorno.
- La columna almacena el blob cifrado (bytea). Las queries exactas (WHERE first_name = 'X') NO funcionan sobre columnas cifradas — se debe descifrar primero o usar un hash auxiliar.
- Para búsquedas, considerar un campo `name_hash` (SHA-256 del nombre) como índice de búsqueda exacta.
- El cifrado es opcional por tenant: controlado por una flag en `clients.settings` (JSONB).

### 9. Auditoría

**9.1 Modelo — `app/models/audit_log.py`**

```python
# app/models/audit_log.py
from sqlalchemy import Column, String, DateTime, Text, Enum
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.models.base import Base
import uuid
from datetime import datetime

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    table_name = Column(String(100), nullable=False, index=True)
    record_id = Column(UUID(as_uuid=True), nullable=False)
    action = Column(
        Enum("INSERT", "UPDATE", "DELETE", name="audit_action_enum"),
        nullable=False
    )
    old_values = Column(JSONB)
    new_values = Column(JSONB)
    user_id = Column(UUID(as_uuid=True))  # NULL para acciones del sistema
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # RLS: audit_logs filtrados por client_id
```

**9.2 Migración — Trigger PostgreSQL**

El trigger se crea directamente en PostgreSQL, NO en la aplicación Python:

```sql
-- Función de auditoría genérica
CREATE OR REPLACE FUNCTION audit_trigger_function()
RETURNS TRIGGER AS $$
DECLARE
    _client_id UUID;
    _user_id UUID;
BEGIN
    -- Obtener client_id del registro
    IF TG_OP = 'DELETE' THEN
        _client_id := OLD.client_id;
    ELSE
        _client_id := NEW.client_id;
    END IF;

    -- Obtener user_id del contexto de sesión (inyectado por middleware)
    BEGIN
        _user_id := current_setting('app.current_user_id', true)::UUID;
    EXCEPTION WHEN OTHERS THEN
        _user_id := NULL;
    END;

    INSERT INTO audit_logs (id, client_id, table_name, record_id, action, old_values, new_values, user_id, created_at)
    VALUES (
        gen_random_uuid(),
        _client_id,
        TG_TABLE_NAME,
        CASE
            WHEN TG_OP = 'DELETE' THEN OLD.id
            ELSE NEW.id
        END,
        TG_OP,
        CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN to_jsonb(OLD) ELSE NULL END,
        CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN to_jsonb(NEW) ELSE NULL END,
        _user_id,
        NOW()
    );

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    ELSE
        RETURN NEW;
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Aplicar trigger a tablas sensibles
CREATE TRIGGER audit_contacts
    AFTER INSERT OR UPDATE OR DELETE ON contacts
    FOR EACH ROW EXECUTE FUNCTION audit_trigger_function();

CREATE TRIGGER audit_conversations
    AFTER INSERT OR UPDATE OR DELETE ON conversations
    FOR EACH ROW EXECUTE FUNCTION audit_trigger_function();

CREATE TRIGGER audit_messages
    AFTER INSERT OR UPDATE OR DELETE ON messages
    FOR EACH ROW EXECUTE FUNCTION audit_trigger_function();

CREATE TRIGGER audit_users
    AFTER INSERT OR UPDATE OR DELETE ON users
    FOR EACH ROW EXECUTE FUNCTION audit_trigger_function();
```

**9.3 RLS en audit_logs**

```sql
ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;

CREATE POLICY audit_logs_tenant_isolation ON audit_logs
    USING (client_id = current_setting('app.current_client_id')::UUID);
```

**9.4 Nota sobre `user_id` en contexto**

El middleware debe hacer `SET LOCAL app.current_user_id = '{user_id}'` junto con el `client_id`. Esto se usa únicamente por el trigger de auditoría.

### 10. Endpoints RGPD — `app/api/v1/admin.py`

**10.1 Exportación de datos de contacto**

```python
# GET /api/v1/admin/contacts/{contact_id}/export
@router.get("/contacts/{contact_id}/export")
async def export_contact_data(
    contact_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "super_admin"])),
):
    """
    Exportar todos los datos asociados a un contacto en formato JSON.
    Incluye: datos del contacto, identificadores, conversaciones, mensajes,
    citas, notas y etiquetas.
    """
    contact = await contact_service.get_with_all_relations(db, contact_id)
    if not contact:
        raise HTTPException(404, "Contacto no encontrado")

    export_data = {
        "export_date": datetime.utcnow().isoformat(),
        "contact": contact_schema.dump(contact),
        "identifiers": [id_schema.dump(i) for i in contact.identifiers],
        "conversations": [],
    }

    for conv in contact.conversations:
        conv_data = conversation_schema.dump(conv)
        conv_data["messages"] = [msg_schema.dump(m) for m in conv.messages]
        export_data["conversations"].append(conv_data)

    export_data["appointments"] = [appt_schema.dump(a) for a in contact.appointments]
    export_data["tags"] = [tag.name for tag in contact.tags]

    # Registrar en audit_log
    await audit_service.log(db, "GDPR_EXPORT", "contacts", contact_id)

    return export_data
```

**10.2 Eliminación RGPD (anonimización)**

```python
# DELETE /api/v1/admin/contacts/{contact_id}/gdpr-delete
@router.delete("/contacts/{contact_id}/gdpr-delete")
async def gdpr_delete_contact(
    contact_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "super_admin"])),
):
    """
    Anonimizar datos del contacto (no borrar registros para mantener integridad referencial).
    - Reemplazar datos personales con "[ELIMINADO]"
    - Mantener registros para estadísticas agregadas
    - Registrar acción en audit_log
    """
    contact = await contact_service.get(db, contact_id)
    if not contact:
        raise HTTPException(404, "Contacto no encontrado")

    # Anonimizar contacto
    contact.first_name = "[ELIMINADO]"
    contact.last_name = "[ELIMINADO]"
    contact.metadata = {}

    # Anonimizar identificadores
    for identifier in contact.identifiers:
        identifier.identifier_value = f"[ELIMINADO-{identifier.id.hex[:8]}]"

    # Anonimizar contenido de mensajes
    for conv in contact.conversations:
        for message in conv.messages:
            if message.direction == "incoming":
                message.content = "[CONTENIDO ELIMINADO POR SOLICITUD RGPD]"
                message.media_url = None

    # Marcar contacto como eliminado
    contact.is_gdpr_deleted = True
    contact.gdpr_deleted_at = datetime.utcnow()

    await db.commit()
    await audit_service.log(db, "GDPR_DELETE", "contacts", contact_id)

    return {"status": "success", "message": "Datos del contacto anonimizados"}
```

### 11. Quick Replies CRUD

**11.1 Modelo — `app/models/quick_reply.py`**

```python
# app/models/quick_reply.py
from sqlalchemy import Column, String, Text, DateTime, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from app.models.base import Base, TimestampMixin
import uuid

class QuickReply(Base, TimestampMixin):
    __tablename__ = "quick_replies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    shortcut = Column(String(50), nullable=False)  # e.g. "/saludo"
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)  # Soporta variables: {{contact_name}}, etc.
    category = Column(String(50))  # Opcional: categorización
    created_by = Column(UUID(as_uuid=True))

    __table_args__ = (
        UniqueConstraint("client_id", "shortcut", name="uq_quick_reply_shortcut"),
    )
```

**11.2 Endpoints**

```python
# En app/api/v1/admin.py o app/api/v1/quick_replies.py

# GET /api/v1/quick-replies
@router.get("/quick-replies")
async def list_quick_replies(
    category: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Listar quick replies del tenant actual."""
    query = select(QuickReply).where(QuickReply.client_id == current_user.client_id)
    if category:
        query = query.where(QuickReply.category == category)
    result = await db.execute(query.order_by(QuickReply.shortcut))
    return result.scalars().all()

# POST /api/v1/quick-replies
@router.post("/quick-replies", status_code=201)
async def create_quick_reply(
    data: QuickReplyCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "agent"])),
):
    """Crear quick reply con shortcut único por tenant."""
    # Verificar unicidad del shortcut
    existing = await db.execute(
        select(QuickReply).where(
            QuickReply.client_id == current_user.client_id,
            QuickReply.shortcut == data.shortcut,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Shortcut '{data.shortcut}' ya existe")

    quick_reply = QuickReply(
        client_id=current_user.client_id,
        shortcut=data.shortcut,
        title=data.title,
        content=data.content,
        category=data.category,
        created_by=current_user.id,
    )
    db.add(quick_reply)
    await db.commit()
    return quick_reply

# PUT /api/v1/quick-replies/{id}
@router.put("/quick-replies/{quick_reply_id}")
async def update_quick_reply(
    quick_reply_id: UUID,
    data: QuickReplyUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "agent"])),
):
    """Actualizar quick reply."""
    qr = await db.get(QuickReply, quick_reply_id)
    if not qr:
        raise HTTPException(404, "Quick reply no encontrado")
    for field, value in data.dict(exclude_unset=True).items():
        setattr(qr, field, value)
    await db.commit()
    return qr

# DELETE /api/v1/quick-replies/{id}
@router.delete("/quick-replies/{quick_reply_id}", status_code=204)
async def delete_quick_reply(
    quick_reply_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"])),
):
    """Eliminar quick reply."""
    qr = await db.get(QuickReply, quick_reply_id)
    if not qr:
        raise HTTPException(404, "Quick reply no encontrado")
    await db.delete(qr)
    await db.commit()
```

**11.3 Resolución de variables dinámicas**

```python
# app/services/quick_reply_service.py
import re

VARIABLE_PATTERN = re.compile(r"\{\{(\w+)\}\}")

VARIABLE_RESOLVERS = {
    "contact_name": lambda ctx: f"{ctx['contact'].first_name} {ctx['contact'].last_name}".strip(),
    "agent_name": lambda ctx: ctx["agent"].display_name,
    "ticket_id": lambda ctx: str(ctx["conversation"].id)[:8],
    "date": lambda ctx: datetime.utcnow().strftime("%d/%m/%Y"),
}

def resolve_quick_reply(content: str, context: dict) -> str:
    """Resolver variables dinámicas en el contenido del quick reply."""
    def replace_var(match):
        var_name = match.group(1)
        resolver = VARIABLE_RESOLVERS.get(var_name)
        if resolver:
            try:
                return resolver(context)
            except (KeyError, AttributeError):
                return match.group(0)  # Dejar sin resolver si falta contexto
        return match.group(0)

    return VARIABLE_PATTERN.sub(replace_var, content)
```

### 12. Test End-to-End — `tests/e2e/test_full_flow.py`

```python
# tests/e2e/test_full_flow.py
import pytest
import httpx
from uuid import uuid4

@pytest.mark.e2e
class TestFullFlow:
    """Test end-to-end: webhook → dedup → Celery → LangGraph → RAG → respuesta."""

    async def test_whatsapp_message_full_pipeline(
        self, async_client: httpx.AsyncClient, test_tenant, test_channel_config
    ):
        """
        Flujo completo:
        1. Webhook entrante (WhatsApp)
        2. Deduplicación
        3. Celery task: process_incoming_message
        4. LangGraph: intent_routing → rag_query → respond
        5. Respuesta enviada por el mismo canal
        6. Verificar trace_id de punta a punta
        """
        # 1. Enviar webhook simulado
        webhook_payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": test_channel_config.provider_config["business_account_id"],
                "changes": [{
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "phone_number_id": test_channel_config.provider_config["phone_number_id"]
                        },
                        "messages": [{
                            "from": "573001234567",
                            "id": f"wamid.test_{uuid4().hex[:16]}",
                            "timestamp": "1700000000",
                            "type": "text",
                            "text": {"body": "¿Cuáles son los horarios de atención?"}
                        }]
                    },
                    "field": "messages"
                }]
            }]
        }

        response = await async_client.post(
            f"/api/v1/webhooks/whatsapp/{test_channel_config.id}",
            json=webhook_payload,
            headers={"X-Hub-Signature-256": compute_signature(webhook_payload, test_channel_config.webhook_secret)},
        )
        assert response.status_code == 200

        # Capturar trace_id del response header
        trace_id = response.headers.get("X-Trace-ID")
        assert trace_id is not None
        assert len(trace_id) == 32  # 128-bit trace ID en hex

        # 2. Esperar procesamiento Celery (con timeout)
        await wait_for_task_completion(trace_id, timeout=30)

        # 3. Verificar mensaje procesado
        messages = await get_conversation_messages(
            async_client, test_tenant.id, phone="573001234567"
        )
        assert len(messages) >= 2  # incoming + outgoing

        incoming = messages[0]
        assert incoming["direction"] == "incoming"
        assert incoming["content"] == "¿Cuáles son los horarios de atención?"

        outgoing = messages[1]
        assert outgoing["direction"] == "outgoing"
        assert len(outgoing["content"]) > 0  # Respuesta no vacía

        # 4. Verificar deduplicación (enviar mismo mensaje)
        response2 = await async_client.post(
            f"/api/v1/webhooks/whatsapp/{test_channel_config.id}",
            json=webhook_payload,
            headers={"X-Hub-Signature-256": compute_signature(webhook_payload, test_channel_config.webhook_secret)},
        )
        assert response2.status_code == 200

        # Verificar que no se creó un mensaje duplicado
        messages_after = await get_conversation_messages(
            async_client, test_tenant.id, phone="573001234567"
        )
        assert len(messages_after) == len(messages)  # Sin duplicados

        # 5. Verificar trace en logs (consultar logs estructurados)
        logs = await get_logs_by_trace_id(trace_id)
        assert any("process_incoming_message" in log.get("function", "") for log in logs)

        # 6. Verificar métricas Prometheus
        metrics_response = await async_client.get("/metrics")
        assert f'messages_processed_total{{channel="whatsapp"' in metrics_response.text

    async def test_duplicate_message_rejected(self, async_client, test_channel_config):
        """Verificar que mensajes duplicados son rechazados por el dedup."""
        message_id = f"wamid.dedup_test_{uuid4().hex[:8]}"
        # ... similar al anterior pero verificando que el segundo envío no procesa

    async def test_trace_id_propagation(self, async_client, test_channel_config):
        """Verificar que el trace_id se propaga correctamente por todo el pipeline."""
        # Enviar mensaje y capturar trace_id
        # Verificar que aparece en: logs, spans de Jaeger, métricas
        pass
```

## Criterios de Aceptación

| # | Criterio | Verificación |
|---|---|---|
| 1 | Trace ID visible desde webhook hasta respuesta en Grafana/Jaeger | Enviar mensaje → buscar trace_id en Jaeger → ver spans de FastAPI, Celery, SQLAlchemy |
| 2 | Backup ejecuta sin errores | `bash scripts/backup.sh` → archivo .sql.gz creado y subido a S3 |
| 3 | Restore test pasa | `bash scripts/restore_test.sh` → tablas verificadas con datos |
| 4 | Dashboards muestran métricas reales | Enviar requests → ver datos en los 4 dashboards de Grafana |
| 5 | Cifrado de columnas funcional | INSERT con dato → verificar que en DB está cifrado → SELECT devuelve dato legible |
| 6 | Audit triggers funcionan | INSERT/UPDATE/DELETE en contacts → registro en audit_logs con old/new values |
| 7 | Test e2e pasa con trace ID verificado | `pytest tests/e2e/test_full_flow.py` → PASS |
| 8 | RGPD export devuelve JSON completo | GET /admin/contacts/{id}/export → JSON con todos los datos del contacto |
| 9 | RGPD delete anonimiza datos | DELETE /admin/contacts/{id}/gdpr-delete → datos reemplazados con "[ELIMINADO]" |
| 10 | Quick replies CRUD funcional | CRUD completo con variables dinámicas resueltas |

## Notas Técnicas

- **Hito MVP**: Este sprint marca el final del MVP. Después de completarlo, un tenant puede operar en producción con: WhatsApp como canal, agente RAG conversacional, scheduling, handoff a humano, dashboard de métricas, backup automatizado y cumplimiento básico de RGPD.
- **Audit trigger**: Se crea en PostgreSQL directamente (en migración SQL), NO en la aplicación Python. Esto garantiza que cualquier cambio (incluso desde psql) queda auditado.
- **Loguru + OTel**: La integración se realiza via el `trace_id` compartido. Loguru NO reemplaza el tracing de OTel; lo complementa con logs estructurados.
- **pgcrypto**: El cifrado es determinístico para un mismo `ENCRYPTION_KEY`. Si se rota la key, se deben re-cifrar todas las columnas (task de migración).
- **Dashboards provisioned**: Los JSON de Grafana se montan como volumen Docker y se auto-importan al iniciar Grafana. No requieren configuración manual.
- **SET LOCAL**: Recordar que el audit trigger lee `current_setting('app.current_client_id')` y `current_setting('app.current_user_id')`, ambos seteados con `SET LOCAL` en el middleware para compatibilidad con pgBouncer en modo transacción.

## Dependencias

| Dependencia | Versión | Propósito |
|---|---|---|
| opentelemetry-api | >=1.24.0 | API de tracing y métricas |
| opentelemetry-sdk | >=1.24.0 | SDK de OpenTelemetry |
| opentelemetry-instrumentation-fastapi | >=0.45b0 | Auto-instrumentación FastAPI |
| opentelemetry-instrumentation-celery | >=0.45b0 | Auto-instrumentación Celery |
| opentelemetry-instrumentation-sqlalchemy | >=0.45b0 | Auto-instrumentación SQLAlchemy |
| opentelemetry-instrumentation-redis | >=0.45b0 | Auto-instrumentación Redis |
| opentelemetry-instrumentation-httpx | >=0.45b0 | Auto-instrumentación httpx |
| opentelemetry-exporter-otlp | >=1.24.0 | Exportar traces a Jaeger/OTLP |
| opentelemetry-exporter-prometheus | >=0.45b0 | Exportar métricas a Prometheus |
| loguru | >=0.7.0 | Logging estructurado |
| prometheus-client | >=0.20.0 | Cliente Prometheus para /metrics |
| jaegertracing/all-in-one | 1.54 | Collector y UI de Jaeger (Docker) |
| awscli | latest | Upload de backups a S3/MinIO |
