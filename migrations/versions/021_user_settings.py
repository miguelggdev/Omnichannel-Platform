"""`users.settings` — Sprint 14b: preferencias de interfaz de cada usuario.

El spec (`specs/sprint-14-sandbox-i18n.md` §4b) guarda el idioma de la interfaz y
el tema en `users.settings`, una columna JSONB que la tabla no tenia. Es de cada
usuario (no del tenant): cada persona de un mismo negocio puede usar la
plataforma en un idioma distinto.

`NOT NULL DEFAULT '{}'`: las filas que ya existen quedan con un objeto vacio y
la API devuelve los valores por defecto, sin una migracion de datos.

Revision ID: 021_user_settings
Revises: 020_user_role_medical
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "021_user_settings"
down_revision: str | None = "020_user_role_medical"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Agrega `users.settings` JSONB, con `{}` en las filas existentes."""
    op.add_column(
        "users",
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Quita `users.settings`; las preferencias guardadas se pierden."""
    op.drop_column("users", "settings")
