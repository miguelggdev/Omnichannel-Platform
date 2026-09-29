"""call_records y clinical_records — Sprint 13: canal de voz y agente clinico.

`call_records`: una fila por llamada de Twilio (`app/models/call_record.py`).
`contact_id` es nullable, a diferencia del spec: una llamada que se corta antes
de que el cliente diga nada no llega a tener contacto, y tiene que quedar
registrada igual. Los telefonos van cifrados (BYTEA, `EncryptedString`).

`clinical_records`: registro RIPS (`app/models/clinical_record.py`). Todo lo
que identifica al paciente o describe su salud va cifrado con pgcrypto,
incluidos los codigos CIE-10/CUPS que el spec dejaba en JSONB en claro. El
indice por documento se hace sobre `patient_document_hash` (indice ciego): el
que propone el spec, sobre el BYTEA cifrado con IV aleatorio, no encuentra
nada.

Las dos tablas llevan `client_id` + RLS con FORCE (regla 1). La segunda
politica del spec (`clinical_records_medical_access`, por rol) no se crea:
PostgreSQL combina las politicas permisivas con OR, asi que no restringiria
nada, y `app.current_user_role` no lo fija nadie. Ver el docstring del modelo.

`clinical_records` entra en el trigger de auditoria del Sprint 8
(`audit_trigger_function()`, migracion 006), como exige Habeas Data. Al estar
cifradas las columnas sensibles, lo que queda en `audit_logs.new_values` es el
ciphertext, no el dato medico.

Revision ID: 016_call_clinical_records
Revises: 015_list_active_tenants_function
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "016_call_clinical_records"
down_revision: str | None = "015_list_active_tenants_function"
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


def _timestamps() -> list[sa.Column[object]]:
    """Columnas `created_at` y `updated_at` comunes a las dos tablas.

    Returns:
        Las dos columnas.
    """
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "call_records",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("call_sid", sa.String(length=64), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=True),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        # Cifrados con pgp_sym_encrypt() desde la aplicacion (EncryptedString).
        sa.Column("phone_from", postgresql.BYTEA(), nullable=True),
        sa.Column("phone_to", postgresql.BYTEA(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "transcript",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("recording_url", sa.String(length=2048), nullable=True),
        sa.Column("recording_duration", sa.Integer(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('initiated', 'queued', 'ringing', 'in-progress', 'completed', "
            "'busy', 'failed', 'no-answer', 'canceled')",
            name="ck_call_records_status",
        ),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound')", name="ck_call_records_direction"
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_call_records_client_id"), "call_records", ["client_id"], unique=False)
    # El upsert de `voice_tasks.save_call_record()` hace ON CONFLICT sobre esto.
    op.create_index(
        "uq_call_records_call_sid", "call_records", ["client_id", "call_sid"], unique=True
    )
    op.create_index(
        "idx_call_records_client_started", "call_records", ["client_id", "started_at"], unique=False
    )
    op.create_index(
        "idx_call_records_contact", "call_records", ["client_id", "contact_id"], unique=False
    )
    _habilitar_rls("call_records")

    op.create_table(
        "clinical_records",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("call_record_id", sa.UUID(), nullable=True),
        sa.Column("patient_document_type", sa.String(length=5), nullable=True),
        sa.Column("patient_document_number", postgresql.BYTEA(), nullable=False),
        sa.Column("patient_document_hash", sa.String(length=64), nullable=False),
        sa.Column("patient_name", postgresql.BYTEA(), nullable=True),
        sa.Column("service_date", sa.Date(), nullable=False),
        sa.Column("service_type", sa.String(length=50), nullable=True),
        sa.Column("specialty", sa.String(length=100), nullable=True),
        sa.Column("provider_code", sa.String(length=20), nullable=True),
        # JSON cifrado (EncryptedJSON): un CIE-10 asociado a un paciente es dato
        # de salud.
        sa.Column("diagnosis_codes", postgresql.BYTEA(), nullable=True),
        sa.Column("procedure_codes", postgresql.BYTEA(), nullable=True),
        sa.Column("diagnosis_type", sa.String(length=20), nullable=True),
        sa.Column("raw_transcription", postgresql.BYTEA(), nullable=True),
        sa.Column("structured_notes", postgresql.BYTEA(), nullable=True),
        sa.Column("medical_entities", postgresql.BYTEA(), nullable=True),
        sa.Column("rips_type", sa.String(length=5), nullable=True),
        sa.Column("purpose_code", sa.String(length=5), nullable=True),
        sa.Column("external_cause", sa.String(length=5), nullable=True),
        sa.Column("discharge_status", sa.String(length=5), nullable=True),
        sa.Column("consent_given", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_type", sa.String(length=50), nullable=True),
        sa.Column("data_processing_authorized", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("reviewed_by", sa.UUID(), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('draft', 'reviewed', 'signed', 'submitted')",
            name="ck_clinical_records_status",
        ),
        sa.CheckConstraint(
            "rips_type IS NULL OR rips_type IN ('AC', 'AP', 'AU', 'AH')",
            name="ck_clinical_records_rips_type",
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["call_record_id"], ["call_records.id"]),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_clinical_records_client_id"), "clinical_records", ["client_id"], unique=False
    )
    op.create_index(
        "idx_clinical_records_client_date",
        "clinical_records",
        ["client_id", "service_date"],
        unique=False,
    )
    op.create_index(
        "idx_clinical_records_patient",
        "clinical_records",
        ["client_id", "patient_document_hash"],
        unique=False,
    )
    op.create_index(
        "idx_clinical_records_contact",
        "clinical_records",
        ["client_id", "contact_id"],
        unique=False,
    )
    _habilitar_rls("clinical_records")

    # Habeas Data: cada alta, cambio o baja de un registro clinico queda en
    # audit_logs. Reutiliza la funcion de la migracion 006.
    op.execute("""
        CREATE TRIGGER audit_clinical_records
            AFTER INSERT OR UPDATE OR DELETE ON clinical_records
            FOR EACH ROW EXECUTE FUNCTION audit_trigger_function();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_clinical_records ON clinical_records")
    _deshabilitar_rls("clinical_records")
    op.drop_index("idx_clinical_records_contact", table_name="clinical_records")
    op.drop_index("idx_clinical_records_patient", table_name="clinical_records")
    op.drop_index("idx_clinical_records_client_date", table_name="clinical_records")
    op.drop_index(op.f("ix_clinical_records_client_id"), table_name="clinical_records")
    op.drop_table("clinical_records")

    _deshabilitar_rls("call_records")
    op.drop_index("idx_call_records_contact", table_name="call_records")
    op.drop_index("idx_call_records_client_started", table_name="call_records")
    op.drop_index("uq_call_records_call_sid", table_name="call_records")
    op.drop_index(op.f("ix_call_records_client_id"), table_name="call_records")
    op.drop_table("call_records")
