"""`clients.lead_automation_config`: calificacion automatica y asignacion de leads (Sprint 17).

Una columna JSONB por tenant, como `icp_config` y `lead_scoring_weights`:

- `qualification`: `{"enabled": bool, "rules": [{"min_score": n, "stage_slug": "..."}]}`; cuando el
  score total de un lead llega a `min_score`, pasa a esa etapa (solo hacia delante y nunca si una
  persona movio la etapa a mano). Lo valida `QualificationConfig`.
- `assignment`: `{"auto_assign": bool, "user_ids": [...] | null}`; round-robin entre agentes.
- `assignment_cursor`: ultimo usuario asignado (lo escribe el servidor, no la API de configuracion).

Revision ID: 032_lead_automation_config
Revises: 031_users_token_version
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "032_lead_automation_config"
down_revision: str | None = "031_users_token_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Anade la columna (`{}` para los tenants existentes: todo apagado)."""
    op.add_column(
        "clients",
        sa.Column(
            "lead_automation_config",
            postgresql.JSONB(),
            server_default="{}",
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Quita la columna."""
    op.drop_column("clients", "lead_automation_config")
