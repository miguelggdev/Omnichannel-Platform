"""Logging estructurado con Loguru (Sprint 8, Dev A).

Configura un único destino de logs para los tres procesos (API, worker, beat) y
resuelve el problema de fondo: hasta este sprint el proyecto escribía con
`logging` de la stdlib (`logging.basicConfig()` en `app/main.py`) y con
`logger.info("...%s", x)`, mientras CLAUDE.md pide Loguru con `client_id` y
`trace_id` en cada línea. Reescribir ~40 módulos para cambiar de librería no
aporta nada; lo que hace falta es que esas líneas **salgan** por Loguru con el
contexto puesto.

Por eso `setup_logging()` no cambia ni una llamada del código existente:
instala un `InterceptHandler` en la raíz de `logging`, que reenvía cada record
de la stdlib a Loguru conservando módulo, función y línea reales. Todo lo que
ya se loguea —incluido lo que emiten uvicorn, SQLAlchemy, httpx y Celery—
aparece con el mismo formato y el mismo contexto.

El contexto (`trace_id`, `client_id`, `user_id`) lo pone
`ObservabilityMiddleware` en la API y `bind_task_context()` en las tareas de
Celery, vía `logger.contextualize()`, que es por tarea asíncrona y no se filtra
entre peticiones concurrentes.

Diferencias respecto del spec (§2):

- **Sin `logger.add(..., filter={"sqlalchemy": "WARNING"})`.** Ese `filter` de
  Loguru filtra por el `name` del *record de Loguru*, que para todo lo que pasa
  por el intercept es el módulo Python que llamó a `logging`; el diccionario
  del spec agregaba un segundo sink que duplicaba cada línea de WARNING para
  arriba. El silenciado de librerías ruidosas se hace donde corresponde, con
  `logging.getLogger("sqlalchemy.engine").setLevel(...)`.
- **Sin sink a `/var/log/app/app.log`.** Los contenedores del proyecto no
  montan ese volumen: el sink fallaría al crear el directorio, o peor, llenaría
  la capa de escritura del contenedor. Los logs salen por stdout, que es lo que
  recoge Docker.
"""

from __future__ import annotations

import inspect
import json
import logging
import sys
from typing import TYPE_CHECKING, Any

from loguru import logger

from app.core.config import get_settings

if TYPE_CHECKING:
    from types import FrameType

    from loguru import Message, Record

# Librerías que hablan de más en INFO/DEBUG y no aportan al diagnóstico.
_NIVELES_POR_LIBRERIA = {
    "sqlalchemy.engine": logging.WARNING,
    "sqlalchemy.pool": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "urllib3": logging.WARNING,
    "asyncio": logging.WARNING,
    "opentelemetry": logging.WARNING,
}

_CAMPOS_DE_CONTEXTO = ("trace_id", "client_id", "user_id")

_configurado = False


class InterceptHandler(logging.Handler):
    """Reenvía los records de `logging` (stdlib) a Loguru.

    Recorre la pila hasta salir de `logging` para que el módulo, la función y
    la línea que aparecen en el log sean los del código que llamó, no los del
    propio handler.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Traduce un record de la stdlib y lo emite por Loguru.

        Args:
            record: Record generado por `logging`.
        """
        try:
            nivel: str | int = logger.level(record.levelname).name
        except ValueError:
            nivel = record.levelno

        # `inspect.currentframe()` y no `logging.currentframe()`: el segundo es
        # `sys._getframe(3)`, un salto fijo que desde aqui aterriza dentro del
        # propio `logging` y deja todos los logs atribuidos a `callHandlers`.
        frame: FrameType | None = inspect.currentframe()
        profundidad = 0
        while frame and (profundidad == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            profundidad += 1

        logger.opt(depth=profundidad, exception=record.exc_info).log(nivel, record.getMessage())


def _serializar(record: Record) -> str:
    """Convierte un record de Loguru a una línea JSON.

    Args:
        record: Record de Loguru.

    Returns:
        El JSON de una sola línea que se escribe a stdout.
    """
    salida: dict[str, Any] = {
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "logger": record["name"],
        "message": record["message"],
        "module": record["module"],
        "function": record["function"],
        "line": record["line"],
    }
    for campo in _CAMPOS_DE_CONTEXTO:
        salida[campo] = str(record["extra"].get(campo, "") or "")

    # Campos extra propios de la llamada (los que no son contexto estandar).
    adicionales = {
        clave: valor for clave, valor in record["extra"].items() if clave not in _CAMPOS_DE_CONTEXTO
    }
    if adicionales:
        salida["extra"] = adicionales

    excepcion = record["exception"]
    if excepcion is not None and excepcion.type is not None:
        salida["exception"] = {
            "type": excepcion.type.__name__,
            "value": str(excepcion.value),
        }

    return json.dumps(salida, default=str, ensure_ascii=False)


def _sink_json(mensaje: Message) -> None:
    """Escribe el record serializado a stdout.

    Args:
        mensaje: Mensaje de Loguru con su record adjunto.
    """
    sys.stdout.write(_serializar(mensaje.record) + "\n")


def setup_logging(force: bool = False) -> None:
    """Configura Loguru y redirige `logging` de la stdlib hacia él.

    Idempotente: llamarla dos veces en el mismo proceso no duplica los sinks.

    Args:
        force: Reconfigurar aunque ya se haya configurado (útil en tests).
    """
    global _configurado
    if _configurado and not force:
        return

    settings = get_settings()
    nivel = settings.LOG_LEVEL.upper()

    logger.remove()
    if settings.LOG_FORMAT.lower() == "json":
        logger.add(_sink_json, level=nivel)
    else:
        logger.add(
            sys.stderr,
            level=nivel,
            format=(
                "<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
                "<cyan>{extra[trace_id]}</cyan> | <magenta>{extra[client_id]}</magenta> | "
                "<dim>{name}:{line}</dim> | {message}"
            ),
        )

    # Valores por defecto del contexto: sin esto, el `format` de consola revienta
    # con KeyError en cualquier log emitido fuera de un request.
    logger.configure(extra=dict.fromkeys(_CAMPOS_DE_CONTEXTO, ""))

    logging.root.handlers = [InterceptHandler()]
    logging.root.setLevel(nivel)
    for nombre, nivel_libreria in _NIVELES_POR_LIBRERIA.items():
        logging.getLogger(nombre).setLevel(nivel_libreria)
    # uvicorn instala sus propios handlers: hay que vaciarlos o cada linea sale
    # dos veces, una con formato de uvicorn y otra con el de Loguru.
    for nombre in ("uvicorn", "uvicorn.error", "uvicorn.access", "celery", "celery.app.trace"):
        logging_logger = logging.getLogger(nombre)
        logging_logger.handlers = []
        logging_logger.propagate = True

    _configurado = True


def bind_task_context(client_id: str | None = None, trace_id: str | None = None) -> Any:
    """Contexto de logging para una tarea de Celery.

    Args:
        client_id: Tenant de la tarea, si se conoce.
        trace_id: Traza activa. Si no se pasa, se toma del span en curso.

    Returns:
        Context manager de `logger.contextualize()` para envolver la tarea.
    """
    from app.core.telemetry import get_trace_id

    return logger.contextualize(
        trace_id=trace_id if trace_id is not None else get_trace_id(),
        client_id=str(client_id or ""),
        user_id="",
    )
