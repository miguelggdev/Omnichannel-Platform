"""Helpers para insertar identificadores de contacto cifrados (Sprint 8).

Desde la migración `008_encrypt_contact_identifiers`,
`contact_identifiers.identifier_value` es `BYTEA` cifrado con pgcrypto y la
tabla tiene además `identifier_hash` (índice ciego, por tenant) como NOT NULL. Los tests de
integración que insertan filas con SQL crudo —sin pasar por el ORM, que es
quien normalmente calcula el hash— tienen que cifrar y hashear a mano.

Concentrarlo aquí evita que cada archivo de test invente su propia versión: si
la normalización del hash cambia en `app.core.encryption`, hay un solo sitio
que ajustar.
"""

import uuid

from app.core.config import get_settings
from app.core.encryption import blind_index

#: INSERT de un identificador ya cifrado. Binds: `cid`, `contact`, `canal`, y
#: los tres que devuelve `params_identificador()`.
SQL_INSERT_IDENTIFICADOR = (
    "INSERT INTO contact_identifiers "
    "(client_id, contact_id, channel, identifier_value, identifier_hash) "
    "VALUES (:cid, :contact, :canal, pgp_sym_encrypt(:valor, :clave), :hash)"
)

#: SELECT del identificador en claro de un contacto. Binds: `id` y `clave`.
SQL_SELECT_IDENTIFICADOR = (
    "SELECT pgp_sym_decrypt(identifier_value, :clave) AS identifier_value "
    "FROM contact_identifiers WHERE contact_id = :id"
)


def params_identificador(
    valor: str, client_id: uuid.UUID | str, canal: str = "whatsapp"
) -> dict[str, str]:
    """Parámetros para insertar un identificador cifrado.

    Args:
        valor: Identificador en claro (teléfono, email, handle).
        client_id: Tenant del identificador: el índice ciego es por tenant
            (migración 009), así que el hash depende de él.
        canal: Canal del identificador.

    Returns:
        Dict con `canal`, `valor`, `clave` y `hash` listos para el bind.
    """
    return {
        "canal": canal,
        "valor": valor,
        "clave": get_settings().ENCRYPTION_KEY,
        "hash": blind_index(valor, client_id) or "",
    }
