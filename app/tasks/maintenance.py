"""Tareas de mantenimiento: backup diario y prueba de restauracion mensual.

Las dos son envoltorios finos sobre los scripts de `scripts/`. La logica vive en
bash a proposito: `pg_dump`, `pg_restore` y el CLI de `aws` son herramientas de
linea de comandos, y reimplementarlas desde Python solo agregaria una capa que
puede fallar por su cuenta. Lo que aporta Celery es el calendario, el registro
del resultado y el aviso cuando algo sale mal.

Nombres de las tareas
---------------------
`app.tasks.bulk_run_backup` y `app.tasks.bulk_run_restore_test`, no los
`app.tasks.maintenance.*` de la spec §7.3: `celery_config.py` (Sprint 2) enruta
por prefijo de nombre (`app.tasks.bulk_* -> cola bulk`) y no existe ninguna cola
`maintenance`. Con el nombre de la spec estas tareas caerian en la cola por
defecto (`webhooks`), compitiendo con los mensajes entrantes contra un worker
que tiene `worker_prefetch_multiplier=1`: un backup de una hora bloquearia la
recepcion de mensajes durante esa hora.
"""

import logging
import subprocess  # nosec B404 — se invoca con lista fija, sin shell
from pathlib import Path
from typing import Any

from app.tasks.celery_config import celery_app

logger = logging.getLogger(__name__)

# Los scripts viven junto al codigo; en la imagen Docker el proyecto se copia a
# /app, asi que esta ruta relativa al modulo vale igual en local y en el
# contenedor.
SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"

BACKUP_SCRIPT = SCRIPTS_DIR / "backup.sh"
RESTORE_TEST_SCRIPT = SCRIPTS_DIR / "restore_test.sh"

# Un backup grande puede tardar; la prueba de restauracion, mas todavia, porque
# crea una base entera. Los limites son generosos pero acotados: una tarea
# colgada para siempre ocuparia el unico worker de la cola `bulk`.
BACKUP_TIMEOUT = 3600
RESTORE_TEST_TIMEOUT = 7200

# Cuanto stdout/stderr se conserva en el resultado y en el log. Un pg_restore
# con --verbose escupe miles de lineas; las utiles para diagnosticar son las
# ultimas.
MAX_SALIDA = 4000


def _ejecutar(script: Path, timeout: int) -> dict[str, Any]:
    """Corre un script de mantenimiento y devuelve su resultado.

    Args:
        script: Ruta absoluta del script.
        timeout: Segundos maximos de ejecucion.

    Returns:
        Dict con el codigo de salida y la cola de la salida estandar.

    Raises:
        RuntimeError: Si el script no existe, termina con error o se pasa del
            tiempo. Se levanta a proposito: Celery marca la tarea como fallida y
            el fallo queda visible, en vez de un backup que "termino" sin hacer
            nada.
    """
    if not script.is_file():
        raise RuntimeError(f"No existe el script de mantenimiento: {script}")

    try:
        resultado = subprocess.run(  # noqa: S603 — lista fija, sin shell
            ["/bin/bash", str(script)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{script.name} supero el limite de {timeout}s y se cancelo") from exc

    salida = (resultado.stdout or "")[-MAX_SALIDA:]
    errores = (resultado.stderr or "")[-MAX_SALIDA:]

    if resultado.returncode != 0:
        logger.error("%s fallo (codigo %s). stderr: %s", script.name, resultado.returncode, errores)
        raise RuntimeError(f"{script.name} fallo con codigo {resultado.returncode}: {errores}")

    logger.info("%s completado correctamente", script.name)
    return {"script": script.name, "returncode": resultado.returncode, "output": salida}


@celery_app.task(
    name="app.tasks.bulk_run_backup",
    queue="bulk",
    acks_late=True,
    time_limit=BACKUP_TIMEOUT + 120,
    soft_time_limit=BACKUP_TIMEOUT + 60,
)
def run_backup() -> dict[str, Any]:
    """Ejecuta el backup diario de la base.

    No reintenta: si el backup falla, reintentarlo en el acto casi siempre falla
    igual (credenciales, disco, S3 caido) y ademas el siguiente pase es en 24 h.
    Lo que hace falta es que el fallo se vea, y para eso basta con que la tarea
    quede marcada como fallida.

    Returns:
        Codigo de salida y cola de la salida del script.

    Raises:
        RuntimeError: Si el script falla o se pasa de tiempo.
    """
    return _ejecutar(BACKUP_SCRIPT, BACKUP_TIMEOUT)


@celery_app.task(
    name="app.tasks.bulk_run_restore_test",
    queue="bulk",
    acks_late=True,
    time_limit=RESTORE_TEST_TIMEOUT + 120,
    soft_time_limit=RESTORE_TEST_TIMEOUT + 60,
)
def run_restore_test() -> dict[str, Any]:
    """Restaura el ultimo backup en una base desechable y lo verifica.

    Returns:
        Codigo de salida y cola de la salida del script.

    Raises:
        RuntimeError: Si la restauracion o alguna comprobacion falla.
    """
    return _ejecutar(RESTORE_TEST_SCRIPT, RESTORE_TEST_TIMEOUT)
