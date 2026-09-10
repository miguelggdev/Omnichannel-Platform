"""Punto de entrada de Celery y registro de tareas.

`celery_config.py` (Dev A, Sprint 2) define la instancia, las 6 colas y el routing.
Este modulo la reexporta y declara que modulos de tareas debe importar el worker:
sin ese `imports`, `celery -A app.tasks.celery_config worker` levanta las colas pero
no conoce ninguna tarea, y todo lo que encole el endpoint queda sin consumidor.

Ambos caminos funcionan como `-A`:
    celery -A app.tasks.celery_config worker -Q webhooks   (el de docker-compose.yml)
    celery -A app.tasks.celery_app    worker -Q webhooks
porque `app/tasks/__init__.py` importa este modulo al cargar el paquete.
"""

from app.tasks.celery_config import celery_app

# Modulos que el worker debe importar al arrancar para registrar sus tareas.
TASK_MODULES = ("app.tasks.webhook_processor",)

celery_app.conf.update(imports=TASK_MODULES)

__all__ = ["TASK_MODULES", "celery_app"]
