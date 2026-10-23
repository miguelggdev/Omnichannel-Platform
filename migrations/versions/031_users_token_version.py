"""`users.token_version`: revocar las sesiones de un usuario (ADR-089).

El refresh token lleva `ver` = `users.token_version` del momento en que se emitio. Subir la
version invalida de golpe todos sus refresh tokens: lo hacen el logout de todos los
dispositivos, el cambio de contrasena, el cambio de rol, la desactivacion y la deteccion de
reutilizacion de un refresh token ya rotado (indicio de robo). Sin esto, un refresh token
robado valia hasta 7 dias y no habia forma de anularlo.

Revision ID: 031_users_token_version
Revises: 030_security_hardening
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "031_users_token_version"
down_revision: str | None = "030_security_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Anade `token_version` (0 para los usuarios existentes)."""
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    """Quita `token_version`."""
    op.drop_column("users", "token_version")
