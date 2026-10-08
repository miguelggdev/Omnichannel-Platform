"""Secuencias de follow-up de leads (#32-34) — Sprint 18, slice Dev A.

Tablas nuevas, todas con `client_id`, RLS habilitada, FORCE y `WITH CHECK`:

- `lead_sequences` (#32): la secuencia (nombre, disparadores, prioridad de canales).
- `lead_sequence_steps` (#33): sus pasos en orden (`message`, `wait`, `condition`, `task`).
- `lead_sequence_enrollments` (#34): un lead inscrito en una secuencia y por donde va.

Desviaciones deliberadas sobre `specs/sprint-16-19-lead-management.md`
-----------------------------------------------------------------------
- **`lead_sequence_steps` lleva `client_id` y RLS.** El spec la deja sin ninguna de las dos
  (se apoya en la secuencia), pero CLAUDE.md lo exige en TODA tabla: sin RLS propia, cualquiera
  que conozca un `sequence_id` lee los mensajes que otro tenant envia a sus leads.
- **Sin `enrolled_count` / `completed_count`** en `lead_sequences`: un contador desnormalizado se
  desincroniza con la concurrencia y los borrados (mismo motivo que `lead_sources.leads_count`,
  ADR-082). `COUNT(*)` sobre `ix_lead_sequence_enrollments_sequence` basta.
- **`UNIQUE (lead_id, sequence_id)` solo entre inscripciones vivas** (`active`, `paused`): el del
  spec impediria volver a inscribir a un lead que ya termino la secuencia (p. ej. una de
  reactivacion meses despues) sin borrar su historia.
- **`created_at` hace de `enrolled_at`** (como `scored_at` en 027) y se anaden `last_step_at`,
  `steps_executed` (tope contra bucles), `enrolled_by_user_id` y `exit_reason` como **codigo**
  (`replied`, `gdpr`...): un motivo en texto libre podria llevar datos de la persona.
- **`UNIQUE (sequence_id, position)` es `DEFERRABLE INITIALLY DEFERRED`**, como las etapas
  (025): reordenar pasos intercambia posiciones dentro de una transaccion.
- `sequence_id` de una inscripcion en `NO ACTION`: una secuencia con inscripciones no se borra,
  se desactiva (`is_active=false`); asi el historial de quien paso por ella no desaparece.
- `name` unico por tenant: dos secuencias con el mismo nombre no se distinguen en el panel.

Revision ID: 028_lead_sequences
Revises: 027_lead_scores
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "028_lead_sequences"
down_revision: str | None = "027_lead_scores"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLITICA = "client_id = current_setting('app.current_client_id')::uuid"
_NUEVAS = ("lead_sequences", "lead_sequence_steps", "lead_sequence_enrollments")

# Deben coincidir con `app.models.lead_sequence` (un test lo comprueba).
STEP_TYPES = ("message", "wait", "condition", "task")
ENROLLMENT_STATUSES = ("active", "paused", "completed", "exited")


def _en(valores: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in valores)


def _created_at() -> sa.Column[sa.DateTime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    """Crea las tres tablas, sus indices y la RLS forzada."""
    op.create_table(
        "lead_sequences",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("trigger_conditions", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column(
            "channel_priority",
            postgresql.JSONB(),
            server_default='["whatsapp", "email", "instagram"]',
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("client_id", "name", name="uq_lead_sequence_name"),
    )

    op.create_table(
        "lead_sequence_steps",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("sequence_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("step_type", sa.String(20), nullable=False),
        sa.Column("config", postgresql.JSONB(), server_default="{}", nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["sequence_id"], ["lead_sequences.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "sequence_id",
            "position",
            name="uq_lead_sequence_step_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint("position >= 1", name="ck_lead_sequence_step_position"),
        sa.CheckConstraint(f"step_type IN ({_en(STEP_TYPES)})", name="ck_lead_sequence_step_type"),
    )

    op.create_table(
        "lead_sequence_enrollments",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("lead_id", sa.UUID(), nullable=False),
        sa.Column("sequence_id", sa.UUID(), nullable=False),
        sa.Column("enrolled_by_user_id", sa.UUID(), nullable=True),
        sa.Column("current_step", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("steps_executed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_step_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_step_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_reason", sa.String(50), nullable=True),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sequence_id"], ["lead_sequences.id"]),
        sa.ForeignKeyConstraint(["enrolled_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("current_step >= 1", name="ck_lead_enrollment_step"),
        sa.CheckConstraint("steps_executed >= 0", name="ck_lead_enrollment_executed"),
        sa.CheckConstraint(
            f"status IN ({_en(ENROLLMENT_STATUSES)})", name="ck_lead_enrollment_status"
        ),
        sa.CheckConstraint(
            "status NOT IN ('completed', 'exited') OR completed_at IS NOT NULL",
            name="ck_lead_enrollment_closed_at",
        ),
    )
    op.create_index(
        "uq_lead_enrollment_live",
        "lead_sequence_enrollments",
        ["lead_id", "sequence_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'paused')"),
    )
    op.create_index(
        "ix_lead_sequence_enrollments_due",
        "lead_sequence_enrollments",
        ["client_id", "next_step_at"],
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_lead_sequence_enrollments_sequence",
        "lead_sequence_enrollments",
        ["sequence_id", "status"],
    )
    op.create_index("ix_lead_sequence_enrollments_lead", "lead_sequence_enrollments", ["lead_id"])

    for tabla in _NUEVAS:
        op.create_index(op.f(f"ix_{tabla}_client_id"), tabla, ["client_id"])
        op.execute(f"ALTER TABLE {tabla} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tabla} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {tabla}
                FOR ALL
                USING ({_POLITICA})
                WITH CHECK ({_POLITICA})
        """)


def downgrade() -> None:
    """Quita las politicas y las tres tablas (hijas antes que padres)."""
    for tabla in reversed(_NUEVAS):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {tabla}")
        op.execute(f"ALTER TABLE {tabla} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tabla} DISABLE ROW LEVEL SECURITY")
    op.drop_table("lead_sequence_enrollments")
    op.drop_table("lead_sequence_steps")
    op.drop_table("lead_sequences")
