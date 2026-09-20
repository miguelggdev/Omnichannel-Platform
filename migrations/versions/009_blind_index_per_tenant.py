"""blind_index_per_tenant — el índice ciego pasa a ser por tenant.

`contact_identifiers.identifier_hash` (migración 008) era
`HMAC(ENCRYPTION_KEY, lower(trim(valor)))`: el mismo teléfono daba el mismo hash
en todos los tenants. Con lectura cruda de la tabla (un rol con `BYPASSRLS`, un
dump, un backup, el soporte del proveedor de base de datos) se podía correlacionar
por igualdad de hash que la misma persona escribe a dos clientes distintos de la
plataforma, que es justo lo que el aislamiento por tenant pretende impedir.

Ahora es `HMAC(ENCRYPTION_KEY, "{client_id}:" || lower(trim(valor)))`, igual que
`app.core.encryption.blind_index(valor, client_id)`. Si las dos se separan, la
aplicación deja de encontrar las filas que migró este script — hay una prueba de
integración (`tests/integration/test_encryption.py::TestParidadConLaMigracion`)
que compara la expresión SQL de aquí con la función de Python.

Qué hace, y por qué no hay que tocar más
----------------------------------------
Recalcula `identifier_hash` de todas las filas. Para eso tiene que **descifrar**
`identifier_value` (ya está en `bytea` desde la 008) y volver a hashear. No cambia
el esquema ni el `UNIQUE`: `(client_id, channel, identifier_hash)` sigue siendo el
mismo, y dos filas con el mismo (tenant, canal, valor) siguen dando el mismo hash
nuevo, así que la migración no puede introducir duplicados que antes no existían.

La clave sale de `ENCRYPTION_KEY` y viaja como parámetro, no incrustada en el SQL.
Aborta si no está definida; con una clave distinta de la que cifró la columna,
`pgp_sym_decrypt` falla con un error claro y la transacción entera se revierte.

Es una sola sentencia UPDATE sobre toda la tabla y toma bloqueos de fila mientras
dura: en una tabla muy grande conviene correrla en una ventana de bajo tráfico.

Revision ID: 009_blind_index_per_tenant
Revises: 008_encrypt_contact_identifiers
Create Date: 2026-09-19
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "009_blind_index_per_tenant"
down_revision: Union[str, None] = "008_encrypt_contact_identifiers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Debe mantenerse igual que `app.core.encryption.blind_index()`.
HASH_POR_TENANT = (
    "encode(hmac(client_id::text || ':' || lower(trim(pgp_sym_decrypt(identifier_value, :clave))), "
    ":clave, 'sha256'), 'hex')"
)
HASH_GLOBAL = (
    "encode(hmac(lower(trim(pgp_sym_decrypt(identifier_value, :clave))), :clave, 'sha256'), 'hex')"
)


def _clave() -> str:
    """Devuelve la clave de cifrado del entorno.

    Returns:
        El valor de `ENCRYPTION_KEY`.

    Raises:
        RuntimeError: Si la variable no está definida o está vacía.
    """
    clave = os.environ.get("ENCRYPTION_KEY", "").strip()
    if not clave:
        raise RuntimeError(
            "ENCRYPTION_KEY no esta definida: la migracion 009 no puede descifrar "
            "contact_identifiers.identifier_value para recalcular el indice ciego. "
            "Definila antes de correr `alembic upgrade head`."
        )
    return clave


def upgrade() -> None:
    op.execute(
        sa.text(f"UPDATE contact_identifiers SET identifier_hash = {HASH_POR_TENANT}").bindparams(  # noqa: S608
            clave=_clave()
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(f"UPDATE contact_identifiers SET identifier_hash = {HASH_GLOBAL}").bindparams(  # noqa: S608
            clave=_clave()
        )
    )
