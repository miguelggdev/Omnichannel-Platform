"""satisfaction_surveys — Sprint 11: encuestas CSAT post-resolucion.

Tabla multi-tenant normal: `client_id` + FORCE RLS, mismo patron que
`011_outgoing_webhooks` (nada que ver con la excepcion de `tenant_templates` de
la migracion 010, que es una tabla de plataforma sin `client_id`).

`conversation_id` UNIQUE es la garantia de fondo de "una encuesta por
conversacion" (criterio 7 del spec): la comprobacion en Python de
`CSATService.schedule_survey()` es una proteccion adicional, no la unica.

Revision ID: 012_satisfaction_surveys
Revises: 011_outgoing_webhooks
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "012_satisfaction_surveys"
down_revision: str | None = "011_outgoing_webhooks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "satisfaction_surveys",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("survey_message_id", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="sent", nullable=False),
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
            "rating IS NULL OR (rating BETWEEN 1 AND 5)", name="ck_csat_rating_range"
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_satisfaction_surveys_client_id"), "satisfaction_surveys", ["client_id"]
    )
    # Indice UNICO, no una constraint aparte mas un indice plano: el modelo
    # (`mapped_column(unique=True, index=True)`) declara esto como un unico
    # indice unico, y con la constraint separada el autogenerate de Alembic
    # detectaba drift entre la migracion y los modelos (Alembic Migration
    # Check de CI). Ademas es lo correcto: un UNIQUE ya sirve de indice para
    # las busquedas por igualdad, un segundo indice plano al lado es redundante.
    op.create_index(
        op.f("ix_satisfaction_surveys_conversation_id"),
        "satisfaction_surveys",
        ["conversation_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_satisfaction_surveys_contact_id"), "satisfaction_surveys", ["contact_id"]
    )
    op.create_index(
        "idx_csat_client_sent_at", "satisfaction_surveys", ["client_id", "sent_at"], unique=False
    )

    op.execute("ALTER TABLE satisfaction_surveys ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE satisfaction_surveys FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON satisfaction_surveys
            FOR ALL
            USING (client_id = current_setting('app.current_client_id')::uuid)
            WITH CHECK (client_id = current_setting('app.current_client_id')::uuid)
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON satisfaction_surveys")
    op.execute("ALTER TABLE satisfaction_surveys NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE satisfaction_surveys DISABLE ROW LEVEL SECURITY")
    op.drop_index("idx_csat_client_sent_at", table_name="satisfaction_surveys")
    op.drop_index(op.f("ix_satisfaction_surveys_contact_id"), table_name="satisfaction_surveys")
    op.drop_index(
        op.f("ix_satisfaction_surveys_conversation_id"), table_name="satisfaction_surveys"
    )
    op.drop_index(op.f("ix_satisfaction_surveys_client_id"), table_name="satisfaction_surveys")
    op.drop_table("satisfaction_surveys")
