"""`app/core/database.py::run_isolated` — engine y Redis compartidos entre `asyncio.run()`.

BUG-006 (ver MEMORY.md) encontro que un engine async de SQLAlchemy queda atado
al event loop donde se creo. Los workers de Celery llaman `asyncio.run()` una
vez por tarea, y un worker prefork procesa muchas tareas seguidas en el mismo
proceso: sin disponer el pool al final de cada llamada, la siguiente tarea
reutilizaria conexiones abiertas en un loop ya cerrado.

BUG-021 (ver MEMORY.md): el mismo riesgo existia para el cliente Redis de
`app/services/dedup.py` (tambien un singleton de modulo) y nunca se limpiaba.

Estos tests verifican el contrato de `run_isolated()` con dobles; no necesitan
Postgres real (eso lo cubre `tests/integration/test_document_pipeline.py`, que
llama `run_isolated`/`tenant_session` dos veces seguidas contra la base real).
"""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

import app.services.dedup as dedup_module
from app.core.database import engine, run_isolated


class TestEcho:
    """`echo` del engine: nunca en True, ni en desarrollo.

    `echo=True` imprime cada sentencia con sus bind params reales por el
    logger propio de SQLAlchemy, que no respeta el `setLevel(WARNING)` de
    `app/core/logging.py` (usa `InstanceLogger`, que llama `logger._log()`
    directo). Contra una columna cifrada con pgcrypto eso filtraria el valor
    en claro y la propia `ENCRYPTION_KEY` por stdout, fuera de Loguru.
    """

    def test_echo_es_false(self) -> None:
        """Sin importar `APP_ENV`, el engine nunca hace echo de las queries."""
        assert engine.echo is False


class TestRunIsolated:
    """Contrato de `run_isolated`: ejecuta y siempre dispone el engine."""

    @pytest.fixture(autouse=True)
    def _contar_dispose(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sustituye `AsyncEngine.dispose()` por un contador, sin tocar conexiones reales.

        `dispose` es de solo lectura a nivel de instancia (lo expone `AsyncEngine`
        como wrapper de un atributo interno), asi que el parche va en la clase,
        no en `database_module.engine` directamente.
        """
        self.llamadas = 0

        async def _dispose_falso(_self: AsyncEngine) -> None:
            self.llamadas += 1

        monkeypatch.setattr(AsyncEngine, "dispose", _dispose_falso)

    def test_devuelve_el_resultado_de_la_corrutina(self) -> None:
        """El valor de retorno de la corrutina llega intacto al llamador sincrono."""

        async def coro() -> str:
            return "listo"

        assert run_isolated(coro()) == "listo"

    def test_dispone_el_engine_al_terminar_con_exito(self) -> None:
        """Tras una ejecucion exitosa, el pool queda disponible para el proximo loop."""

        async def coro() -> None:
            return None

        run_isolated(coro())

        assert self.llamadas == 1

    def test_dispone_el_engine_incluso_si_la_corrutina_revienta(self) -> None:
        """Una tarea fallida no debe dejar conexiones atadas a un loop que va a cerrarse."""

        async def coro() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            run_isolated(coro())

        assert self.llamadas == 1

    def test_dos_llamadas_seguidas_disponen_el_engine_cada_vez(self) -> None:
        """Simula dos tareas de Celery seguidas en el mismo proceso worker."""

        async def coro(valor: Any) -> Any:
            return valor

        assert run_isolated(coro(1)) == 1
        assert run_isolated(coro(2)) == 2
        assert self.llamadas == 2


class TestRunIsolatedCierraRedis:
    """BUG-021: `run_isolated()` tambien debe cerrar el cliente Redis de `dedup.py`."""

    @pytest.fixture(autouse=True)
    def _sin_engine_real(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No toca conexiones reales de SQLAlchemy en estos tests."""

        async def _dispose_falso(_self: AsyncEngine) -> None:
            return None

        monkeypatch.setattr(AsyncEngine, "dispose", _dispose_falso)

    def test_cierra_redis_al_terminar_con_exito(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tras una ejecucion exitosa, el cliente Redis queda cerrado para el proximo loop."""
        llamadas = 0

        async def _close_falso() -> None:
            nonlocal llamadas
            llamadas += 1

        monkeypatch.setattr(dedup_module, "close_redis", _close_falso)

        async def coro() -> None:
            return None

        run_isolated(coro())

        assert llamadas == 1

    def test_cierra_redis_incluso_si_la_corrutina_revienta(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Una tarea fallida tampoco debe dejar el cliente Redis atado a un loop que cierra."""
        llamadas = 0

        async def _close_falso() -> None:
            nonlocal llamadas
            llamadas += 1

        monkeypatch.setattr(dedup_module, "close_redis", _close_falso)

        async def coro() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            run_isolated(coro())

        assert llamadas == 1

    def test_fallo_al_cerrar_redis_no_tumba_una_tarea_exitosa(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si Redis ya estaba caido, cerrarlo puede fallar; no debe perderse el resultado."""

        async def _close_que_falla() -> None:
            raise ConnectionError("redis ya estaba caido")

        monkeypatch.setattr(dedup_module, "close_redis", _close_que_falla)

        async def coro() -> str:
            return "listo"

        assert run_isolated(coro()) == "listo"
