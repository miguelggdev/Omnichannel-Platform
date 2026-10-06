"""Lead management — Sprint 16: etapas del pipeline, fuentes y leads.

Tablas nuevas (todas con `client_id`, RLS habilitada y FORCE):

- `lead_pipeline_stages` (#27): etapas configurables por tenant.
- `lead_sources` (#29): fuentes de captura. Su token publico no se guarda: solo su SHA-256.
- `leads` (#28): la entidad principal.

Y en tablas existentes: `clients.icp_config`, `clients.lead_scoring_weights`,
`contacts.lead_id` y `contacts.is_lead`. (`clients.lead_management_enabled` y
`clients.theme_config` ya existian desde `001_baseline`.)

Desviaciones deliberadas sobre `specs/sprint-16-19-lead-management.md`
-----------------------------------------------------------------------
- **`email` y `phone` cifrados** (pgcrypto, `BYTEA`) con indice ciego por tenant
  (`email_hash`, `phone_hash`), igual que `contact_identifiers` (migraciones 008/009): CLAUDE.md
  exige cifrar telefonos y emails. El UNIQUE de deduplicacion va sobre el hash y es parcial
  (`deleted_at IS NULL`): borrar un lead (soft delete) no impide volver a capturarlo.
- **`total_score` es una columna normal, no `GENERATED`**. El spec la define con los pesos
  40/30/30 fijos y a la vez dice (ADR-022) que los pesos son configurables por tenant
  (`clients.lead_scoring_weights`): una columna generada no puede leer otra tabla. Lo calcula la
  aplicacion con `app.services.lead_scoring.compute_total_score()`.
- **Sin ENUM de PostgreSQL** (`lead_source_type`, `lead_stage_type`): `ALTER TYPE ... ADD VALUE` no
  es transaccional y las etapas ya son datos por tenant. Se usa `VARCHAR` + `CHECK` y la lista
  vive en el modelo. Los enums de deals y llamadas (Sprints 18-19) se crean cuando se usen.
- **Sin `lead_sources.leads_count`**: un contador desnormalizado se desincroniza con el soft
  delete, las importaciones y la concurrencia; `COUNT(*)` sobre `ix_leads_client_source` basta.
- **`UNIQUE (client_id, position)` es `DEFERRABLE INITIALLY DEFERRED`**: sin eso, reordenar dos
  etapas (intercambiar posiciones) falla a mitad de la transaccion.
- **`capture_token_hash` + `capture_lookup_source()`**: la captura publica
  (`POST /capture/{token}`) llega sin JWT, asi que no hay tenant que fijar y la RLS de
  `lead_sources` no dejaria ver nada. Como `auth_lookup_user()` (007) y `admin_list_clients()`
  (024), se resuelve con una funcion `SECURITY DEFINER` que solo busca por hash y devuelve lo
  imprescindible. Se salta la RLS solo si su dueno tiene `BYPASSRLS`; la migracion lo comprueba.
- **`ON DELETE SET NULL`** en las FK de `leads` hacia contacto, fuente y usuario: borrar uno de
  ellos no debe borrar ni bloquear el historial del lead. `pipeline_stage_id` queda en `NO
  ACTION`: no se puede borrar una etapa con leads.

Las FK entre tablas **no** garantizan el mismo tenant (un `pipeline_stage_id` de otro tenant
seria una FK valida): quien escribe leads debe resolver etapa, fuente y usuario dentro de
`tenant_session()`, donde RLS ya oculta los de otros tenants.

Revision ID: 025_lead_management
Revises: 024_admin_list_clients
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "025_lead_management"
down_revision: str | None = "024_admin_list_clients"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NUEVAS = ("lead_pipeline_stages", "lead_sources", "leads")
_POLITICA = "client_id = current_setting('app.current_client_id')::uuid"
SOURCE_TYPES = (
    "web_form",
    "linkedin",
    "facebook_ad",
    "google_ad",
    "instagram",
    "referral",
    "manual",
    "api",
    "whatsapp",
    "import",
)


def _uuid_pk() -> sa.Column[sa.UUID]:
    return sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _created_at() -> sa.Column[sa.DateTime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    """Crea las tablas, las columnas nuevas, la RLS y `capture_lookup_source()`."""
    # ── clients ──────────────────────────────────────────────────────────────────────
    op.add_column(
        "clients",
        sa.Column("icp_config", postgresql.JSONB(), server_default="{}", nullable=False),
    )
    op.add_column(
        "clients",
        sa.Column(
            "lead_scoring_weights",
            postgresql.JSONB(),
            server_default='{"fit": 40, "behavioral": 30, "ai": 30}',
            nullable=False,
        ),
    )

    # ── lead_pipeline_stages (#27) ───────────────────────────────────────────────────
    op.create_table(
        "lead_pipeline_stages",
        _uuid_pk(),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(50), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("color", sa.String(7), nullable=True),
        sa.Column("auto_actions", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("is_terminal", sa.Boolean(), server_default="false", nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.UniqueConstraint("client_id", "slug", name="uq_lead_stage_slug"),
        sa.UniqueConstraint(
            "client_id",
            "position",
            name="uq_lead_stage_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint("position >= 0", name="ck_lead_stage_position"),
        sa.CheckConstraint(
            "color IS NULL OR color ~ '^#[0-9a-fA-F]{6}$'", name="ck_lead_stage_color"
        ),
    )
    op.create_index(
        op.f("ix_lead_pipeline_stages_client_id"), "lead_pipeline_stages", ["client_id"]
    )

    # ── lead_sources (#29) ───────────────────────────────────────────────────────────
    op.create_table(
        "lead_sources",
        _uuid_pk(),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("config", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("utm_tracking", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("capture_token_hash", sa.String(64), nullable=True),
        _created_at(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.CheckConstraint(
            "source_type IN (" + ", ".join(f"'{t}'" for t in SOURCE_TYPES) + ")",
            name="ck_lead_source_type",
        ),
    )
    op.create_index(op.f("ix_lead_sources_client_id"), "lead_sources", ["client_id"])
    op.create_index(
        "ix_lead_sources_capture_token_hash",
        "lead_sources",
        ["capture_token_hash"],
        unique=True,
        postgresql_where=sa.text("capture_token_hash IS NOT NULL"),
    )

    # ── leads (#28) ──────────────────────────────────────────────────────────────────
    op.create_table(
        "leads",
        _uuid_pk(),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=True),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column("assigned_user_id", sa.UUID(), nullable=True),
        sa.Column("pipeline_stage_id", sa.UUID(), nullable=True),
        sa.Column("first_name", sa.String(100), nullable=True),
        sa.Column("last_name", sa.String(100), nullable=True),
        sa.Column("email", postgresql.BYTEA(), nullable=True),
        sa.Column("email_hash", sa.String(64), nullable=True),
        sa.Column("phone", postgresql.BYTEA(), nullable=True),
        sa.Column("phone_hash", sa.String(64), nullable=True),
        sa.Column("linkedin_url", sa.String(500), nullable=True),
        sa.Column("company_name", sa.String(200), nullable=True),
        sa.Column("company_domain", sa.String(255), nullable=True),
        sa.Column("company_size", sa.String(50), nullable=True),
        sa.Column("industry", sa.String(100), nullable=True),
        sa.Column("job_title", sa.String(150), nullable=True),
        sa.Column("fit_score", sa.Integer(), server_default="0", nullable=False),
        sa.Column("behavioral_score", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ai_score", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_score", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("temperature", sa.String(10), server_default="cold", nullable=False),
        sa.Column("estimated_value", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(3), server_default="USD", nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_follow_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disqualified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disqualified_reason", sa.Text(), nullable=True),
        sa.Column("enrichment_data", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_id"], ["lead_sources.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["assigned_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["pipeline_stage_id"], ["lead_pipeline_stages.id"]),
        sa.CheckConstraint("fit_score BETWEEN 0 AND 100", name="ck_leads_fit_score"),
        sa.CheckConstraint("behavioral_score BETWEEN 0 AND 100", name="ck_leads_behavioral_score"),
        sa.CheckConstraint("ai_score BETWEEN 0 AND 100", name="ck_leads_ai_score"),
        sa.CheckConstraint("total_score BETWEEN 0 AND 100", name="ck_leads_total_score"),
        sa.CheckConstraint(
            "status IN ('active', 'won', 'lost', 'disqualified')", name="ck_leads_status"
        ),
        sa.CheckConstraint("temperature IN ('cold', 'warm', 'hot')", name="ck_leads_temperature"),
        sa.CheckConstraint(
            "estimated_value IS NULL OR estimated_value >= 0", name="ck_leads_value"
        ),
    )
    op.create_index(op.f("ix_leads_client_id"), "leads", ["client_id"])
    op.create_index(op.f("ix_leads_contact_id"), "leads", ["contact_id"])
    op.create_index("ix_leads_client_stage", "leads", ["client_id", "pipeline_stage_id"])
    op.create_index("ix_leads_client_source", "leads", ["client_id", "source_id"])
    op.create_index("ix_leads_client_score", "leads", ["client_id", sa.text("total_score DESC")])
    op.create_index(
        "ix_leads_client_next_followup",
        "leads",
        ["client_id", "next_follow_up_at"],
        postgresql_where=sa.text("status = 'active' AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_leads_assigned",
        "leads",
        ["assigned_user_id", "client_id"],
        postgresql_where=sa.text("status = 'active' AND deleted_at IS NULL"),
    )
    # Deduplicacion: un email (o telefono) por tenant entre los leads no borrados.
    op.create_index(
        "uq_leads_client_email_hash",
        "leads",
        ["client_id", "email_hash"],
        unique=True,
        postgresql_where=sa.text("email_hash IS NOT NULL AND deleted_at IS NULL"),
    )
    op.create_index(
        "uq_leads_client_phone_hash",
        "leads",
        ["client_id", "phone_hash"],
        unique=True,
        postgresql_where=sa.text("phone_hash IS NOT NULL AND deleted_at IS NULL"),
    )

    # ── contacts <-> leads (FK circular: se anade despues de crear `leads`) ───────────
    op.add_column("contacts", sa.Column("lead_id", sa.UUID(), nullable=True))
    op.add_column(
        "contacts", sa.Column("is_lead", sa.Boolean(), server_default="false", nullable=False)
    )
    op.create_foreign_key(
        "fk_contacts_lead_id", "contacts", "leads", ["lead_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index(op.f("ix_contacts_lead_id"), "contacts", ["lead_id"])

    # ── RLS ──────────────────────────────────────────────────────────────────────────
    for tabla in _NUEVAS:
        op.execute(f"ALTER TABLE {tabla} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tabla} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {tabla}
                FOR ALL
                USING ({_POLITICA})
                WITH CHECK ({_POLITICA})
        """)

    # ── captura publica: unico punto sin contexto de tenant ──────────────────────────
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles
                WHERE rolname = current_user AND (rolbypassrls OR rolsuper)
            ) THEN
                RAISE EXCEPTION
                    'El rol % no tiene BYPASSRLS ni es superusuario: capture_lookup_source() '
                    'quedaria sujeta a la RLS de lead_sources y no encontraria nada. '
                    'Ejecuta las migraciones con el rol propietario (DATABASE_URL_DIRECT).',
                    current_user;
            END IF;
        END $$;
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.capture_lookup_source(p_token_hash text)
        RETURNS TABLE (
            source_id uuid, client_id uuid, source_type text, source_name text,
            source_active boolean, client_active boolean, leads_enabled boolean
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public, pg_temp
        AS $$
            SELECT s.id, s.client_id, s.source_type::text, s.name::text,
                   s.is_active, c.is_active, c.lead_management_enabled
            FROM public.lead_sources s
            JOIN public.clients c ON c.id = s.client_id
            WHERE s.capture_token_hash = p_token_hash
              AND NOT c.is_sandbox
        $$
    """)


def downgrade() -> None:
    """Deshace todo en orden inverso."""
    op.execute("DROP FUNCTION IF EXISTS public.capture_lookup_source(text)")
    for tabla in reversed(_NUEVAS):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {tabla}")
        op.execute(f"ALTER TABLE {tabla} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tabla} DISABLE ROW LEVEL SECURITY")
    op.drop_index(op.f("ix_contacts_lead_id"), table_name="contacts")
    op.drop_constraint("fk_contacts_lead_id", "contacts", type_="foreignkey")
    op.drop_column("contacts", "is_lead")
    op.drop_column("contacts", "lead_id")
    op.drop_table("leads")
    op.drop_table("lead_sources")
    op.drop_table("lead_pipeline_stages")
    op.drop_column("clients", "lead_scoring_weights")
    op.drop_column("clients", "icp_config")
