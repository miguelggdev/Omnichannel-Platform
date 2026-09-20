"""Las migraciones que cifran o re-hashean `contact_identifiers` exigen la clave.

Sin `ENCRYPTION_KEY`, la 008 cifraria la columna con una clave vacia (parece
cifrado y no protege nada) y la 009 no podria descifrar para recalcular el
indice ciego. Las dos tienen que abortar **antes de ejecutar ninguna sentencia**,
con un mensaje que diga que hacer.
"""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def _cargar(nombre_archivo: str) -> ModuleType:
    """Carga una migracion por ruta de archivo.

    `migrations/` no es un paquete y los nombres empiezan por un digito, asi que
    `import` normal no sirve.

    Args:
        nombre_archivo: Nombre del `.py` dentro de `migrations/versions/`.

    Returns:
        El modulo cargado.
    """
    spec = importlib.util.spec_from_file_location(
        f"mig_{nombre_archivo}", VERSIONS / nombre_archivo
    )
    assert spec is not None
    assert spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class _OpEspia:
    """Sustituto de `alembic.op` que registra lo que se intenta ejecutar."""

    def __init__(self) -> None:
        self.ejecutadas: list[Any] = []

    def execute(self, sentencia: Any) -> None:
        """Registra la sentencia en vez de correrla."""
        self.ejecutadas.append(sentencia)

    def __getattr__(self, nombre: str) -> Any:
        """Cualquier otra operacion de `op` (add_column, ...) tambien se registra."""

        def _registrar(*args: Any, **kwargs: Any) -> None:
            self.ejecutadas.append((nombre, args))

        return _registrar


MIGRACIONES = [
    "008_encrypt_contact_identifiers.py",
    "009_blind_index_per_tenant.py",
]


@pytest.mark.parametrize("archivo", MIGRACIONES)
class TestExigenLaClave:
    """008 y 009: sin `ENCRYPTION_KEY` no hacen nada y lo dicen."""

    @pytest.mark.parametrize("valor", [None, "", "   "])
    def test_upgrade_aborta_antes_de_ejecutar_nada(
        self, archivo: str, valor: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ausente, vacia o solo espacios: mismo resultado."""
        if valor is None:
            monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
        else:
            monkeypatch.setenv("ENCRYPTION_KEY", valor)
        modulo = _cargar(archivo)
        espia = _OpEspia()
        monkeypatch.setattr(modulo, "op", espia)

        with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
            modulo.upgrade()

        assert espia.ejecutadas == [], "no debe ejecutarse ninguna sentencia sin la clave"

    def test_downgrade_tambien_la_exige(
        self, archivo: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Descifrar de vuelta tambien necesita la clave que cifro."""
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
        modulo = _cargar(archivo)
        espia = _OpEspia()
        monkeypatch.setattr(modulo, "op", espia)

        with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
            modulo.downgrade()

        assert espia.ejecutadas == []

    def test_el_mensaje_dice_que_hacer(self, archivo: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """Quien lo lee a las 3 de la manana necesita la instruccion, no solo el error."""
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
        modulo = _cargar(archivo)

        with pytest.raises(RuntimeError) as error:
            modulo._clave()

        assert "alembic upgrade head" in str(error.value)


class TestClaveEnLaConsulta:
    """La clave viaja como parametro, no incrustada en el SQL."""

    def test_la_009_no_incrusta_la_clave_en_el_sql(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Si quedara en el texto, apareceria en `pg_stat_statements` y en los logs."""
        clave = "clave-de-prueba-de-mas-de-32-caracteres-x"
        monkeypatch.setenv("ENCRYPTION_KEY", clave)
        modulo = _cargar("009_blind_index_per_tenant.py")
        espia = _OpEspia()
        monkeypatch.setattr(modulo, "op", espia)

        modulo.upgrade()

        sentencia = espia.ejecutadas[0]
        assert clave not in str(sentencia)
        assert sentencia.compile().params == {"clave": clave}
