"""invoices y campaigns — Sprint 12: agentes financiero y de marketing.

Renumerada de 012 a 013 (hallazgo de /code-review sobre el PR #42): esta rama
partio de `main` antes de que el PR #43 (Sprint 11, Dev B) se mergeara, y las
dos usaron el numero 012 para migraciones distintas
(`012_satisfaction_surveys`, ya en `main`). Mismo tipo de choque que la
renumeracion de Sprint 7/8 documentada en PROGRESS.md.

Dos tablas multi-tenant normales: `client_id` + FORCE RLS, mismo patron que
004/005/011.

`invoices` no esta en el spec del sprint (su tabla de archivos solo lista
`campaigns`), pero §1.2 deja "Guardar en DB # ..." en `create_invoice()` y
hace que `get_invoice_status()`/`list_invoices()` devuelvan datos inventados:
sin esta tabla, tres de las cuatro tools del agente financiero son una
maqueta. Ver ADR-067.

Dos decisiones que se apartan del pseudocodigo del spec:

1. Los importes son `INTEGER` en centavos, no `FLOAT`/`NUMERIC` en pesos. El
   binario flotante no representa exactamente 0.1, y con IVA del 19% sobre
   varias lineas el error se ve en el total de una factura.
2. `buyer_nit` es `BYTEA` cifrado con pgcrypto (`EncryptedString`), como los
   identificadores de contacto desde la migracion 008: es el documento fiscal
   de un tercero.

Revision ID: 013_invoices_campaigns
Revises: 012_satisfaction_surveys
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "013_invoices_campaigns"
down_revision: str | None = "012_satisfaction_surveys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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


def _deshabilitar_rls(tabla: str) -> None:
    """Revierte lo que hizo `_habilitar_rls()`.

    Args:
        tabla: Tabla sobre la que revertir.
    """
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {tabla}")
    op.execute(f"ALTER TABLE {tabla} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {tabla} DISABLE ROW LEVEL SECURITY")


def upgrade() -> None:
    op.create_table(
        "invoices",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=True),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("invoice_number", sa.String(length=50), nullable=False),
        # Cifrado con pgp_sym_encrypt() desde la aplicacion (EncryptedString).
        sa.Column("buyer_nit", postgresql.BYTEA(), nullable=False),
        sa.Column("buyer_name", sa.String(length=300), nullable=False),
        sa.Column("items", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # Centavos: el dinero no va en flotante.
        sa.Column("subtotal_cents", sa.Integer(), nullable=False),
        sa.Column("tax_total_cents", sa.Integer(), nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="COP", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("dian_cufe", sa.String(length=200), nullable=True),
        sa.Column(
            "dian_response",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
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
        sa.CheckConstraint(
            "status IN ('draft', 'pending_dian', 'approved', 'rejected')",
            name="ck_invoices_status",
        ),
        sa.CheckConstraint(
            "total_cents = subtotal_cents + tax_total_cents", name="ck_invoices_total"
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_invoices_client_id"), "invoices", ["client_id"], unique=False)
    op.create_index(op.f("ix_invoices_contact_id"), "invoices", ["contact_id"], unique=False)
    # El consecutivo es unico dentro del tenant, no globalmente: cada tenant
    # tiene su propia numeracion autorizada.
    op.create_index(
        "uq_invoices_number", "invoices", ["client_id", "invoice_number"], unique=True
    )
    op.create_index(
        "idx_invoices_contact_status",
        "invoices",
        ["client_id", "contact_id", "status"],
        unique=False,
    )
    _habilitar_rls("invoices")

    op.create_table(
        "campaigns",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("segment_criteria", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("message_template", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("target_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("delivered_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("read_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("replied_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "error_log",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
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
        sa.CheckConstraint(
            "status IN ('draft', 'scheduled', 'sending', 'completed', 'failed')",
            name="ck_campaigns_status",
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_campaigns_client_id"), "campaigns", ["client_id"], unique=False)
    op.create_index(
        "idx_campaigns_client_status", "campaigns", ["client_id", "status"], unique=False
    )
    _habilitar_rls("campaigns")


def downgrade() -> None:
    _deshabilitar_rls("campaigns")
    op.drop_index("idx_campaigns_client_status", table_name="campaigns")
    op.drop_index(op.f("ix_campaigns_client_id"), table_name="campaigns")
    op.drop_table("campaigns")

    _deshabilitar_rls("invoices")
    op.drop_index("idx_invoices_contact_status", table_name="invoices")
    op.drop_index("uq_invoices_number", table_name="invoices")
    op.drop_index(op.f("ix_invoices_contact_id"), table_name="invoices")
    op.drop_index(op.f("ix_invoices_client_id"), table_name="invoices")
    op.drop_table("invoices")
