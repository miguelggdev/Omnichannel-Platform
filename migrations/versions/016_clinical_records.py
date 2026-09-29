"""clinical_records y patient_consents — Sprint 13: agente clinico (RIPS).

Dos tablas multi-tenant normales: `client_id` + FORCE RLS, mismo patron que
004/005/011/013. Se aparta del SQL del spec (§8.3) en tres cosas (ADR-071):

1. **Sin la politica `clinical_records_medical_access`.** Lee
   `current_setting('app.current_user_role')`, un parametro que nada en el repo
   fija (`tenant_session()` solo fija `app.current_client_id` y
   `app.current_user_id`), y sin `missing_ok` esa politica revienta con
   "unrecognized configuration parameter" en cada consulta. Ademas las
   politicas permisivas se suman con OR: con `tenant_isolation FOR ALL` al
   lado, la del rol medico no restringiria nada. El acceso a los datos clinicos
   se controla en la aplicacion (solo los profesionales que el tenant declara
   en `agent_configs.config.clinical.professional_contact_ids`, y solo
   `admin`/`super_admin` por la API).
2. **Trigger de auditoria propio para `clinical_records`.** El generico
   (`audit_trigger_function()`, migracion 006) copia la fila entera con
   `to_jsonb(NEW)` a `audit_logs.old_values/new_values`: diagnosticos, procedimientos
   y notas SOAP quedarian ahi, legibles para cualquier admin del tenant y
   fuera del cifrado y de la anonimizacion. El trigger nuevo solo guarda
   metadatos (ids, fechas, estado): que se creo, quien y cuando, no el
   contenido clinico. `patient_consents` no lleva contenido sensible (solo el
   indice ciego) y usa el generico.
3. **Documento cifrado + indice ciego** (`patient_document_hash`), como los
   identificadores de contacto desde 008/009, y `patient_consents` como tabla
   aparte (ver `app/models/clinical_record.py`).

`call_record_id` no tiene FK: `call_records` es del canal de voz (Dev A) y no
existe todavia; su migracion la agrega.

Revision ID: 016_clinical_records
Revises: 015_list_active_tenants_function
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "016_clinical_records"
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


def upgrade() -> None:
    op.create_table(
        "clinical_records",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=True),
        sa.Column("dictated_by_contact_id", sa.UUID(), nullable=True),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("call_record_id", sa.UUID(), nullable=True),
        sa.Column("patient_document_type", sa.String(length=5), nullable=True),
        # Cifrados con pgp_sym_encrypt() desde la aplicacion (EncryptedString).
        sa.Column("patient_document_number", postgresql.BYTEA(), nullable=False),
        sa.Column("patient_document_hash", sa.String(length=64), nullable=False),
        sa.Column("patient_name", postgresql.BYTEA(), nullable=True),
        sa.Column("service_date", sa.Date(), nullable=False),
        sa.Column("service_type", sa.String(length=50), nullable=True),
        sa.Column("specialty", sa.String(length=100), nullable=True),
        sa.Column("provider_code", sa.String(length=20), nullable=True),
        sa.Column(
            "diagnosis_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "procedure_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("diagnosis_type", sa.String(length=20), nullable=True),
        sa.Column("raw_transcription", postgresql.BYTEA(), nullable=True),
        sa.Column(
            "structured_notes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "medical_entities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
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
        sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('draft', 'reviewed', 'signed', 'submitted')",
            name="ck_clinical_records_status",
        ),
        sa.CheckConstraint(
            "rips_type IS NULL OR rips_type IN ('AC', 'AP', 'AU', 'AH')",
            name="ck_clinical_records_rips_type",
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
        sa.ForeignKeyConstraint(["dictated_by_contact_id"], ["contacts.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
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
    _habilitar_rls("clinical_records")

    op.create_table(
        "patient_consents",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("patient_document_type", sa.String(length=5), nullable=False),
        sa.Column("patient_document_hash", sa.String(length=64), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=True),
        sa.Column("data_category", sa.String(length=30), server_default="health", nullable=False),
        sa.Column("consent_type", sa.String(length=20), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("registered_by_contact_id", sa.UUID(), nullable=True),
        sa.Column("registered_by_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "consent_type IN ('verbal', 'digital', 'written')",
            name="ck_patient_consents_type",
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"]),
        sa.ForeignKeyConstraint(["registered_by_contact_id"], ["contacts.id"]),
        sa.ForeignKeyConstraint(["registered_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_patient_consents_client_id"), "patient_consents", ["client_id"], unique=False
    )
    op.create_index(
        "idx_patient_consents_patient",
        "patient_consents",
        ["client_id", "patient_document_hash"],
        unique=False,
    )
    _habilitar_rls("patient_consents")

    # Auditoria de clinical_records SIN contenido clinico (ver el docstring).
    # SECURITY DEFINER por la misma razon que en 006: el rol de la aplicacion
    # no puede escribir en audit_logs con UPDATE/DELETE revocados a PUBLIC.
    op.execute("""
        CREATE OR REPLACE FUNCTION clinical_audit_trigger_function()
        RETURNS TRIGGER AS $$
        DECLARE
            _client_id UUID;
            _user_id UUID;
            _old JSONB;
            _new JSONB;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                _client_id := OLD.client_id;
            ELSE
                _client_id := NEW.client_id;
            END IF;

            BEGIN
                _user_id := NULLIF(current_setting('app.current_user_id', true), '')::UUID;
            EXCEPTION WHEN OTHERS THEN
                _user_id := NULL;
            END;

            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                _old := jsonb_build_object(
                    'id', OLD.id,
                    'contact_id', OLD.contact_id,
                    'dictated_by_contact_id', OLD.dictated_by_contact_id,
                    'service_date', OLD.service_date,
                    'rips_type', OLD.rips_type,
                    'status', OLD.status,
                    'reviewed_by', OLD.reviewed_by,
                    'signed_at', OLD.signed_at,
                    'anonymized_at', OLD.anonymized_at
                );
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                _new := jsonb_build_object(
                    'id', NEW.id,
                    'contact_id', NEW.contact_id,
                    'dictated_by_contact_id', NEW.dictated_by_contact_id,
                    'service_date', NEW.service_date,
                    'rips_type', NEW.rips_type,
                    'status', NEW.status,
                    'reviewed_by', NEW.reviewed_by,
                    'signed_at', NEW.signed_at,
                    'anonymized_at', NEW.anonymized_at
                );
            END IF;

            INSERT INTO audit_logs (
                client_id, table_name, record_id, action, old_values, new_values, user_id
            )
            VALUES (
                _client_id,
                TG_TABLE_NAME,
                CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END,
                TG_OP::audit_action,
                _old,
                _new,
                _user_id
            );

            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            ELSE
                RETURN NEW;
            END IF;
        END;
        $$ LANGUAGE plpgsql SECURITY DEFINER;
    """)
    op.execute("""
        CREATE TRIGGER audit_clinical_records
            AFTER INSERT OR UPDATE OR DELETE ON clinical_records
            FOR EACH ROW EXECUTE FUNCTION clinical_audit_trigger_function();
    """)
    op.execute("""
        CREATE TRIGGER audit_patient_consents
            AFTER INSERT OR UPDATE OR DELETE ON patient_consents
            FOR EACH ROW EXECUTE FUNCTION audit_trigger_function();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_patient_consents ON patient_consents")
    op.execute("DROP TRIGGER IF EXISTS audit_clinical_records ON clinical_records")
    op.execute("DROP FUNCTION IF EXISTS clinical_audit_trigger_function()")

    _deshabilitar_rls("patient_consents")
    op.drop_index("idx_patient_consents_patient", table_name="patient_consents")
    op.drop_index(op.f("ix_patient_consents_client_id"), table_name="patient_consents")
    op.drop_table("patient_consents")

    _deshabilitar_rls("clinical_records")
    op.drop_index("idx_clinical_records_patient", table_name="clinical_records")
    op.drop_index("idx_clinical_records_client_date", table_name="clinical_records")
    op.drop_index(op.f("ix_clinical_records_client_id"), table_name="clinical_records")
    op.drop_table("clinical_records")
