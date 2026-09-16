"""Registro de tareas de Celery — que `TASK_MODULES` no se quede corto.

Ya paso una vez, en Sprint 4 (ver PROGRESS.md): un modulo de tarea nuevo sin
sumar a `TASK_MODULES` deja al worker levantando la cola sin conocer ninguna
tarea, y todo lo que se encole ahi queda `NotRegistered` en silencio. Sprint 6
lo repitio con `app/tasks/ai_processor.py`. Este test verifica la lista
completa para que un cuarto modulo nuevo no vuelva a faltar.
"""

from app.tasks.celery_app import celery_app

# Nombre de tarea (el `name=` de cada `@shared_task`), no el nombre del modulo:
# es lo que efectivamente busca Celery al recibir un mensaje de la cola.
TAREAS_ESPERADAS = frozenset(
    {
        "app.tasks.webhook_process_incoming",
        "app.tasks.document_ingest",
        "app.tasks.ai_process_response",
    }
)


def test_todas_las_tareas_conocidas_quedan_registradas() -> None:
    """Importar `TASK_MODULES` debe registrar las tareas de los tres workers."""
    celery_app.loader.import_default_modules()

    faltantes = TAREAS_ESPERADAS - set(celery_app.tasks.keys())

    assert not faltantes, f"Tareas no registradas (falta su modulo en TASK_MODULES): {faltantes}"
