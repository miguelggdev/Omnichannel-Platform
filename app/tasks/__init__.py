"""Paquete de tareas de Celery.

Importar `app.tasks.celery_app` aqui garantiza que el registro de tareas ocurra
sea cual sea el modulo que se pase en `-A` (docker-compose usa `celery_config`).
"""

from app.tasks.celery_app import celery_app

__all__ = ["celery_app"]
