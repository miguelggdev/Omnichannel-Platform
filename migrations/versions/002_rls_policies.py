"""rls_policies — BUG-005: habilitar Row Level Security en las 18 tablas.

`001_baseline` crea el schema completo pero no habilita RLS: una base creada
solo con Alembic queda sin aislamiento entre tenants, aunque el middleware
aplique `SET LOCAL app.current_client_id` (nada lo hace cumplir sin las
politicas). Esta migracion agrega exactamente lo que `supabase/init/init.sql`
ya definia para el schema legacy, adaptado a las 18 tablas reales de
`001_baseline` (ese script no se ejecuta contra Supabase Cloud, ver ADR-020).

`clients` es la tabla raiz: se filtra por `id`, no por `client_id`. Las otras
17 tablas siguen la Regla 1 de CLAUDE.md: `client_id = current_setting(
'app.current_client_id')::uuid`.

Revision ID: 002_rls_policies
Revises: 001_baseline
Create Date: 2026-09-10
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002_rls_policies"
down_revision: Union[str, None] = "001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Las 17 tablas que aislan por client_id. `clients` se maneja aparte (usa id).
CLIENT_SCOPED_TABLES = (
    "agent_configs",
    "contacts",
    "documents",
    "quick_replies",
    "tags",
    "token_budgets",
    "users",
    "webhook_dedup",
    "contact_identifiers",
    "contact_tags",
    "conversations",
    "document_chunks",
    "internal_notes",
    "approved_responses",
    "messages",
    "pending_responses",
    "token_usage_logs",
)


def upgrade() -> None:
    # clients: tabla raiz, se filtra por id (no tiene client_id).
    op.execute("ALTER TABLE clients ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE clients FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON clients")
    op.execute("""
        CREATE POLICY tenant_isolation ON clients
            FOR ALL
            USING (id = current_setting('app.current_client_id')::uuid)
            WITH CHECK (id = current_setting('app.current_client_id')::uuid)
    """)

    for table in CLIENT_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")  # noqa: S608
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")  # noqa: S608
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")  # noqa: S608
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
                FOR ALL
                USING (client_id = current_setting('app.current_client_id')::uuid)
                WITH CHECK (client_id = current_setting('app.current_client_id')::uuid)
        """)  # noqa: S608


def downgrade() -> None:
    for table in reversed(CLIENT_SCOPED_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")  # noqa: S608
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")  # noqa: S608
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")  # noqa: S608

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON clients")
    op.execute("ALTER TABLE clients NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE clients DISABLE ROW LEVEL SECURITY")
