"""Deals y llamadas agendadas (#35-36) — Sprint 19, slice Dev A.

Tablas nuevas, con `client_id`, RLS habilitada, FORCE y `WITH CHECK`:

- `deals` (#35): una oportunidad de venta de un lead, con valor, moneda, etapa y probabilidad.
- `scheduled_calls` (#36): una llamada agendada con un lead (humana, con voz IA o mixta).

Desviaciones deliberadas sobre `specs/sprint-16-19-lead-management.md`
-----------------------------------------------------------------------
- **`VARCHAR` + `CHECK` en vez de los tipos `deal_stage`, `call_type` y `call_status`**, como el
  resto del modulo de leads (025-028): anadir un valor a un `ENUM` de PostgreSQL exige
  `ALTER TYPE` fuera de transaccion, y un `CHECK` se cambia en una migracion normal.
- **`scheduled_calls` no guarda `transcript` ni `recording_url`**: la llamada real ya queda en
  `call_records` (Sprint 13), con la transcripcion **cifrada** (`EncryptedJSON`). Aqui va
  `call_record_id` hacia ella. Una transcripcion en `TEXT` plano duplicaria, sin cifrar, lo que
  dijo la persona.
- **Dos recordatorios en vez de `reminder_sent_at`**: el spec pide avisar 24 h y 1 h antes; con
  una sola columna no se sabe cual se envio. `reminder_24h_sent_at` y `reminder_1h_sent_at`.
- **`deals.stage_changed_at`**: "pipeline health" necesita cuanto lleva un deal en su etapa, y
  `updated_at` cambia con cualquier edicion.
- **`deals.lead_id` en `NO ACTION`** (no `CASCADE`): un deal es historia de ingresos. Un lead no
  se borra de verdad en la aplicacion (soft delete o anonimizacion RGPD); si alguna vez se borra,
  sus deals deben decidirse antes. `scheduled_calls.lead_id` si va en `CASCADE` (es su agenda) y
  `deal_id` en `SET NULL`.
- **CHECKs de coherencia:** un deal ganado tiene `won_at`, uno perdido `lost_at`; ninguno tiene
  los dos; valor >= 0; moneda ISO de 3 letras; duracion de llamada entre 5 y 480 minutos; una
  llamada completada tiene `completed_at` y una confirmada `confirmed_at`.
- `notes` y `lost_reason` son texto libre: pueden llevar datos de la persona. La supresion RGPD
  los anonimiza (`app/services/lead_suppression.py`) y el export los incluye.

Revision ID: 029_deals_scheduled_calls
Revises: 028_lead_sequences
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "029_deals_scheduled_calls"
down_revision: str | None = "028_lead_sequences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLITICA = "client_id = current_setting('app.current_client_id')::uuid"
_NUEVAS = ("deals", "scheduled_calls")

# Deben coincidir con `app.models.deal` y `app.models.scheduled_call` (un test lo comprueba).
DEAL_STAGES = ("new_contact", "qualified", "proposal", "negotiation", "closed_won", "closed_lost")
CALL_TYPES = ("ai_voice", "human", "hybrid")
CALL_STATUSES = (
    "pending",
    "confirmed",
    "in_progress",
    "completed",
    "no_show",
    "cancelled",
    "rescheduled",
)


def _en(valores: tuple[str, ...]) -> str:
    """Lista SQL de literales para un `IN (...)`: `('a', 'b')` -> `'a', 'b'`."""
    return ", ".join(f"'{v}'" for v in valores)


def _ts(nombre: str, *, por_defecto: bool = False) -> sa.Column[sa.DateTime]:
    """Columna de instante con zona; con `por_defecto`, `now()` y `NOT NULL`."""
    if por_defecto:
        return sa.Column(
            nombre, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        )
    return sa.Column(nombre, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    """Crea las dos tablas, sus indices y la RLS forzada."""
    op.create_table(
        "deals",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("lead_id", sa.UUID(), nullable=False),
        sa.Column("assigned_user_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("value", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("currency", sa.String(3), server_default="USD", nullable=False),
        sa.Column("stage", sa.String(20), server_default="new_contact", nullable=False),
        sa.Column("probability", sa.Integer(), server_default="10", nullable=False),
        sa.Column("expected_close_date", sa.Date(), nullable=True),
        sa.Column("actual_close_date", sa.Date(), nullable=True),
        _ts("won_at"),
        _ts("lost_at"),
        sa.Column("lost_reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), server_default="{}", nullable=False),
        _ts("stage_changed_at", por_defecto=True),
        _ts("created_at", por_defecto=True),
        _ts("updated_at", por_defecto=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.ForeignKeyConstraint(["assigned_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(f"stage IN ({_en(DEAL_STAGES)})", name="ck_deals_stage"),
        sa.CheckConstraint("probability BETWEEN 0 AND 100", name="ck_deals_probability"),
        sa.CheckConstraint("value >= 0", name="ck_deals_value"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_deals_currency"),
        sa.CheckConstraint("stage <> 'closed_won' OR won_at IS NOT NULL", name="ck_deals_won_at"),
        sa.CheckConstraint(
            "stage <> 'closed_lost' OR lost_at IS NOT NULL", name="ck_deals_lost_at"
        ),
        sa.CheckConstraint("won_at IS NULL OR lost_at IS NULL", name="ck_deals_won_xor_lost"),
    )
    op.create_index("ix_deals_client_stage", "deals", ["client_id", "stage"])
    op.create_index(
        "ix_deals_client_value",
        "deals",
        ["client_id", sa.text("value DESC")],
        postgresql_where=sa.text("stage NOT IN ('closed_won', 'closed_lost')"),
    )
    op.create_index("ix_deals_lead", "deals", ["lead_id"])
    op.create_index(
        "ix_deals_client_won_at",
        "deals",
        ["client_id", "won_at"],
        postgresql_where=sa.text("stage = 'closed_won'"),
    )

    op.create_table(
        "scheduled_calls",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("lead_id", sa.UUID(), nullable=False),
        sa.Column("deal_id", sa.UUID(), nullable=True),
        sa.Column("assigned_user_id", sa.UUID(), nullable=True),
        sa.Column("call_record_id", sa.UUID(), nullable=True),
        sa.Column("call_type", sa.String(20), server_default="human", nullable=False),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), server_default="30", nullable=False),
        sa.Column("timezone", sa.String(50), server_default="America/Bogota", nullable=False),
        sa.Column("ai_voice_provider", sa.String(30), nullable=True),
        sa.Column("ai_voice_config", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("outcome", sa.String(30), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        _ts("reminder_24h_sent_at"),
        _ts("reminder_1h_sent_at"),
        _ts("confirmed_at"),
        _ts("completed_at"),
        _ts("cancelled_at"),
        _ts("created_at", por_defecto=True),
        _ts("updated_at", por_defecto=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["deal_id"], ["deals.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["assigned_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["call_record_id"], ["call_records.id"], ondelete="SET NULL"),
        sa.CheckConstraint(f"call_type IN ({_en(CALL_TYPES)})", name="ck_scheduled_calls_type"),
        sa.CheckConstraint(f"status IN ({_en(CALL_STATUSES)})", name="ck_scheduled_calls_status"),
        sa.CheckConstraint(
            "duration_minutes BETWEEN 5 AND 480", name="ck_scheduled_calls_duration"
        ),
        sa.CheckConstraint(
            "status <> 'completed' OR completed_at IS NOT NULL",
            name="ck_scheduled_calls_completed_at",
        ),
        sa.CheckConstraint(
            "status <> 'confirmed' OR confirmed_at IS NOT NULL",
            name="ck_scheduled_calls_confirmed_at",
        ),
        sa.CheckConstraint(
            "status <> 'cancelled' OR cancelled_at IS NOT NULL",
            name="ck_scheduled_calls_cancelled_at",
        ),
        sa.CheckConstraint(
            "call_type = 'human' OR ai_voice_provider IS NOT NULL",
            name="ck_scheduled_calls_ai_provider",
        ),
    )
    op.create_index(
        "ix_scheduled_calls_upcoming",
        "scheduled_calls",
        ["client_id", "scheduled_at"],
        postgresql_where=sa.text("status IN ('pending', 'confirmed')"),
    )
    op.create_index(
        "ix_scheduled_calls_user_upcoming",
        "scheduled_calls",
        ["assigned_user_id", "scheduled_at"],
        postgresql_where=sa.text("status IN ('pending', 'confirmed')"),
    )
    op.create_index("ix_scheduled_calls_lead", "scheduled_calls", ["lead_id"])
    op.create_index("ix_scheduled_calls_deal", "scheduled_calls", ["deal_id"])

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
    """Quita las politicas y las dos tablas (hija antes que padre)."""
    for tabla in reversed(_NUEVAS):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {tabla}")
        op.execute(f"ALTER TABLE {tabla} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tabla} DISABLE ROW LEVEL SECURITY")
    op.drop_table("scheduled_calls")
    op.drop_table("deals")
