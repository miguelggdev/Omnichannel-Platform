"""voice_pins — Sprint 13: PIN por DTMF que autentica las llamadas (ADR-073).

El caller ID de una llamada se puede falsificar y por eso no autoriza a los
agentes clinico y de marketing (ADR-072). Esta tabla guarda, por numero
registrado, el PIN con el que un profesional demuestra quien es durante la
llamada. RLS con FORCE como el resto. Sin trigger de auditoria: copiaria el hash.

Revision ID: 020_voice_pins
Revises: 019_encrypt_call_transcript
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "020_voice_pins"
down_revision: str | None = "019_encrypt_call_transcript"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "voice_pins",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=False),
        sa.Column("phone_hash", sa.String(length=64), nullable=False),
        sa.Column("pin_hash", sa.String(length=255), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_voice_pins_client_id"), "voice_pins", ["client_id"], unique=False)
    op.create_index("uq_voice_pins_phone", "voice_pins", ["client_id", "phone_hash"], unique=True)
    op.create_index("idx_voice_pins_contact", "voice_pins", ["client_id", "contact_id"])
    op.execute("ALTER TABLE voice_pins ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE voice_pins FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON voice_pins
            FOR ALL
            USING (client_id = current_setting('app.current_client_id')::uuid)
            WITH CHECK (client_id = current_setting('app.current_client_id')::uuid)
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON voice_pins")
    op.drop_table("voice_pins")
