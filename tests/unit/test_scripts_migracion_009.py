"""Tests de los scripts de despliegue de la migracion 009.

El script de despliegue PARA los servicios: lo que importa probar es la seguridad de su
orden y de sus puertas (no toca nada sin `--execute`, exige el backup verificado, y ante
un fallo deja los servicios parados en vez de arrancar una aplicacion sobre datos
inconsistentes). Se prueba con `alembic`, `docker` y `python` falsos en el PATH: ninguna
base de datos ni contenedor reales.
"""

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

RAIZ = Path(__file__).resolve().parents[2]
SCRIPT = RAIZ / "scripts" / "deploy_migration_009.sh"
VERIFICADOR = RAIZ / "scripts" / "verify_migration_009.py"

REV_ANTERIOR = "008_encrypt_contact_identifiers"
REV_OBJETIVO = "009_blind_index_per_tenant"


def _cargar_verificador() -> ModuleType:
    especificacion = importlib.util.spec_from_file_location("verify_migration_009", VERIFICADOR)
    assert especificacion is not None
    assert especificacion.loader is not None
    modulo = importlib.util.module_from_spec(especificacion)
    especificacion.loader.exec_module(modulo)
    return modulo


class TestVerificador:
    """`verify_migration_009.py` solo lee y nunca filtra la clave."""

    def test_sin_entorno_no_se_puede_verificar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATABASE_URL_DIRECT", raising=False)
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)

        assert _cargar_verificador().main() == 2

    @pytest.mark.parametrize(("desalineadas", "esperado"), [(0, 0), (3, 1)])
    def test_el_codigo_de_salida_depende_de_las_filas_desalineadas(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        desalineadas: int,
        esperado: int,
    ) -> None:
        monkeypatch.setenv("DATABASE_URL_DIRECT", "postgresql+asyncpg://u:p@h/db")
        monkeypatch.setenv("ENCRYPTION_KEY", "k" * 40)
        consultas: list[tuple[str, dict[str, Any] | None]] = []

        class Cursor:
            def __init__(self) -> None:
                self.ultimo = ""

            def __enter__(self) -> "Cursor":
                return self

            def __exit__(self, *exc: Any) -> None:
                return None

            def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
                consultas.append((sql, params))
                self.ultimo = sql

            def fetchone(self) -> tuple[int]:
                return (100,) if "<>" not in self.ultimo else (desalineadas,)

        class Conexion:
            def __enter__(self) -> "Conexion":
                return self

            def __exit__(self, *exc: Any) -> None:
                return None

            def cursor(self) -> Cursor:
                return Cursor()

        urls: list[str] = []

        def conectar(url: str) -> Conexion:
            urls.append(url)
            return Conexion()

        falso = ModuleType("psycopg2")
        falso.connect = conectar  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "psycopg2", falso)

        assert _cargar_verificador().main() == esperado

        assert urls == ["postgresql://u:p@h/db"]  # el driver de Alembic, no asyncpg
        assert all(
            " UPDATE " not in sql.upper() and "DELETE" not in sql.upper() for sql, _ in consultas
        )
        # La clave viaja como parametro, nunca incrustada en el SQL.
        assert all("k" * 40 not in sql for sql, _ in consultas)
        assert capsys.readouterr().out.count("100 filas") == 1

    def test_un_error_de_conexion_no_filtra_la_clave(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("DATABASE_URL_DIRECT", "postgresql://u:p@h/db")
        monkeypatch.setenv("ENCRYPTION_KEY", "clave-super-secreta-de-32-caracteres")

        def conectar(url: str) -> None:
            raise RuntimeError(f"no conecta con {url} usando clave-super-secreta-de-32-caracteres")

        falso = ModuleType("psycopg2")
        falso.connect = conectar  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "psycopg2", falso)

        assert _cargar_verificador().main() == 2

        salida = capsys.readouterr()
        assert "secreta" not in salida.out + salida.err
        assert "u:p@h" not in salida.out + salida.err

    def test_la_consulta_es_la_misma_expresion_que_la_migracion(self) -> None:
        """Si divergen, el script daria por buena (o mala) una formula que no es la real."""
        migracion = (RAIZ / "migrations" / "versions" / "009_blind_index_per_tenant.py").read_text(
            encoding="utf-8"
        )
        consulta = _cargar_verificador().CONSULTA_DESALINEADAS

        assert "client_id::text || ':' || lower(trim(pgp_sym_decrypt(identifier_value" in consulta
        assert "client_id::text || ':' ||" in migracion
        assert "'sha256'" in consulta
        assert "'hex'" in consulta


BASH = shutil.which("bash") or "bash"

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="el script es de bash; se prueba en Linux (CI)",
)


class Entorno:
    """PATH con `alembic`, `docker` y `python` falsos que registran lo que se les pide."""

    def __init__(self, directorio: Path, revision: str, verificacion_ok: bool, migra_ok: bool):
        self.bin = directorio / "bin"
        self.bin.mkdir()
        self.registro = directorio / "llamadas.log"
        self.registro.write_text("")
        self._shim(
            "alembic",
            f'''
if [[ "$*" == *current* ]]; then
    echo "{revision} (head)"; exit 0
fi
echo "alembic $*" >> "{self.registro}"
{"exit 0" if migra_ok else "exit 1"}
''',
        )
        self._shim(
            "docker",
            f'echo "docker $*" >> "{self.registro}"\nexit 0',
        )
        self._shim(
            "python",
            f'echo "verificar $*" >> "{self.registro}"\n{"exit 0" if verificacion_ok else "exit 1"}',
        )

    def _shim(self, nombre: str, cuerpo: str) -> None:
        ruta = self.bin / nombre
        ruta.write_text("#!/usr/bin/env bash\n" + cuerpo)
        ruta.chmod(ruta.stat().st_mode | stat.S_IXUSR)

    def correr(
        self, *args: str, entorno: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "DATABASE_URL_DIRECT": "postgresql+asyncpg://u:p@h/db",
            "ENCRYPTION_KEY": "k" * 40,
            "HOME": str(self.bin.parent),
        }
        env.update(entorno or {})
        return subprocess.run(  # noqa: S603 - argumentos fijos del propio test
            [BASH, str(SCRIPT), *args], capture_output=True, text=True, env=env, check=False
        )

    def llamadas(self) -> list[str]:
        return [linea for linea in self.registro.read_text().splitlines() if linea]


@pytest.fixture
def entorno_ok(tmp_path: Path) -> Entorno:
    return Entorno(tmp_path, REV_ANTERIOR, verificacion_ok=True, migra_ok=True)


@needs_bash
class TestDespliegue:
    def test_la_sintaxis_es_valida(self) -> None:
        comprobacion = subprocess.run(  # noqa: S603 - argumentos fijos del propio test
            [BASH, "-n", str(SCRIPT)], check=False
        )

        assert comprobacion.returncode == 0

    def test_sin_execute_solo_comprueba_y_no_toca_nada(self, entorno_ok: Entorno) -> None:
        resultado = entorno_ok.correr()

        assert resultado.returncode == 0
        assert entorno_ok.llamadas() == []
        assert "--execute" in resultado.stdout

    def test_execute_sin_backup_verificado_se_niega_antes_de_parar_nada(
        self, entorno_ok: Entorno
    ) -> None:
        resultado = entorno_ok.correr("--execute")

        assert resultado.returncode != 0
        assert "--backup-verificado" in resultado.stderr
        assert entorno_ok.llamadas() == []

    @pytest.mark.parametrize("faltante", ["DATABASE_URL_DIRECT", "ENCRYPTION_KEY"])
    def test_sin_las_variables_no_arranca(self, entorno_ok: Entorno, faltante: str) -> None:
        resultado = entorno_ok.correr("--execute", "--backup-verificado", entorno={faltante: ""})

        assert resultado.returncode != 0
        assert faltante in resultado.stderr
        assert entorno_ok.llamadas() == []

    def test_una_clave_corta_no_arranca(self, entorno_ok: Entorno) -> None:
        resultado = entorno_ok.correr(
            "--execute", "--backup-verificado", entorno={"ENCRYPTION_KEY": "corta"}
        )

        assert resultado.returncode != 0
        assert entorno_ok.llamadas() == []

    def test_con_otra_revision_no_migra(self, tmp_path: Path) -> None:
        entorno = Entorno(tmp_path, "007_auth_lookup_function", True, True)

        resultado = entorno.correr("--execute", "--backup-verificado")

        assert resultado.returncode != 0
        assert REV_ANTERIOR in resultado.stderr
        assert entorno.llamadas() == []

    def test_el_orden_es_parar_migrar_verificar_arrancar(self, entorno_ok: Entorno) -> None:
        resultado = entorno_ok.correr("--execute", "--backup-verificado")

        assert resultado.returncode == 0, resultado.stderr
        llamadas = entorno_ok.llamadas()
        orden = [
            next(i for i, x in enumerate(llamadas) if x.startswith("docker compose stop")),
            next(i for i, x in enumerate(llamadas) if x.startswith("alembic")),
            next(i for i, x in enumerate(llamadas) if x.startswith("verificar")),
            next(i for i, x in enumerate(llamadas) if x.startswith("docker compose up")),
        ]
        assert orden == sorted(orden)
        assert any(REV_OBJETIVO in x for x in llamadas)

    def test_para_todos_los_servicios_que_escriben_identificadores(
        self, entorno_ok: Entorno
    ) -> None:
        entorno_ok.correr("--execute", "--backup-verificado")

        parada = next(x for x in entorno_ok.llamadas() if x.startswith("docker compose stop"))
        for servicio in ("api", "celery-webhooks", "celery-ai"):
            assert servicio in parada.split()

    def test_si_la_migracion_falla_los_servicios_siguen_parados(self, tmp_path: Path) -> None:
        entorno = Entorno(tmp_path, REV_ANTERIOR, verificacion_ok=True, migra_ok=False)

        resultado = entorno.correr("--execute", "--backup-verificado")

        assert resultado.returncode != 0
        assert any(x.startswith("docker compose stop") for x in entorno.llamadas())
        assert not any(x.startswith("docker compose up") for x in entorno.llamadas())
        assert not any(x.startswith("verificar") for x in entorno.llamadas())

    def test_si_la_verificacion_falla_no_se_arranca_la_aplicacion(self, tmp_path: Path) -> None:
        """Arrancar sobre hashes inconsistentes duplicaria un contacto por cada mensaje."""
        entorno = Entorno(tmp_path, REV_ANTERIOR, verificacion_ok=False, migra_ok=True)

        resultado = entorno.correr("--execute", "--backup-verificado")

        assert resultado.returncode != 0
        assert "NO los arranques" in resultado.stderr
        assert not any(x.startswith("docker compose up") for x in entorno.llamadas())

    def test_si_ya_esta_migrada_solo_verifica(self, tmp_path: Path) -> None:
        entorno = Entorno(tmp_path, REV_OBJETIVO, verificacion_ok=True, migra_ok=True)

        resultado = entorno.correr("--execute", "--backup-verificado")

        assert resultado.returncode == 0
        assert entorno.llamadas() == ["verificar scripts/verify_migration_009.py"]
