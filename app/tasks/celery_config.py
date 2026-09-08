"""Configuracion de Celery — Plataforma SaaS Omnicanal Multi-Tenant.

Define colas, routing y configuracion del broker/backend.
6 colas especializadas con routing automatico por nombre de tarea.

Colas:
    - webhooks: Procesamiento de webhooks entrantes (c=4)
    - ai_inference: Inferencia LLM y agentes LangGraph (c=2)
    - documents: Ingestion de documentos y OCR (c=2)
    - notifications: Envio de notificaciones (c=2)
    - bulk: Operaciones masivas secuenciales (c=1)
    - lead_enrichment: Enriquecimiento asincrono de leads (c=2) — ADR-022
"""

import os

from celery import Celery
from kombu import Exchange, Queue

# ─── Instancia de Celery ────────────────────────────────────────────────────
celery_app = Celery("omnichannel")

# ─── Configuracion ──────────────────────────────────────────────────────────
celery_app.conf.update(
    # Broker y backend (Redis, separando DBs)
    broker_url=os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"),
    result_backend=os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/1"),

    # Serializacion segura
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,

    # Fiabilidad: ACK despues de ejecutar, re-encolar si worker muere
    task_acks_late=True,
    task_reject_on_worker_lost=True,

    # Prefetch: 1 tarea a la vez (importante para tareas largas de IA)
    worker_prefetch_multiplier=1,

    # Limites de reintento por defecto
    task_default_retry_delay=60,
    task_max_retries=3,

    # Resultados expiran en 1 hora
    result_expires=3600,

    # ─── Definicion de colas ────────────────────────────────────────────────
    task_queues=(
        Queue(
            "webhooks",
            Exchange("webhooks", type="direct"),
            routing_key="webhooks",
        ),
        Queue(
            "ai_inference",
            Exchange("ai_inference", type="direct"),
            routing_key="ai_inference",
        ),
        Queue(
            "documents",
            Exchange("documents", type="direct"),
            routing_key="documents",
        ),
        Queue(
            "notifications",
            Exchange("notifications", type="direct"),
            routing_key="notifications",
        ),
        Queue(
            "bulk",
            Exchange("bulk", type="direct"),
            routing_key="bulk",
        ),
        Queue(
            "lead_enrichment",
            Exchange("lead_enrichment", type="direct"),
            routing_key="lead_enrichment",
        ),
    ),

    # ─── Routing automatico por nombre de tarea ────────────────────────────
    task_routes={
        "app.tasks.webhook_*": {"queue": "webhooks"},
        "app.tasks.ai_*": {"queue": "ai_inference"},
        "app.tasks.document_*": {"queue": "documents"},
        "app.tasks.notification_*": {"queue": "notifications"},
        "app.tasks.bulk_*": {"queue": "bulk"},
        "app.tasks.enrichment_*": {"queue": "lead_enrichment"},
    },

    # Cola por defecto si no matchea ningun patron
    task_default_queue="webhooks",

    # ─── Beat schedule (tareas periodicas) ──────────────────────────────────
    beat_schedule={
        "auto-close-conversations": {
            "task": "app.tasks.bulk_auto_close_conversations",
            "schedule": 900.0,  # cada 15 minutos
            "options": {"queue": "bulk"},
        },
        "check-token-budgets": {
            "task": "app.tasks.notification_check_token_budgets",
            "schedule": 3600.0,  # cada hora
            "options": {"queue": "notifications"},
        },
        "recalculate-lead-scores": {
            "task": "app.tasks.enrichment_recalculate_scores",
            "schedule": 1800.0,  # cada 30 minutos
            "options": {"queue": "lead_enrichment"},
        },
        "check-stale-leads": {
            "task": "app.tasks.enrichment_check_stale_leads",
            "schedule": 3600.0,  # cada hora
            "options": {"queue": "lead_enrichment"},
        },
    },
)
