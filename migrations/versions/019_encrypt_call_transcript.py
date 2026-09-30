"""Cifra call_records.transcript — Sprint 13.

La transcripcion de una llamada era `JSONB` en claro: en disco, en los backups
y en cualquier `SELECT` con acceso a la base. Es lo que dijeron las dos partes
y, en un consultorio, incluye datos de salud (Ley 1581, art. 5).

Pasa a `BYTEA` cifrado con `pgp_sym_encrypt()` (`EncryptedJSON`), la misma clave
y el mismo esquema que `phone_from`/`phone_to`. La conversion es en una sola
pasada y conserva las llamadas ya guardadas. El precio es que la columna deja de
consultarse por contenido; nada en la aplicacion lo hacia salvo la redaccion de
`clinical_privacy`, que ahora se hace en Python.

La columna pierde su `DEFAULT '[]'`: un default no puede cifrarse sin la clave,
y `voice_tasks.guardar_llamada` siempre manda la lista. La clave sale de
`ENCRYPTION_KEY` y viaja como parametro, no incrustada en el SQL.

Revision ID: 019_encrypt_call_transcript
Revises: 018_clinical_catalogs
Create Date: 2026-09-30
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "019_encrypt_call_transcript"
down_revision: str | None = "018_clinical_catalogs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _clave() -> str:
    """Devuelve la clave de cifrado del entorno.

    Returns:
        El valor de `ENCRYPTION_KEY`.

    Raises:
        RuntimeError: Si la variable no esta definida o esta vacia.
    """
    clave = os.environ.get("ENCRYPTION_KEY", "").strip()
    if not clave:
        raise RuntimeError(
            "ENCRYPTION_KEY no esta definida: la migracion 019 cifraria "
            "call_records.transcript con una clave vacia. "
            "Definila antes de correr `alembic upgrade head`."
        )
    return clave


def upgrade() -> None:
    # El DEFAULT '[]'::jsonb no se puede convertir a bytea: se quita antes.
    op.execute("ALTER TABLE call_records ALTER COLUMN transcript DROP DEFAULT")
    op.execute(
        sa.text(
            "ALTER TABLE call_records "
            "ALTER COLUMN transcript TYPE bytea "
            "USING pgp_sym_encrypt(transcript::text, :clave)"
        ).bindparams(clave=_clave())
    )


def downgrade() -> None:
    # `pgp_sym_decrypt` falla con un error claro si la clave no es la que
    # cifro: mejor eso que dejar basura en la columna.
    op.execute(
        sa.text(
            "ALTER TABLE call_records "
            "ALTER COLUMN transcript TYPE jsonb "
            "USING pgp_sym_decrypt(transcript, :clave)::jsonb"
        ).bindparams(clave=_clave())
    )
    op.execute("ALTER TABLE call_records ALTER COLUMN transcript SET DEFAULT '[]'::jsonb")
