"""audit_gdpr_quick_replies — Sprint 8 (Dev B).

Tres cosas que el Sprint 8 necesita del schema:

1. `audit_logs` + el trigger que la alimenta. El trigger vive en PostgreSQL, no
   en Python (spec §9.2 y Notas Tecnicas): asi queda auditado cualquier cambio,
   venga de la API, de un worker de Celery o de un `psql` a mano.
2. Las dos columnas de RGPD en `contacts` para poder anonimizar sin borrar la
   fila (spec §10.2), que romperia la integridad referencial de las
   conversaciones y falsearia las estadisticas agregadas.
3. `quick_replies.shortcut` y `quick_replies.created_by`, que la spec §11.1 da
   por hechas y el modelo de Sprint 1 nunca tuvo.

Sobre que tablas lleva trigger
------------------------------
`contacts`, `conversations` y `messages`. La spec tambien pide `users`, pero se
deja fuera a proposito: el login (`app/api/v1/auth.py`) busca al usuario por
email **sin contexto de tenant** y le escribe `last_login_at`. Un trigger ahi
intentaria insertar en `audit_logs`, cuya politica de RLS evalua
`current_setting('app.current_client_id')`, que en esa transaccion no esta
definido. Ver la nota de BUG-025 en MEMORY.md: el login ya tiene un problema
propio con la RLS de `users`, anterior a este sprint, y auditarla lo taparia
detras de un error distinto. Cuando ese bug se cierre, agregar el trigger de
`users` es una migracion de una linea.

Renumerada de 004 a 006 (sesion 22): esta migracion se escribio en una rama
que partio de main antes de que el Sprint 7 (Dev A, PR #19) se mergeara, y
"004" ya quedo tomado por 004_service_types. Encadenada ahora tras
005_agent_action_logs, no tras 003_langgraph_checkpoints.

Revision ID: 006_audit_gdpr_quick_replies
Revises: 005_agent_action_logs
Create Date: 2026-09-17
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "006_audit_gdpr_quick_replies"
down_revision: Union[str, None] = "005_agent_action_logs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tablas que quedan auditadas. Ver la nota de arriba sobre `users`.
AUDITED_TABLES = ("contacts", "conversations", "messages")


def upgrade() -> None:
    # ─── 1. audit_logs ──────────────────────────────────────────────────────
    # El enum `audit_action` lo crea SQLAlchemy al ejecutar create_table con
    # sa.Enum(), igual que los cinco de 001_baseline. Crearlo antes a mano y
    # dejar que create_table lo cree otra vez es lo que rompio la primera
    # version de esta migracion ("type audit_action already exists").
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("table_name", sa.String(length=100), nullable=False),
        sa.Column("record_id", sa.UUID(), nullable=False),
        sa.Column(
            "action",
            sa.Enum("INSERT", "UPDATE", "DELETE", name="audit_action"),
            nullable=False,
        ),
        sa.Column("old_values", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("new_values", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        # ON DELETE CASCADE: borrar un tenant se lleva su rastro. Sin esto, la
        # FK bloquea el DELETE de `clients` y cualquier limpieza (la de los
        # tests, y la baja de un cliente en produccion) falla por una tabla que
        # nadie escribio a mano.
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        # SET NULL: dar de baja a un empleado no debe borrar lo que hizo; la
        # fila queda, con el autor en NULL.
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_logs_client_id"), "audit_logs", ["client_id"], unique=False)
    op.create_index(op.f("ix_audit_logs_table_name"), "audit_logs", ["table_name"], unique=False)
    op.create_index(op.f("ix_audit_logs_record_id"), "audit_logs", ["record_id"], unique=False)
    # Consulta tipica: "que paso en este tenant ultimamente". El indice
    # compuesto sirve al WHERE y al ORDER BY a la vez. Sin DESC a proposito:
    # PostgreSQL recorre un btree hacia atras igual de bien, y un indice por
    # expresion no lo compara de forma fiable `alembic check`.
    op.create_index(
        "ix_audit_logs_client_created",
        "audit_logs",
        ["client_id", "created_at"],
        unique=False,
    )

    # RLS, igual que las otras 18 tablas (CLAUDE.md regla 1).
    op.execute("ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_logs FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON audit_logs
            FOR ALL
            USING (client_id = current_setting('app.current_client_id')::uuid)
            WITH CHECK (client_id = current_setting('app.current_client_id')::uuid)
    """)

    # Nadie deberia reescribir el pasado. Esto quita el permiso que viene por
    # PUBLIC, pero NO convierte la tabla en append-only por si solo: un `GRANT
    # UPDATE, DELETE ON ALL TABLES` posterior al rol de la aplicacion (que es lo
    # que hacen tanto el provisioning de CI como el de Supabase) vuelve a
    # concederlo. Para que el rastro sea de verdad inmutable hay que revocarlo
    # sobre el rol concreto **despues** de conceder el resto, en el script que
    # crea el rol; queda anotado en PROGRESS.md como tarea de infraestructura.
    op.execute("REVOKE UPDATE, DELETE ON audit_logs FROM PUBLIC")

    # ─── 2. Trigger de auditoria ────────────────────────────────────────────
    # SECURITY DEFINER: corre con los privilegios del dueno de la funcion, no
    # con los del rol de la aplicacion, que tiene revocado UPDATE/DELETE.
    op.execute("""
        CREATE OR REPLACE FUNCTION audit_trigger_function()
        RETURNS TRIGGER AS $$
        DECLARE
            _client_id UUID;
            _user_id UUID;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                _client_id := OLD.client_id;
            ELSE
                _client_id := NEW.client_id;
            END IF;

            -- El segundo argumento (missing_ok) evita que reviente cuando la
            -- transaccion no tiene usuario: un worker de Celery actua en nombre
            -- del sistema, no de una persona, y ahi user_id queda NULL.
            BEGIN
                _user_id := NULLIF(current_setting('app.current_user_id', true), '')::UUID;
            EXCEPTION WHEN OTHERS THEN
                _user_id := NULL;
            END;

            INSERT INTO audit_logs (
                client_id, table_name, record_id, action, old_values, new_values, user_id
            )
            VALUES (
                _client_id,
                TG_TABLE_NAME,
                CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END,
                TG_OP::audit_action,
                CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN to_jsonb(OLD) ELSE NULL END,
                CASE WHEN TG_OP IN ('INSERT', 'UPDATE') THEN to_jsonb(NEW) ELSE NULL END,
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

    for tabla in AUDITED_TABLES:
        op.execute(f"""
            CREATE TRIGGER audit_{tabla}
                AFTER INSERT OR UPDATE OR DELETE ON {tabla}
                FOR EACH ROW EXECUTE FUNCTION audit_trigger_function();
        """)  # noqa: S608

    # ─── 3. Columnas de RGPD en contacts ────────────────────────────────────
    op.add_column(
        "contacts",
        sa.Column("is_gdpr_deleted", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "contacts", sa.Column("gdpr_deleted_at", sa.DateTime(timezone=True), nullable=True)
    )

    # ─── 4. quick_replies: shortcut y created_by ────────────────────────────
    op.add_column("quick_replies", sa.Column("shortcut", sa.String(length=50), nullable=True))
    # Backfill antes de poner NOT NULL: la tabla nunca tuvo endpoint que la
    # escribiera, asi que en la practica esta vacia, pero una migracion no puede
    # dar eso por hecho. El atajo sale del titulo, saneado, mas un sufijo del id
    # para no chocar con el unique de (client_id, shortcut).
    op.execute("""
        UPDATE quick_replies
        SET shortcut = '/' || left(
                regexp_replace(lower(coalesce(title, 'atajo')), '[^a-z0-9]+', '-', 'g'), 32
            ) || '-' || left(id::text, 8)
        WHERE shortcut IS NULL
    """)
    op.alter_column("quick_replies", "shortcut", nullable=False)
    op.create_unique_constraint(
        "uq_quick_reply_shortcut", "quick_replies", ["client_id", "shortcut"]
    )
    op.add_column("quick_replies", sa.Column("created_by", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_quick_replies_created_by", "quick_replies", "users", ["created_by"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_quick_replies_created_by", "quick_replies", type_="foreignkey")
    op.drop_column("quick_replies", "created_by")
    op.drop_constraint("uq_quick_reply_shortcut", "quick_replies", type_="unique")
    op.drop_column("quick_replies", "shortcut")

    op.drop_column("contacts", "gdpr_deleted_at")
    op.drop_column("contacts", "is_gdpr_deleted")

    for tabla in AUDITED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS audit_{tabla} ON {tabla}")  # noqa: S608
    op.execute("DROP FUNCTION IF EXISTS audit_trigger_function()")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON audit_logs")
    op.drop_index("ix_audit_logs_client_created", table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_record_id"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_table_name"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_client_id"), table_name="audit_logs")
    op.drop_table("audit_logs")
    op.execute("DROP TYPE IF EXISTS audit_action")
