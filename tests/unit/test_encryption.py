"""Tests del cifrado de columnas con pgcrypto (Sprint 8, Dev A).

Lo que se puede comprobar sin base de datos es la forma del SQL que genera el
tipo y el comportamiento del índice ciego. Que PostgreSQL cifre de verdad —y
que la columna en disco sea ilegible— se verifica contra Postgres real en
`tests/integration/test_encryption.py`.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.core.config import get_settings
from app.core.encryption import EncryptedString, blind_index, mask_identifier
from app.models.contact_identifier import ContactIdentifier


def _sql(stmt: object) -> str:
    """Compila una sentencia al dialecto de PostgreSQL.

    Args:
        stmt: Sentencia de SQLAlchemy.

    Returns:
        El SQL resultante como texto.
    """
    return str(stmt.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]


class TestIndiceCiego:
    """`blind_index()`: determinismo, normalizacion y dependencia de la clave."""

    def test_es_determinista(self) -> None:
        """El mismo valor da siempre el mismo hash: sin eso no hay UNIQUE."""
        assert blind_index("+573001234567") == blind_index("+573001234567")

    def test_normaliza_espacios_y_mayusculas(self) -> None:
        """`  Juan@X.com ` y `juan@x.com` son el mismo identificador."""
        assert blind_index("  Juan@X.com ") == blind_index("juan@x.com")

    def test_sin_normalizar_distingue_mayusculas(self) -> None:
        """Con `normalizar=False` las mayusculas si cuentan."""
        assert blind_index("ABC", normalizar=False) != blind_index("abc", normalizar=False)

    def test_valores_distintos_dan_hashes_distintos(self) -> None:
        """Dos telefonos distintos no pueden colisionar en el UNIQUE."""
        assert blind_index("+573001234567") != blind_index("+573001234568")

    def test_none_devuelve_none(self) -> None:
        """Un identificador ausente no se hashea a la cadena vacia."""
        assert blind_index(None) is None

    def test_longitud_de_sha256(self) -> None:
        """64 caracteres hex: es lo que declara la columna `String(64)`."""
        hash_ = blind_index("+573001234567")
        assert hash_ is not None
        assert len(hash_) == 64
        assert int(hash_, 16) >= 0  # es hexadecimal valido

    def test_depende_de_la_clave(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rotar `ENCRYPTION_KEY` cambia todos los hashes.

        No es un detalle: es la razon por la que rotar la clave obliga a
        recalcular la columna entera, no solo a re-cifrar.
        """
        original = blind_index("+573001234567")

        settings = get_settings()
        monkeypatch.setattr(settings, "ENCRYPTION_KEY", "otra-clave-de-mas-de-32-caracteres-x")
        assert blind_index("+573001234567") != original


class TestTipoCifrado:
    """`EncryptedString`: el SQL que genera al escribir y al leer."""

    def test_el_select_descifra(self) -> None:
        """Leer la columna pasa por `pgp_sym_decrypt`."""
        sql = _sql(select(ContactIdentifier.identifier_value))
        assert "pgp_sym_decrypt" in sql

    def test_el_insert_cifra(self) -> None:
        """Escribir la columna pasa por `pgp_sym_encrypt`."""
        tabla = ContactIdentifier.__table__
        sql = _sql(tabla.insert().values(identifier_value="+573001234567"))
        assert "pgp_sym_encrypt" in sql

    def test_la_clave_no_queda_incrustada_en_el_sql(self) -> None:
        """La clave viaja como parametro, no como literal en la sentencia.

        Si se incrustara, aparecería en `pg_stat_statements`, en los logs de
        queries lentas y en cualquier traza de SQLAlchemy.
        """
        sql = _sql(select(ContactIdentifier.identifier_value))
        assert get_settings().ENCRYPTION_KEY not in sql

    def test_la_columna_es_bytea(self) -> None:
        """El tipo real en PostgreSQL es BYTEA, no TEXT.

        `pgp_sym_encrypt()` devuelve `bytea`; declarar la columna como TEXT
        (lo que dice el spec §8.2) hace fallar el INSERT en asyncpg.
        """
        columna = ContactIdentifier.__table__.c.identifier_value
        assert isinstance(columna.type, EncryptedString)
        assert "BYTEA" in str(columna.type.impl)


class TestMascara:
    """`mask_identifier()`: lo que un agente ve en la bandeja antes de nombrar al contacto."""

    def test_conserva_los_ultimos_cuatro(self) -> None:
        """El resto queda enmascarado con `*`."""
        assert mask_identifier("573001234567") == "********4567"

    def test_mismo_largo_que_el_original(self) -> None:
        """El enmascarado no delata la longitud real por truncamiento ni relleno."""
        valor = "573001234567"
        assert len(mask_identifier(valor)) == len(valor)

    def test_valor_de_cuatro_o_menos_se_enmascara_entero(self) -> None:
        """Con 4 caracteres o menos, dejar algo a la vista ya revela casi todo."""
        assert mask_identifier("abcd") == "****"
        assert mask_identifier("ab") == "**"

    def test_no_es_reversible_por_construccion(self) -> None:
        """Dos valores que comparten los ultimos 4 dan la misma mascara.

        No es un bug: la mascara nunca pretendio identificar de forma unica,
        solo dar una pista visual. La unicidad real la da `identifier_hash`.
        """
        assert mask_identifier("573001234567") == mask_identifier("999991234567")


class TestSincronizacionDelHash:
    """El listener del modelo mantiene el indice ciego al dia."""

    def test_el_hash_se_calcula_al_insertar(self) -> None:
        """Nadie tiene que acordarse de escribir `identifier_hash` a mano."""
        from sqlalchemy import event

        identificador = ContactIdentifier(
            client_id=uuid.uuid4(),
            contact_id=uuid.uuid4(),
            channel="whatsapp",
            identifier_value="+573001234567",
        )
        # El listener corre en el flush; se invoca directo para no necesitar DB.
        assert event.contains(ContactIdentifier, "before_insert", _listener())
        _listener()(None, None, identificador)
        assert identificador.identifier_hash == blind_index("+573001234567")

    def test_cambiar_el_valor_recalcula_el_hash(self) -> None:
        """Un UPDATE del valor deja el hash consistente.

        Es el caso de la anonimizacion de RGPD: si el hash se quedara con el
        telefono viejo, la fila anonimizada seguiria siendo encontrable por el
        identificador que se suponia borrado.
        """
        identificador = ContactIdentifier(
            client_id=uuid.uuid4(),
            contact_id=uuid.uuid4(),
            channel="whatsapp",
            identifier_value="+573001234567",
        )
        _listener()(None, None, identificador)
        identificador.identifier_value = "[ELIMINADO-abc12345]"
        _listener()(None, None, identificador)
        assert identificador.identifier_hash == blind_index("[ELIMINADO-abc12345]")


def _listener() -> object:
    """Devuelve el listener registrado en el modelo.

    Returns:
        La función `_sincronizar_indice_ciego` del módulo del modelo.
    """
    from app.models.contact_identifier import _sincronizar_indice_ciego

    return _sincronizar_indice_ciego
