# =============================================================================
# Dockerfile — Plataforma SaaS Omnicanal Multi-Tenant
# Multi-stage build: builder (compilacion) → runner (produccion)
# =============================================================================

# ============ BUILDER ============
FROM python:3.12-slim AS builder

WORKDIR /build

# Dependencias del sistema para compilacion de paquetes nativos
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Instalar dependencias Python en prefijo separado
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ============ RUNNER ============
FROM python:3.12-slim AS runner

WORKDIR /app

# Dependencias de runtime:
# - libpq5: cliente PostgreSQL (asyncpg)
# - tesseract-ocr + tesseract-ocr-spa: OCR para pipeline de documentos (Sprint 5)
# - curl: healthcheck del contenedor
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    tesseract-ocr \
    tesseract-ocr-spa \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copiar dependencias Python compiladas desde builder
COPY --from=builder /install /usr/local

# Crear usuario no-root para seguridad
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

# Copiar codigo de la aplicacion
COPY ./app ./app
COPY ./migrations ./migrations
COPY ./alembic.ini .

# Permisos del usuario no-root
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Healthcheck interno del contenedor
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=40s \
    CMD curl -f http://localhost:8000/internal/health || exit 1

# Uvicorn con app factory pattern
# Workers ajustables via variable de entorno WEB_CONCURRENCY
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
