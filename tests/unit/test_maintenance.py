"""Tests de las tareas de mantenimiento (backup y prueba de restauracion).

No se ejecuta ningun script de verdad: `subprocess.run` se sustituye. Lo que se
comprueba es el contrato que rodea a los scripts — nombre y cola de la tarea,
que un fallo se propague en vez de darse por bueno, y que el timeout no quede
colgado para siempre.
"""

import subprocess
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from app.tasks import maintenance as maintenance_module
from app.tasks.celery_config import celery_app

TAREAS = ("app.tasks.bulk_run_backup", "app.tasks.bulk_run_restore_test")


class _Resultado:
    """Sustituto de `subprocess.CompletedProcess`."""

    def __init__(self, returncode: int = 0, stdout: str = "ok", stderr: str = "") -> None:
        """Guarda lo que devolveria el proceso."""
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def run_espia(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Sustituye `subprocess.run` y registra como se le llamo."""
    registro: dict[str, Any] = {"llamadas": [], "resultado": _Resultado(), "excepcion": None}

    def _run(cmd: list[str], **kwargs: Any) -> _Resultado:
        registro["llamadas"].append({"cmd": cmd, **kwargs})
        if registro["excepcion"] is not None:
            raise registro["excepcion"]
        return registro["resultado"]  # type: ignore[no-any-return]

    monkeypatch.setattr(maintenance_module.subprocess, "run", _run)
    # Los scripts existen en el repo, pero el test no debe depender de eso.
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    return registro


# ─── Registro en Celery ──────────────────────────────────────────────────────


class TestRegistro:
    """Nombre, cola y entradas de Beat."""

    @pytest.mark.parametrize("nombre", TAREAS)
    def test_las_tareas_estan_registradas(self, nombre: str) -> None:
        """El worker las conoce al importar el modulo."""
        import app.tasks.maintenance  # noqa: F401 — el import es lo que registra

        assert nombre in celery_app.tasks

    @pytest.mark.parametrize("nombre", TAREAS)
    def test_van_a_la_cola_bulk(self, nombre: str) -> None:
        """Un backup de una hora no puede bloquear la cola de webhooks.

        Con el nombre `app.tasks.maintenance.*` de la spec §7.3 estas tareas
        caerian en `task_default_queue` (webhooks), contra un worker con
        `worker_prefetch_multiplier=1`.
        """
        import app.tasks.maintenance  # noqa: F401

        assert celery_app.tasks[nombre].queue == "bulk"

    def test_el_modulo_esta_en_task_modules(self) -> None:
        """Sin esto el worker levanta sin conocer las tareas (la leccion de BUG-014)."""
        from app.tasks.celery_app import TASK_MODULES

        assert "app.tasks.maintenance" in TASK_MODULES

    def test_beat_programa_el_backup_diario(self) -> None:
        """Una vez al dia, de madrugada."""
        entrada = celery_app.conf.beat_schedule["daily-backup"]
        assert entrada["task"] == "app.tasks.bulk_run_backup"
        assert entrada["options"]["queue"] == "bulk"

    def test_beat_programa_la_prueba_de_restauracion(self) -> None:
        """Una vez al mes, despues del backup diario."""
        entrada = celery_app.conf.beat_schedule["monthly-restore-test"]
        assert entrada["task"] == "app.tasks.bulk_run_restore_test"
        assert entrada["options"]["queue"] == "bulk"

    def test_la_restauracion_corre_despues_del_backup(self) -> None:
        """La prueba debe encontrar un dump recien subido, no el de ayer."""
        backup = celery_app.conf.beat_schedule["daily-backup"]["schedule"]
        restore = celery_app.conf.beat_schedule["monthly-restore-test"]["schedule"]
        # `crontab.hour` es un set de horas, no un entero.
        assert min(backup.hour) < min(restore.hour)


# ─── Los scripts existen ─────────────────────────────────────────────────────


class TestScripts:
    """Las rutas que las tareas van a ejecutar."""

    @pytest.mark.parametrize(
        "script", [maintenance_module.BACKUP_SCRIPT, maintenance_module.RESTORE_TEST_SCRIPT]
    )
    def test_el_script_existe_en_el_repo(self, script: Path) -> None:
        """Una ruta mal calculada solo se descubriria en la primera ejecucion real."""
        assert script.is_file(), f"no existe {script}"

    @pytest.mark.parametrize(
        "script", [maintenance_module.BACKUP_SCRIPT, maintenance_module.RESTORE_TEST_SCRIPT]
    )
    def test_el_script_aborta_ante_el_primer_error(self, script: Path) -> None:
        """`set -euo pipefail`: sin eso, un `pg_dump` fallido seguiria hasta el
        `aws s3 cp` y subiria un archivo incompleto como si fuera un backup."""
        assert "set -euo pipefail" in script.read_text(encoding="utf-8")


# ─── Ejecucion ───────────────────────────────────────────────────────────────


class TestEjecucion:
    """Que hace la tarea con el resultado del script."""

    def test_el_backup_invoca_su_script_con_bash(self, run_espia: dict[str, Any]) -> None:
        """Se llama con lista fija, sin shell."""
        maintenance_module.run_backup()

        llamada = run_espia["llamadas"][0]
        assert llamada["cmd"][0] == "/bin/bash"
        assert llamada["cmd"][1].endswith("backup.sh")
        assert llamada["timeout"] == maintenance_module.BACKUP_TIMEOUT

    def test_la_restauracion_invoca_su_script(self, run_espia: dict[str, Any]) -> None:
        """Y con su propio timeout, mas largo."""
        maintenance_module.run_restore_test()

        llamada = run_espia["llamadas"][0]
        assert llamada["cmd"][1].endswith("restore_test.sh")
        assert llamada["timeout"] == maintenance_module.RESTORE_TEST_TIMEOUT
        assert maintenance_module.RESTORE_TEST_TIMEOUT > maintenance_module.BACKUP_TIMEOUT

    def test_un_script_que_falla_levanta(self, run_espia: dict[str, Any]) -> None:
        """Un backup que "termina" sin hacer nada es peor que uno que falla.

        Si no se levantara, Celery marcaria la tarea como exitosa y nadie se
        enteraria hasta el dia que hiciera falta restaurar.
        """
        run_espia["resultado"] = _Resultado(returncode=2, stderr="pg_dump: connection refused")

        with pytest.raises(RuntimeError, match="codigo 2"):
            maintenance_module.run_backup()

    def test_el_error_del_script_llega_al_mensaje(self, run_espia: dict[str, Any]) -> None:
        """El motivo tiene que estar en la excepcion para poder diagnosticar."""
        run_espia["resultado"] = _Resultado(returncode=1, stderr="no such bucket")

        with pytest.raises(RuntimeError, match="no such bucket"):
            maintenance_module.run_backup()

    def test_un_timeout_levanta_con_motivo(self, run_espia: dict[str, Any]) -> None:
        """Un backup colgado ocuparia para siempre el unico worker de `bulk`."""
        run_espia["excepcion"] = subprocess.TimeoutExpired(cmd="backup.sh", timeout=10)

        with pytest.raises(RuntimeError, match="supero el limite"):
            maintenance_module.run_backup()

    def test_si_falta_el_script_levanta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Una imagen mal construida se detecta en la primera ejecucion."""
        monkeypatch.setattr(Path, "is_file", lambda self: False)

        with pytest.raises(RuntimeError, match="No existe el script"):
            maintenance_module.run_backup()

    def test_la_salida_se_recorta(self, run_espia: dict[str, Any]) -> None:
        """Un `pg_restore` verboso escupe miles de lineas; se guardan las ultimas."""
        run_espia["resultado"] = _Resultado(stdout="x" * 10_000)

        resultado = maintenance_module.run_backup()

        assert len(resultado["output"]) == maintenance_module.MAX_SALIDA

    def test_devuelve_el_resultado_del_script(self, run_espia: dict[str, Any]) -> None:
        """El exito reporta que script corrio y con que salida."""
        run_espia["resultado"] = _Resultado(stdout="Backup completado correctamente.")

        resultado = maintenance_module.run_backup()

        assert resultado["script"] == "backup.sh"
        assert resultado["returncode"] == 0
        assert "Backup completado" in resultado["output"]


# ─── Purga de outgoing_webhook_logs (Sprint 11, Dev B) ───────────────────────


class TestRegistroDePurga:
    """Nombre, cola y entrada de Beat de la purga de logs de webhooks salientes."""

    def test_esta_registrada(self) -> None:
        assert "app.tasks.bulk_purge_outgoing_webhook_logs" in celery_app.tasks

    def test_va_a_la_cola_bulk(self) -> None:
        assert celery_app.tasks["app.tasks.bulk_purge_outgoing_webhook_logs"].queue == "bulk"

    def test_beat_la_programa_fuera_de_la_ventana_del_backup(self) -> None:
        purga = celery_app.conf.beat_schedule["purge-outgoing-webhook-logs"]
        backup = celery_app.conf.beat_schedule["daily-backup"]["schedule"]
        assert purga["task"] == "app.tasks.bulk_purge_outgoing_webhook_logs"
        assert purga["options"]["queue"] == "bulk"
        assert min(purga["schedule"].hour) != min(backup.hour)


class _ResultadoRowcount:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _SesionDePurga:
    def __init__(self, filas: int) -> None:
        self._filas = filas

    async def execute(self, *_a: object, **_k: object) -> _ResultadoRowcount:
        return _ResultadoRowcount(self._filas)


def _tenant_session_por_tenant(por_tenant: dict[object, int]):
    @asynccontextmanager
    async def _cm(client_id: object, user_id: object = None):
        yield _SesionDePurga(por_tenant[client_id])

    return _cm


def _fake_load(client_ids: list) -> Any:
    async def _load() -> list:
        return client_ids

    return _load


class TestPurgarLogs:
    """Orquestacion de `_purgar_outgoing_webhook_logs`: recorrido por tenant."""

    @pytest.mark.asyncio
    async def test_suma_las_filas_borradas_de_cada_tenant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
        monkeypatch.setattr(
            maintenance_module, "_load_active_client_ids", _fake_load([tenant_a, tenant_b])
        )
        monkeypatch.setattr(
            maintenance_module,
            "tenant_session",
            _tenant_session_por_tenant({tenant_a: 5, tenant_b: 2}),
        )

        resultado = await maintenance_module._purgar_outgoing_webhook_logs()

        assert resultado == {"deleted": 7, "tenants": 2, "tenants_failed": 0}

    @pytest.mark.asyncio
    async def test_un_tenant_con_error_no_corta_el_recorrido(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
        monkeypatch.setattr(
            maintenance_module, "_load_active_client_ids", _fake_load([tenant_a, tenant_b])
        )

        @asynccontextmanager
        async def _falla_en_a(client_id: object, user_id: object = None):
            if client_id == tenant_a:
                raise RuntimeError("conexion perdida")
            yield _SesionDePurga(3)

        monkeypatch.setattr(maintenance_module, "tenant_session", _falla_en_a)

        resultado = await maintenance_module._purgar_outgoing_webhook_logs()

        assert resultado == {"deleted": 3, "tenants": 2, "tenants_failed": 1}
