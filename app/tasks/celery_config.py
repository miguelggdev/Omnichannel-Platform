"""Configuracion de Celery — Plataforma SaaS Omnicanal Multi-Tenant.

Define colas, routing y configuracion del broker/backend.
7 colas especializadas con routing automatico por nombre de tarea.

Colas:
    - webhooks: Procesamiento de webhooks entrantes (c=4)
    - ai_inference: Inferencia LLM y agentes LangGraph (c=2)
    - documents: Ingestion de documentos y OCR (c=2)
    - notifications: Envio de notificaciones (c=2)
    - bulk: Operaciones masivas secuenciales (c=1)
    - lead_enrichment: Enriquecimiento asincrono de leads (c=2) — ADR-022
    - media: Transcripcion de audio con Whisper (c=2) — ADR-060
"""

import os

from celery import Celery
from celery.schedules import crontab
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
        Queue(
            "media",
            Exchange("media", type="direct"),
            routing_key="media",
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
        "app.tasks.media_*": {"queue": "media"},
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
        "daily-backup": {
            "task": "app.tasks.bulk_run_backup",
            "schedule": crontab(hour=3, minute=0),  # 03:00 UTC
            "options": {"queue": "bulk"},
        },
        "monthly-restore-test": {
            "task": "app.tasks.bulk_run_restore_test",
            # Dia 1 de cada mes a las 04:00 UTC, una hora despues del backup
            # diario: asi la prueba corre sobre un dump recien subido.
            "schedule": crontab(day_of_month="1", hour=4, minute=0),
            "options": {"queue": "bulk"},
        },
        "expire-csat-surveys": {
            "task": "app.tasks.bulk_expire_csat_surveys",
            "schedule": 3600.0,  # cada hora
            "options": {"queue": "bulk"},
        },
        "purge-outgoing-webhook-logs": {
            "task": "app.tasks.bulk_purge_outgoing_webhook_logs",
            # Domingo 05:00 UTC, fuera de la ventana del backup diario (03:00).
            "schedule": crontab(day_of_week="sun", hour=5, minute=0),
            "options": {"queue": "bulk"},
        },
        # Cada entrada de aqui tiene que apuntar a una tarea que exista: Beat
        # publica el mensaje igual, y un worker que no la conoce lo rechaza
        # como "unregistered task" en cada ciclo, para siempre, sin que nada
        # falle de forma visible (`tests/unit/test_celery_config.py` lo vigila).
        # Sprint 2 dejo aqui tres entradas de features que aun no existen y que
        # se retiraron: `check-token-budgets` (avisos de presupuesto por
        # `notifications`), `recalculate-lead-scores` y `check-stale-leads`
        # (Lead Management, Sprints 16-19). Se vuelven a agregar cuando se
        # entregue la tarea correspondiente, no antes.
    },
)
