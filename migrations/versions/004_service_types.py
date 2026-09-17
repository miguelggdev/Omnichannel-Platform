"""service_types — catalogo de servicios agendables y tabla de citas (Sprint 7).

Ninguna de las dos tablas estaba en `001_baseline`: el schema original de
Sprint 1 no contemplaba agendamiento. Se agregan aca, siguiendo la Regla 1 de
CLAUDE.md (client_id + FORCE RLS) igual que `002_rls_policies` hizo con las
demas tablas del baseline.

Sin `ondelete` en las FK, a proposito: es la misma convencion que el resto del
schema (ver `contact_tags`/`document_chunks`, que tampoco lo declaran) — un
borrado que rompa la referencia falla explicito en vez de arrastrar filas en
cascada silenciosamente.

`appointments.status` es `VARCHAR(50)`, no un tipo ENUM de Postgres como
`conversations.status`: la lista de estados es mas probable que crezca (ej.
"rescheduled") y no vale migrar un tipo ENUM cada vez que se agregue uno. Se
valida en el schema Pydantic (`APPOINTMENT_STATUSES`).

Revision ID: 004_service_types
Revises: 003_langgraph_checkpoints
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "004_service_types"
down_revision: str | None = "003_langgraph_checkpoints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_types",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("duration_minutes", sa.Integer(), server_default="60", nullable=False),
        sa.Column("buffer_minutes", sa.Integer(), server_default="15", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "name", name="uq_service_types_name"),
    )
    op.create_index(
        op.f("ix_service_types_client_id"), "service_types", ["client_id"], unique=False
    )

    op.create_table(
        "appointments",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("service_type_id", sa.UUID(), nullable=False),
        sa.Column("google_event_id", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=50), server_default="confirmed", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("cancelled_reason", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["service_type_id"], ["service_types.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_appointments_client_id"), "appointments", ["client_id"], unique=False)
    op.create_index(op.f("ix_appointments_contact_id"), "appointments", ["contact_id"], unique=False)
    op.create_index(
        "idx_appointments_starts_at", "appointments", ["client_id", "starts_at"], unique=False
    )

    for table in ("service_types", "appointments"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
                FOR ALL
                USING (client_id = current_setting('app.current_client_id')::uuid)
                WITH CHECK (client_id = current_setting('app.current_client_id')::uuid)
        """)


def downgrade() -> None:
    for table in ("appointments", "service_types"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("idx_appointments_starts_at", table_name="appointments")
    op.drop_index(op.f("ix_appointments_contact_id"), table_name="appointments")
    op.drop_index(op.f("ix_appointments_client_id"), table_name="appointments")
    op.drop_table("appointments")

    op.drop_index(op.f("ix_service_types_client_id"), table_name="service_types")
    op.drop_table("service_types")
