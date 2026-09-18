"""encrypt_contact_identifiers — Sprint 8 (Dev A).

Cifra `contact_identifiers.identifier_value` con pgcrypto y agrega el índice
ciego (`identifier_hash`) que sostiene la búsqueda por igualdad y la unicidad.
El razonamiento completo está en `app/core/encryption.py`; lo que importa aquí
es el orden de las operaciones, que no es intercambiable:

1. Agregar `identifier_hash` y calcularlo **sobre el texto todavía en claro**.
   Después del paso 3 ya no se puede: el valor estaría cifrado y habría que
   descifrarlo para hashearlo.
2. Cambiar el UNIQUE de `identifier_value` a `identifier_hash`. Tiene que ir
   antes de alterar el tipo de la columna: PostgreSQL no deja un
   `ALTER COLUMN ... TYPE` sobre una columna que participa en un índice único.
3. Convertir la columna a `BYTEA` cifrando lo que ya hay
   (`USING pgp_sym_encrypt(...)`), en una sola pasada.

La clave sale de `ENCRYPTION_KEY` y viaja como parámetro, no incrustada en el
SQL. La migración aborta si no está definida: seguir adelante dejaría la tabla
cifrada con una clave vacía, que es peor que no cifrarla — parece cifrado y no
protege nada.

El HMAC se calcula con `hmac()` de pgcrypto sobre `lower(trim(valor))`, la misma
normalización que `app.core.encryption.blind_index()`. Si las dos se separan, la
aplicación deja de encontrar las filas que migró este script.

Revision ID: 008_encrypt_contact_identifiers
Revises: 007_auth_lookup_function
Create Date: 2026-09-17
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "008_encrypt_contact_identifiers"
down_revision: Union[str, None] = "007_auth_lookup_function"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT = "uq_contact_identifier"


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
            "ENCRYPTION_KEY no esta definida: la migracion 008 cifraria "
            "contact_identifiers.identifier_value con una clave vacia. "
            "Definila antes de correr `alembic upgrade head`."
        )
    return clave


def upgrade() -> None:
    clave = _clave()

    # ─── 1. Indice ciego, calculado sobre el texto en claro ─────────────────
    op.add_column(
        "contact_identifiers",
        sa.Column("identifier_hash", sa.String(length=64), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE contact_identifiers "
            "SET identifier_hash = encode("
            "  hmac(lower(trim(identifier_value)), :clave, 'sha256'), 'hex')"
        ).bindparams(clave=clave)
    )
    op.alter_column("contact_identifiers", "identifier_hash", nullable=False)
    op.create_index(
        "ix_contact_identifiers_identifier_hash",
        "contact_identifiers",
        ["identifier_hash"],
    )

    # ─── 2. Mover el UNIQUE al hash ─────────────────────────────────────────
    # Antes del ALTER TYPE: PostgreSQL rechaza cambiar el tipo de una columna
    # que forma parte de un indice unico.
    op.drop_constraint(_CONSTRAINT, "contact_identifiers", type_="unique")
    op.create_unique_constraint(
        _CONSTRAINT,
        "contact_identifiers",
        ["client_id", "channel", "identifier_hash"],
    )

    # ─── 3. Cifrar la columna ───────────────────────────────────────────────
    op.execute(
        sa.text(
            "ALTER TABLE contact_identifiers "
            "ALTER COLUMN identifier_value TYPE bytea "
            "USING pgp_sym_encrypt(identifier_value, :clave)"
        ).bindparams(clave=clave)
    )


def downgrade() -> None:
    clave = _clave()

    # Descifrar de vuelta a texto. `pgp_sym_decrypt` falla con un error claro si
    # la clave no es la que cifro: mejor eso que dejar basura en la columna.
    op.execute(
        sa.text(
            "ALTER TABLE contact_identifiers "
            "ALTER COLUMN identifier_value TYPE varchar(255) "
            "USING pgp_sym_decrypt(identifier_value, :clave)"
        ).bindparams(clave=clave)
    )

    op.drop_constraint(_CONSTRAINT, "contact_identifiers", type_="unique")
    op.create_unique_constraint(
        _CONSTRAINT,
        "contact_identifiers",
        ["client_id", "channel", "identifier_value"],
    )
    op.drop_index("ix_contact_identifiers_identifier_hash", table_name="contact_identifiers")
    op.drop_column("contact_identifiers", "identifier_hash")
