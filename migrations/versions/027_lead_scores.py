"""lead_scores (#31) — historial del scoring de leads (Sprint 17, slice Dev A).

Cada fila es un calculo que **cambio** un score del lead (FIT, comportamiento o IA): el valor
nuevo, el anterior, que lo provoco y los factores que lo explican. El valor vigente sigue en
`leads.fit_score` / `behavioral_score` / `ai_score` (y `total_score`); esta tabla es la
explicacion y la historia, no la fuente de verdad del valor actual.

Desviaciones deliberadas sobre `specs/sprint-16-19-lead-management.md`
-----------------------------------------------------------------------
- **`created_at` en vez de `scored_at`**: es lo que ya da `TenantBaseModel` y significa lo mismo.
  Lo asigna la aplicacion con un instante estrictamente creciente (como `lead_activities`): dos
  recalculos en la misma transaccion tendrian el mismo `now()` y el "ultimo score" seria ambiguo.
- **`previous_score`, `trigger` y `user_id`** de mas: sin el valor anterior el historial no dice
  si el lead mejoro o empeoro, y sin el disparador no se puede auditar por que cambio.
- **`score_type` con `CHECK`** (`fit`, `behavioral`, `ai`), no texto libre ni ENUM (ver 025).
- **`user_id ON DELETE SET NULL`**, como en `lead_activities`: borrar un usuario no borra la
  historia.
- RLS con `FORCE` y `WITH CHECK`, como todas las tablas con `client_id`.

Los factores no llevan datos personales del lead (el calculador guarda dimensiones, puntos y
codigos de motivo, no el cargo ni el sector del lead). Aun asi la supresion RGPD de un lead
borra su historial de scores: el del AI score (Dev B) puede llevar razonamiento en texto.

Revision ID: 027_lead_scores
Revises: 026_lead_activities_linkedin
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "027_lead_scores"
down_revision: str | None = "026_lead_activities_linkedin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLITICA = "client_id = current_setting('app.current_client_id')::uuid"

# Debe coincidir con `app.models.lead_score.SCORE_TYPES` (un test lo comprueba).
SCORE_TYPES = ("fit", "behavioral", "ai")


def upgrade() -> None:
    """Crea `lead_scores` con su indice de consulta y la RLS forzada."""
    tipos = ", ".join(f"'{t}'" for t in SCORE_TYPES)
    op.create_table(
        "lead_scores",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("lead_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("score_type", sa.String(20), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("previous_score", sa.Integer(), nullable=True),
        sa.Column("trigger", sa.String(30), nullable=False),
        sa.Column("factors", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(f"score_type IN ({tipos})", name="ck_lead_scores_type"),
        sa.CheckConstraint("score BETWEEN 0 AND 100", name="ck_lead_scores_score"),
        sa.CheckConstraint(
            "previous_score IS NULL OR previous_score BETWEEN 0 AND 100",
            name="ck_lead_scores_previous",
        ),
    )
    op.create_index(op.f("ix_lead_scores_client_id"), "lead_scores", ["client_id"])
    op.create_index(
        "ix_lead_scores_lead_type",
        "lead_scores",
        ["lead_id", "score_type", sa.text("created_at DESC")],
    )
    op.execute("ALTER TABLE lead_scores ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE lead_scores FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON lead_scores
            FOR ALL
            USING ({_POLITICA})
            WITH CHECK ({_POLITICA})
    """)


def downgrade() -> None:
    """Quita la politica, los indices y la tabla."""
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON lead_scores")
    op.execute("ALTER TABLE lead_scores NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE lead_scores DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_lead_scores_lead_type", table_name="lead_scores")
    op.drop_index(op.f("ix_lead_scores_client_id"), table_name="lead_scores")
    op.drop_table("lead_scores")
