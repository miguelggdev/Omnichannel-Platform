"""tenant_templates — Sprint 10: clonacion de tenants desde plantillas.

`tenant_templates` y `template_instantiations` son, a proposito, la segunda
excepcion (la primera es `clients`) a la Regla 1 de CLAUDE.md: ninguna de las
dos lleva `client_id` ni RLS. No son datos de un tenant, son datos de la
plataforma. Un template es un snapshot que un `super_admin` toma de un tenant
origen (`source_client_id`, una columna de referencia, no de aislamiento) para
crear tenants nuevos a partir de el; la propia operacion de instanciar es
por definicion cross-tenant, porque el tenant destino ni siquiera existe
todavia cuando se hace la consulta. La RLS de `USING (client_id =
current_setting('app.current_client_id')::uuid)` no tiene ningun valor de
`current_client_id` que tenga sentido aqui — es el mismo caso que ya resolvio
`clients` (migracion 002), y se trata igual: sin `client_id`, control de
acceso por RBAC (`require_role("super_admin"/"admin")`) en los endpoints, no
por RLS. Ver ADR-064 en MEMORY.md.

`template_instantiations.progress` (JSONB) guarda, ademas del avance, el
password temporal del usuario admin del tenant nuevo (Sprint 10 no tiene
flujo de invitacion/reset todavia): visible solo a `super_admin` via
GET /admin/templates/instantiations/{id}, igual que el resto de esta tabla.

Revision ID: 010_tenant_templates
Revises: 009_blind_index_per_tenant
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "010_tenant_templates"
down_revision: str | None = "009_blind_index_per_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_templates",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_client_id", sa.UUID(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("is_public", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("version", sa.String(length=20), server_default="1.0", nullable=False),
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
        sa.ForeignKeyConstraint(["source_client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_tenant_templates_source_client_id"),
        "tenant_templates",
        ["source_client_id"],
        unique=False,
    )

    op.create_table(
        "template_instantiations",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("template_id", sa.UUID(), nullable=False),
        # NULL hasta que la task crea el Client nuevo.
        sa.Column("target_client_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "progress", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["template_id"], ["tenant_templates.id"]),
        sa.ForeignKeyConstraint(["target_client_id"], ["clients.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_template_instantiations_template_id"),
        "template_instantiations",
        ["template_id"],
        unique=False,
    )
    op.create_index(
        "idx_template_instantiations_status", "template_instantiations", ["status"], unique=False
    )


def downgrade() -> None:
    op.drop_index("idx_template_instantiations_status", table_name="template_instantiations")
    op.drop_index(
        op.f("ix_template_instantiations_template_id"), table_name="template_instantiations"
    )
    op.drop_table("template_instantiations")

    op.drop_index(op.f("ix_tenant_templates_source_client_id"), table_name="tenant_templates")
    op.drop_table("tenant_templates")
