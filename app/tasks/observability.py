"""Observabilidad de los workers de Celery (Sprint 8, Dev A).

Engancha, vía señales de Celery, las tres piezas que en la API instala
`app/main.py`: logging estructurado, tracing distribuido y métricas.

Dónde corre cada cosa importa, y no es intercambiable:

- `worker_init` (**proceso maestro**, una vez): configura Loguru y levanta el
  servidor HTTP de métricas. Tiene que ser el maestro porque sus hijos prefork
  no pueden escuchar todos en el mismo puerto; el maestro agrega lo que los
  hijos escriben en `PROMETHEUS_MULTIPROC_DIR`.
- `worker_process_init` (**cada hijo prefork**): instrumenta OpenTelemetry. El
  `TracerProvider` usa hilos de fondo (el `BatchSpanProcessor`) y un `fork()`
  no los hereda: instrumentar en el maestro deja a cada hijo con un exportador
  cuyo hilo no existe, y ni un solo span sale del worker.
- `task_prerun` / `task_postrun`: contexto de log por tarea y métricas de
  duración y estado.

`celery_config.py` (Sprint 2) no importa este módulo: lo hace
`app/tasks/celery_app.py`, que es el punto donde el worker registra lo suyo.
"""

from __future__ import annotations

import logging
from typing import Any

from celery.signals import (
    task_failure,
    task_postrun,
    task_prerun,
    worker_init,
    worker_process_init,
)

from app.core.logging import bind_task_context, setup_logging
from app.core.metrics import (
    celery_task_duration_seconds,
    celery_tasks_total,
    start_worker_metrics_server,
)

logger = logging.getLogger(__name__)

# Contexto vivo por tarea: {task_id: (context_manager, inicio_monotonico)}.
# Se indexa por task_id y no por variable de modulo porque un worker con
# concurrencia > 1 y pool de hilos puede tener varias tareas a la vez.
_en_curso: dict[str, tuple[Any, float]] = {}


def _cola_de(task: Any) -> str:
    """Devuelve la cola por la que llegó una tarea.

    Args:
        task: Instancia de la tarea de Celery.

    Returns:
        Nombre de la cola, o `"desconocida"` si el mensaje no la trae.
    """
    solicitud = getattr(task, "request", None)
    entrega = getattr(solicitud, "delivery_info", None) or {}
    return str(entrega.get("routing_key") or "desconocida")


@worker_init.connect
def _configurar_worker(**_: Any) -> None:
    """Configura logging y expone las métricas del worker (proceso maestro)."""
    setup_logging()
    start_worker_metrics_server()


@worker_process_init.connect
def _instrumentar_proceso(**_: Any) -> None:
    """Configura logging y OpenTelemetry (Celery, SQL, Redis, httpx) en cada hijo prefork."""
    from app.core.database import engine
    from app.core.telemetry import setup_celery_telemetry, setup_telemetry

    # force=True: `_configurado` ya es True en el hijo porque el fork copia la
    # memoria del maestro, donde `worker_init` ya llamo a `setup_logging()`. Sin
    # forzar, esta llamada seria un no-op y el hijo heredaria un sink
    # `enqueue=True` cuyo hilo de fondo NO sobrevive al fork — los logs de cada
    # worker quedarian encolados sin nadie que los escriba.
    setup_logging(force=True)
    # Ademas de Celery: las queries (engine de SQLAlchemy), Redis y las llamadas
    # salientes (httpx: YCloud, OpenAI) ocurren casi todas en el worker. Sin esto
    # solo la API generaba esos spans y la traza de un mensaje se cortaba en
    # cuanto la tarea empezaba a trabajar. Es idempotente y va post-fork por lo
    # mismo que la de Celery: el exportador usa hilos que un fork no hereda.
    setup_telemetry(engine=engine)
    setup_celery_telemetry()


@task_prerun.connect
def _al_empezar(task_id: str | None = None, task: Any = None, **kwargs: Any) -> None:
    """Abre el contexto de log y arranca el cronómetro de la tarea.

    Args:
        task_id: Identificador de la tarea.
        task: Instancia de la tarea.
        **kwargs: Resto de la señal de Celery (incluye `args` y `kwargs`).
    """
    import time

    if task_id is None:
        return
    # La convencion del proyecto es que el client_id viaja como primer
    # argumento o como kwarg homonimo; si no aparece, el contexto queda vacio
    # en vez de inventarse un tenant.
    argumentos = kwargs.get("kwargs") or {}
    client_id = argumentos.get("client_id")
    contexto = bind_task_context(client_id=client_id)
    contexto.__enter__()
    _en_curso[task_id] = (contexto, time.monotonic())


def _estado_efectivo(state: str | None, retval: Any) -> str:
    """Distingue un éxito real de un mensaje enviado a la DLQ.

    `process_incoming_message` no relanza la excepción cuando agota los
    reintentos: escribe en la DLQ y devuelve `{"status": "dlq"}`, así que para
    Celery la tarea terminó en `SUCCESS`. Contarla así ocultaba en el dashboard
    y en las alertas un mensaje abandonado que alguien tiene que revisar.

    Args:
        state: Estado que reporta Celery.
        retval: Valor de retorno de la tarea.

    Returns:
        `"DLQ"` si la tarea devolvió `{"status": "dlq"}`; si no, el estado tal cual.
    """
    if isinstance(retval, dict) and retval.get("status") == "dlq":
        return "DLQ"
    return str(state or "UNKNOWN")


@task_postrun.connect
def _al_terminar(
    task_id: str | None = None,
    task: Any = None,
    state: str | None = None,
    retval: Any = None,
    **_: Any,
) -> None:
    """Cierra el contexto de log y registra duración y estado.

    Args:
        task_id: Identificador de la tarea.
        task: Instancia de la tarea.
        state: Estado final (`SUCCESS`, `FAILURE`, `RETRY`, ...).
        retval: Valor de retorno de la tarea (para detectar la DLQ).
        **_: Resto de la señal.
    """
    import time

    if task_id is None:
        return
    entrada = _en_curso.pop(task_id, None)
    nombre = getattr(task, "name", "desconocida")
    cola = _cola_de(task)

    if entrada is not None:
        contexto, inicio = entrada
        try:
            celery_task_duration_seconds.labels(nombre, cola).observe(time.monotonic() - inicio)
        except Exception:  # pragma: no cover — defensivo
            logger.debug("No se pudo medir la duracion de %s", nombre, exc_info=True)
        finally:
            contexto.__exit__(None, None, None)

    try:
        celery_tasks_total.labels(nombre, cola, _estado_efectivo(state, retval)).inc()
    except Exception:  # pragma: no cover — defensivo
        logger.debug("No se pudo contar la tarea %s", nombre, exc_info=True)


@task_failure.connect
def _al_fallar(
    task_id: str | None = None, exception: BaseException | None = None, **kwargs: Any
) -> None:
    """Deja constancia del fallo con el contexto de la tarea todavía abierto.

    Args:
        task_id: Identificador de la tarea.
        exception: Excepción que tumbó la tarea.
        **kwargs: Resto de la señal (incluye `sender`).
    """
    remitente = kwargs.get("sender")
    logger.error(
        "Tarea fallida: %s (task_id=%s): %s",
        getattr(remitente, "name", "desconocida"),
        task_id,
        exception,
    )
