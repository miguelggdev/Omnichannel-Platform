"""lead_activities (#30) y deduplicacion de leads por URL de LinkedIn — cierre del Sprint 16.

1. `lead_activities`: historial de cada lead (creacion, cambio de etapa, asignacion, enlace con
   contacto...). El spec la asigna al Sprint 17, pero sin ella mover de etapa o reasignar no deja
   rastro y el Kanban queda sin historia; el Sprint 17 solo anade tipos de actividad. La fila
   guarda **ids y slugs, nunca datos personales** (por eso la anonimizacion RGPD de un lead no
   tiene que tocarla) y `user_id` pasa a NULL si el usuario se borra.

2. Un lead por URL de LinkedIn y tenant (entre los no borrados), igual que ya ocurre con email y
   telefono. Hace falta para importar perfiles de LinkedIn que no traen email ni telefono: sin
   ello cada importacion o webhook de Phantombuster duplicaria a todo el mundo.

   La URL se guarda en forma canonica (https, minusculas, sin parametros ni `/` final: el
   listener de `Lead` y `canonicalizar_linkedin()` hacen lo mismo que el SQL de aqui). Los datos
   existentes se canonizan primero y, si ya habia duplicados, **el mas antiguo conserva la URL y
   los demas la guardan en `enrichment_data["linkedin_url_duplicado"]`** y quedan con la columna
   a NULL: no se pierde ningun dato ni falla la migracion.

Revision ID: 026_lead_activities_linkedin
Revises: 025_lead_management
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "026_lead_activities_linkedin"
down_revision: str | None = "025_lead_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLITICA = "client_id = current_setting('app.current_client_id')::uuid"

# Debe hacer lo mismo que `app.models.lead.canonicalizar_linkedin()`.
CANONICA_SQL = (
    "lower(regexp_replace(regexp_replace(regexp_replace(trim(linkedin_url), '[?#].*$', ''), "
    "'/+$', ''), '^http://', 'https://', 'i'))"
)


def upgrade() -> None:
    """Crea `lead_activities`, canoniza las URL de LinkedIn y crea el indice unico."""
    op.create_table(
        "lead_activities",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("lead_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("activity_type", sa.String(50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(op.f("ix_lead_activities_client_id"), "lead_activities", ["client_id"])
    op.create_index(
        "ix_lead_activities_lead", "lead_activities", ["lead_id", sa.text("created_at DESC")]
    )
    op.execute("ALTER TABLE lead_activities ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE lead_activities FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON lead_activities
            FOR ALL
            USING ({_POLITICA})
            WITH CHECK ({_POLITICA})
    """)

    # La migracion corre con el rol propietario (BYPASSRLS), asi que ve todos los tenants.
    op.execute(
        f"UPDATE leads SET linkedin_url = {CANONICA_SQL} WHERE linkedin_url IS NOT NULL"  # noqa: S608
    )
    op.execute("""
        WITH ordenados AS (
            SELECT id, row_number() OVER (
                PARTITION BY client_id, linkedin_url ORDER BY created_at, id
            ) AS puesto
            FROM leads
            WHERE linkedin_url IS NOT NULL AND deleted_at IS NULL
        )
        UPDATE leads l
        SET enrichment_data = l.enrichment_data
                || jsonb_build_object('linkedin_url_duplicado', l.linkedin_url),
            linkedin_url = NULL
        FROM ordenados o
        WHERE l.id = o.id AND o.puesto > 1
    """)
    op.create_index(
        "uq_leads_client_linkedin",
        "leads",
        ["client_id", "linkedin_url"],
        unique=True,
        postgresql_where=sa.text("linkedin_url IS NOT NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    """Quita el indice y la tabla (las URL canonizadas no se restauran)."""
    op.drop_index("uq_leads_client_linkedin", table_name="leads")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON lead_activities")
    op.execute("ALTER TABLE lead_activities NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE lead_activities DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_lead_activities_lead", table_name="lead_activities")
    op.drop_index(op.f("ix_lead_activities_client_id"), table_name="lead_activities")
    op.drop_table("lead_activities")
