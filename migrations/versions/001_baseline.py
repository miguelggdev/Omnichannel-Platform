"""Baseline — Schema inicial gestionado por supabase/init/init.sql.

Esta migración vacía establece el punto de partida para Alembic.
El schema real se crea via init.sql (DDL manual con RLS, pgvector, etc.).
Alembic tracking comienza aquí; futuras migraciones son incrementales.

Revision ID: 001_baseline
Revises: -
Create Date: 2026-09-04
"""

from typing import Union

# revision identifiers, used by Alembic.
revision: str = "001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    """Baseline — schema already exists via supabase/init/init.sql."""


def downgrade() -> None:
    """No downgrade for baseline."""
