"""Avisos al equipo humano, encolados de forma perezosa.

`app/tasks/notifications.py` es entrega de Sprint 8 (`specs/sprint-08-addendum-ops.md`).
Mientras no exista, los nodos que quieren avisar a un agente o a un supervisor
registran el aviso en el log y siguen: la conversacion ya quedo escalada o la
respuesta ya quedo pendiente en la base, que es lo que no se puede perder.

Mismo patron que `webhook_processor._enqueue_ai_processing()` uso en Sprint 4 con
este mismo modulo de nodos: import dentro de la funcion y ImportError tratado
como "todavia no esta", no como fallo.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def enqueue_notification(task_attr: str, **kwargs: Any) -> bool:
    """Encola una notificacion del modulo `app.tasks.notifications`, si existe.

    Args:
        task_attr: Nombre de la tarea dentro del modulo (`notify_handoff`, ...).
        **kwargs: Argumentos de la tarea, ya serializables.

    Returns:
        True si la notificacion se encolo; False si el modulo o la tarea todavia
        no existen, o si el broker no acepto el mensaje.
    """
    try:
        # El ignore de abajo se quita cuando notifications.py entre en main (Sprint 8).
        from app.tasks import notifications  # type: ignore[attr-defined]
    except ImportError:
        logger.info(
            "notifications no disponible todavia (Sprint 8); aviso no encolado: %s %s",
            task_attr,
            kwargs,
        )
        return False

    task = getattr(notifications, task_attr, None)
    if task is None:
        logger.warning("app.tasks.notifications no expone %s; aviso no encolado", task_attr)
        return False

    try:
        task.delay(**kwargs)
    except Exception:
        # Un broker caido no puede deshacer un handoff que ya esta en la base.
        logger.exception("No se pudo encolar la notificacion %s", task_attr)
        return False
    return True
