"""tenant_webhooks y outgoing_webhook_logs — Sprint 11: webhooks salientes.

Dos tablas multi-tenant normales: `client_id` + FORCE RLS, mismo patron que
`004_service_types`/`005_agent_action_logs` (nada que ver con la excepcion de
`tenant_templates` de la migracion 010, que es una tabla de plataforma).

Dos desviaciones del SQL del spec, ambas a proposito:

1. `tenant_webhooks.secret` es `BYTEA`, no `VARCHAR(256)`. Es la credencial con
   la que se firma cada envio: se guarda cifrada con pgcrypto via
   `EncryptedString`, igual que `contact_identifiers.identifier_value` desde la
   migracion 008. Un `SELECT` sin la clave devuelve bytes.
2. La FK de `outgoing_webhook_logs.webhook_id` lleva `ON DELETE CASCADE` (el
   resto del repo no usa `ondelete`): el log no tiene sentido sin su webhook, y
   sin cascada el DELETE de un webhook con envios fallaria con violacion de FK.

Revision ID: 011_outgoing_webhooks
Revises: 010_tenant_templates
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "011_outgoing_webhooks"
down_revision: str | None = "010_tenant_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_webhooks",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        # Cifrada con pgp_sym_encrypt() desde la aplicacion (EncryptedString).
        sa.Column("secret", postgresql.BYTEA(), nullable=False),
        sa.Column("events", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column(
            "headers", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_reason", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_tenant_webhooks_client_id"), "tenant_webhooks", ["client_id"], unique=False
    )
    op.create_index(
        "idx_tenant_webhooks_active_events",
        "tenant_webhooks",
        ["client_id", "is_active"],
        unique=False,
        postgresql_where=sa.text("is_active"),
    )

    op.execute("ALTER TABLE tenant_webhooks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_webhooks FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON tenant_webhooks
            FOR ALL
            USING (client_id = current_setting('app.current_client_id')::uuid)
            WITH CHECK (client_id = current_setting('app.current_client_id')::uuid)
    """)

    op.create_table(
        "outgoing_webhook_logs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("webhook_id", sa.UUID(), nullable=False),
        sa.Column("event", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["webhook_id"], ["tenant_webhooks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_outgoing_webhook_logs_client_id"),
        "outgoing_webhook_logs",
        ["client_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_outgoing_webhook_logs_webhook_id"),
        "outgoing_webhook_logs",
        ["webhook_id"],
        unique=False,
    )
    op.create_index(
        "idx_webhook_logs_webhook_status",
        "outgoing_webhook_logs",
        ["webhook_id", "status"],
        unique=False,
    )
    op.create_index(
        "idx_webhook_logs_created_at", "outgoing_webhook_logs", ["created_at"], unique=False
    )

    op.execute("ALTER TABLE outgoing_webhook_logs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE outgoing_webhook_logs FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON outgoing_webhook_logs
            FOR ALL
            USING (client_id = current_setting('app.current_client_id')::uuid)
            WITH CHECK (client_id = current_setting('app.current_client_id')::uuid)
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON outgoing_webhook_logs")
    op.execute("ALTER TABLE outgoing_webhook_logs NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE outgoing_webhook_logs DISABLE ROW LEVEL SECURITY")
    op.drop_index("idx_webhook_logs_created_at", table_name="outgoing_webhook_logs")
    op.drop_index("idx_webhook_logs_webhook_status", table_name="outgoing_webhook_logs")
    op.drop_index(op.f("ix_outgoing_webhook_logs_webhook_id"), table_name="outgoing_webhook_logs")
    op.drop_index(op.f("ix_outgoing_webhook_logs_client_id"), table_name="outgoing_webhook_logs")
    op.drop_table("outgoing_webhook_logs")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON tenant_webhooks")
    op.execute("ALTER TABLE tenant_webhooks NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_webhooks DISABLE ROW LEVEL SECURITY")
    op.drop_index("idx_tenant_webhooks_active_events", table_name="tenant_webhooks")
    op.drop_index(op.f("ix_tenant_webhooks_client_id"), table_name="tenant_webhooks")
    op.drop_table("tenant_webhooks")
