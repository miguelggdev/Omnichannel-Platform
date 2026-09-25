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
    curl \
    && rm -rf /var/lib/apt/lists/*

# Instalar dependencias Python en prefijo separado
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Cross-encoder de re-ranking del RAG (Sprint 12), en la etapa de build: un
# contenedor de produccion no sale a internet a buscar pesos la primera vez que
# alguien pregunta algo. Si la descarga falla, la imagen se construye igual y el
# re-ranking queda desactivado — `services/reranker.py` degrada al orden de los
# embeddings en vez de tumbar el RAG.
ARG RERANKER_REPO=cross-encoder/ms-marco-MiniLM-L-6-v2
ARG RERANKER_BASE=https://huggingface.co/${RERANKER_REPO}/resolve/main
RUN mkdir -p /install/models/reranker \
    && { curl -fsSL -o /install/models/reranker/model.onnx \
            "${RERANKER_BASE}/onnx/model.onnx" \
         && curl -fsSL -o /install/models/reranker/tokenizer.json \
            "${RERANKER_BASE}/tokenizer.json"; } \
    || { echo "AVISO: sin cross-encoder; el re-ranking quedara desactivado" \
         && rm -f /install/models/reranker/*; }

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

# El cross-encoder viaja dentro de /install; queda en la ruta que espera
# RERANKER_MODEL_PATH.
RUN mkdir -p /app/models/reranker \
    && cp -r /usr/local/models/reranker/. /app/models/reranker/ \
    || echo "AVISO: imagen sin cross-encoder; el re-ranking quedara desactivado"

# Crear usuario no-root para seguridad
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

# Copiar codigo de la aplicacion
COPY ./app ./app
COPY ./migrations ./migrations

# Permisos del usuario no-root
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Healthcheck interno del contenedor
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=40s \
    CMD curl -f http://localhost:8000/internal/health || exit 1

# Uvicorn con app factory pattern
# Workers ajustables via variable de entorno WEB_CONCURRENCY
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--ws-max-size", "65536"]
