"""agent_action_logs — traza de actividad de los nodos del grafo (addendum Sprint 7).

Reprogramado desde Sprint 6 (ver la nota al inicio de
`specs/sprint-07-addendum-agent-logging.md`). Mismo patron que
`004_service_types`: tabla nueva con client_id + FORCE RLS, sin ondelete en
las FK.

Indice parcial `idx_agent_action_logs_error_status` (solo filas con
`status = 'error'`): las consultas de errores recientes
(`app/api/v1/agent_logs.py::recent_errors`) filtran siempre por ese status, y
un indice parcial pesa una fraccion de uno completo en una tabla que crece un
registro por nodo y por mensaje.

Revision ID: 005_agent_action_logs
Revises: 004_service_types
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "005_agent_action_logs"
down_revision: str | None = "004_service_types"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_action_logs",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("message_id", sa.UUID(), nullable=True),
        sa.Column("node_name", sa.String(length=100), nullable=False),
        sa.Column("action_type", sa.String(length=50), nullable=False),
        sa.Column("input_summary", sa.Text(), nullable=True),
        sa.Column("output_summary", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("tokens_used", sa.Integer(), server_default="0", nullable=True),
        sa.Column("model_used", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="success", nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agent_action_logs_client_id"), "agent_action_logs", ["client_id"], unique=False
    )
    op.create_index(
        op.f("ix_agent_action_logs_conversation_id"),
        "agent_action_logs",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_action_logs_node_name"), "agent_action_logs", ["node_name"], unique=False
    )
    op.create_index(
        "idx_agent_action_logs_created_at", "agent_action_logs", ["created_at"], unique=False
    )
    op.create_index(
        "idx_agent_action_logs_error_status",
        "agent_action_logs",
        ["status"],
        unique=False,
        postgresql_where=sa.text("status = 'error'"),
    )

    op.execute("ALTER TABLE agent_action_logs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_action_logs FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON agent_action_logs
            FOR ALL
            USING (client_id = current_setting('app.current_client_id')::uuid)
            WITH CHECK (client_id = current_setting('app.current_client_id')::uuid)
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON agent_action_logs")
    op.execute("ALTER TABLE agent_action_logs NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_action_logs DISABLE ROW LEVEL SECURITY")

    op.drop_index("idx_agent_action_logs_error_status", table_name="agent_action_logs")
    op.drop_index("idx_agent_action_logs_created_at", table_name="agent_action_logs")
    op.drop_index(op.f("ix_agent_action_logs_node_name"), table_name="agent_action_logs")
    op.drop_index(op.f("ix_agent_action_logs_conversation_id"), table_name="agent_action_logs")
    op.drop_index(op.f("ix_agent_action_logs_client_id"), table_name="agent_action_logs")
    op.drop_table("agent_action_logs")
