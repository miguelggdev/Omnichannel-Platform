"""Sprint 13, Dev B: extiende clinical_records y agrega patient_consents.

`clinical_records` y `call_records` los crea la migracion 016 (Dev A, PR #50);
esta la extiende para el agente clinico (ADR-072):

1. **`contact_id` pasa a opcional y aparece `dictated_by_contact_id`.** La 016
   modela el paciente como contacto del tenant, pero quien escribe al agente es
   el *profesional* que dicta; el paciente se identifica por su documento y
   puede no ser un contacto. `dictated_by_contact_id` registra al profesional
   ("registra siempre quien dicto", spec §9.1).
2. **`anonymized_at`** marca los registros anonimizados (derecho de supresion).
3. **`patient_consents`**: el spec busca el consentimiento en
   `clinical_records.data_processing_authorized`, pero ese registro no puede
   existir antes del consentimiento. Tabla propia: revocable y con quien la
   registro. Sin documento en claro: solo el indice ciego.
4. **Retencion de 20 anos por trigger** (`clinical_records_protect_function`).
   La historia clinica se conserva 20 anos desde la ultima atencion del
   paciente (Resolucion 839 de 2017). Un registro `signed`/`submitted` no se
   borra ni se modifica mientras corra ese plazo —solo puede pasar de `signed`
   a `submitted`—, aunque lo intente un admin o un bug: lo garantiza la base.
   El estado solo avanza `draft -> reviewed -> signed -> submitted`. Vencido el
   plazo se puede anonimizar (derecho de supresion).

No agrega un trigger de auditoria propio para `clinical_records`: la 016 ya lo
engancha al generico, y como sus campos sensibles van cifrados el `to_jsonb()`
solo copia bytes cifrados.

Revision ID: 017_clinical_records
Revises: 016_call_clinical_records
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "017_clinical_records"
down_revision: str | None = "016_call_clinical_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Congelado a proposito: una migracion no importa constantes de la aplicacion
# (cambiarlas no debe reescribir lo que ya corrio). Igual a `RETENTION_YEARS`.
RETENTION_YEARS = 20


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
    op.alter_column("clinical_records", "contact_id", existing_type=sa.UUID(), nullable=True)
    op.add_column("clinical_records", sa.Column("dictated_by_contact_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_clinical_records_dictated_by",
        "clinical_records",
        "contacts",
        ["dictated_by_contact_id"],
        ["id"],
    )
    op.add_column(
        "clinical_records", sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True)
    )

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
    op.execute("""
        CREATE TRIGGER audit_patient_consents
            AFTER INSERT OR UPDATE OR DELETE ON patient_consents
            FOR EACH ROW EXECUTE FUNCTION audit_trigger_function();
    """)

    op.execute(f"""
        CREATE OR REPLACE FUNCTION clinical_records_protect_function()
        RETURNS TRIGGER AS $$
        DECLARE
            _ultima DATE;
            _vencida BOOLEAN := FALSE;
            _limpio_old JSONB;
            _limpio_new JSONB;
        BEGIN
            IF TG_OP = 'UPDATE' AND NEW.status <> OLD.status AND NOT (
                (OLD.status = 'draft' AND NEW.status = 'reviewed')
                OR (OLD.status = 'reviewed' AND NEW.status = 'signed')
                OR (OLD.status = 'signed' AND NEW.status = 'submitted')
            ) THEN
                RAISE EXCEPTION 'Transicion de estado invalida: % -> %', OLD.status, NEW.status;
            END IF;

            IF OLD.status IN ('signed', 'submitted') THEN
                SELECT max(service_date) INTO _ultima
                FROM clinical_records
                WHERE client_id = OLD.client_id
                  AND patient_document_hash = OLD.patient_document_hash;
                _vencida := _ultima + INTERVAL '{RETENTION_YEARS} years' <= CURRENT_DATE;

                IF NOT _vencida THEN
                    IF TG_OP = 'DELETE' THEN
                        RAISE EXCEPTION
                            'La historia clinica firmada no se puede borrar antes de % anos desde la ultima atencion',
                            {RETENTION_YEARS};
                    END IF;
                    _limpio_old := to_jsonb(OLD) - 'status' - 'updated_at';
                    _limpio_new := to_jsonb(NEW) - 'status' - 'updated_at';
                    IF _limpio_old IS DISTINCT FROM _limpio_new THEN
                        RAISE EXCEPTION
                            'La historia clinica firmada no se puede modificar (retencion de % anos)',
                            {RETENTION_YEARS};
                    END IF;
                END IF;
            END IF;

            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER clinical_records_protect_trigger
            BEFORE UPDATE OR DELETE ON clinical_records
            FOR EACH ROW EXECUTE FUNCTION clinical_records_protect_function();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS clinical_records_protect_trigger ON clinical_records")
    op.execute("DROP FUNCTION IF EXISTS clinical_records_protect_function()")

    op.execute("DROP TRIGGER IF EXISTS audit_patient_consents ON patient_consents")
    _deshabilitar_rls("patient_consents")
    op.drop_index("idx_patient_consents_patient", table_name="patient_consents")
    op.drop_index(op.f("ix_patient_consents_client_id"), table_name="patient_consents")
    op.drop_table("patient_consents")

    op.drop_column("clinical_records", "anonymized_at")
    op.drop_constraint("fk_clinical_records_dictated_by", "clinical_records", type_="foreignkey")
    op.drop_column("clinical_records", "dictated_by_contact_id")
    # `contact_id` queda opcional: volver a NOT NULL falla si hay registros del agente.
