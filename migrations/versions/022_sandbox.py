"""Sandbox por tenant — Sprint 14c: tenant clonado, historial de configuracion.

Diseno (ADR-078): el sandbox de un tenant es **otro tenant** (`clients.is_sandbox`)
con su propio `client_id`, no una columna `environment` en cada tabla de
configuracion. Asi las politicas RLS (que solo miran `client_id`) y los ~30
lectores de `agent_configs` no cambian.

- `clients.is_sandbox`: marca los tenants que son un sandbox, para sacarlos de los
  listados y de la facturacion.
- `tenant_sandboxes`: de que tenant es el sandbox. Vive en el tenant **de
  produccion** (RLS por su `client_id`): la fila `clients` del sandbox no es
  visible desde produccion, asi que la relacion no podia guardarse alli. Un
  sandbox por tenant (`UNIQUE (client_id)`).
- `config_history`: instantanea de la configuracion de produccion antes de cada
  publicacion o rollback, para poder volver atras. Una fila por tipo de
  configuracion y version.

Revision ID: 022_sandbox
Revises: 021_user_settings
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "022_sandbox"
down_revision: str | None = "021_user_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Congelados a proposito: una migracion no importa constantes de la aplicacion.
CONFIG_TYPES = ("agent_configs", "quick_replies")
HISTORY_REASONS = ("publish", "rollback")


def _habilitar_rls(tabla: str) -> None:
    """Activa RLS con FORCE y la politica de aislamiento por tenant.

    Args:
        tabla: Tabla sobre la que aplicar la politica.
    """
    op.execute(f"ALTER TABLE {tabla} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {tabla} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON {tabla}
            FOR ALL
            USING (client_id = current_setting('app.current_client_id')::uuid)
            WITH CHECK (client_id = current_setting('app.current_client_id')::uuid)
    """)


def _lista(valores: Sequence[str]) -> str:
    """`'a', 'b'` para un `IN (...)` de un CHECK."""
    return ", ".join(f"'{v}'" for v in valores)


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column("is_sandbox", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )

    op.create_table(
        "tenant_sandboxes",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("sandbox_client_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("client_id <> sandbox_client_id", name="ck_tenant_sandboxes_distinct"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["sandbox_client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", name="uq_tenant_sandboxes_client"),
        sa.UniqueConstraint("sandbox_client_id", name="uq_tenant_sandboxes_sandbox"),
    )
    op.create_index(
        op.f("ix_tenant_sandboxes_client_id"), "tenant_sandboxes", ["client_id"], unique=False
    )
    _habilitar_rls("tenant_sandboxes")

    op.create_table(
        "config_history",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("config_type", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("config_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"config_type IN ({_lista(CONFIG_TYPES)})", name="ck_config_history_type"
        ),
        sa.CheckConstraint(
            f"reason IN ({_lista(HISTORY_REASONS)})", name="ck_config_history_reason"
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "client_id", "config_type", "version", name="uq_config_history_version"
        ),
    )
    op.create_index(
        op.f("ix_config_history_client_id"), "config_history", ["client_id"], unique=False
    )
    _habilitar_rls("config_history")


def downgrade() -> None:
    """Quita las tablas y la columna; el historial de configuracion se pierde."""
    for tabla in ("config_history", "tenant_sandboxes"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {tabla}")
        op.drop_table(tabla)
    op.drop_column("clients", "is_sandbox")
