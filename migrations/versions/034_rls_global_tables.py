"""RLS en las tablas globales de `public` (ADR-093).

Las tablas sin `client_id` se crearon sin RLS: `tenant_templates` y `template_instantiations`
(plantillas de la plataforma), `cie10_catalog` y `cups_catalog` (catalogos clinicos), las cuatro
del checkpointer de LangGraph (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`,
`checkpoint_migrations`, con el estado de las conversaciones del agente) y `alembic_version`.

En Supabase Cloud, PostgREST expone `public` y los roles `anon` y `authenticated` reciben por
defecto todos los privilegios sobre las tablas nuevas: con la URL del proyecto y la clave publica
cualquiera podia leer, modificar y borrar esas tablas (Security Advisor: `rls_disabled_in_public`).

Por que una politica y no solo RLS sin politicas: la app conecta, en CI y en produccion, con un
rol sujeto a RLS (`app_user`, ver ADR-088). Con RLS y ninguna politica, ese rol tampoco veria las
filas y se romperian las plantillas, los catalogos y el checkpointer. La politica
`backend_only` deja pasar a cualquier rol **salvo** los de la API de Supabase, sin depender del
nombre del rol de la app ni de que exista al migrar. Ademas se revocan los privilegios de `anon`
y `authenticated` sobre estas tablas (si esos roles existen): doble barrera.

Sin `FORCE`: el dueno de las tablas (el rol de las migraciones) sigue sin politicas que lo
filtren, igual que hasta ahora.

Revision ID: 034_rls_global_tables
Revises: 033_call_calendar_event
Create Date: 2026-11-10
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "034_rls_global_tables"
down_revision: str | None = "033_call_calendar_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tablas de `public` sin `client_id` que quedaban sin RLS.
TABLAS_GLOBALES = (
    "alembic_version",
    "checkpoint_blobs",
    "checkpoint_migrations",
    "checkpoint_writes",
    "checkpoints",
    "cie10_catalog",
    "cups_catalog",
    "template_instantiations",
    "tenant_templates",
)
#: Roles con los que PostgREST (la API REST de Supabase) ejecuta las peticiones.
ROLES_API = ("anon", "authenticated")
POLITICA = "backend_only"
_FUERA_DE_LA_API = "current_user NOT IN ('anon', 'authenticated')"


def upgrade() -> None:
    """Habilita RLS con la politica `backend_only` y quita los privilegios de la API REST."""
    # Solo se interpolan constantes de este modulo (nombres de tablas, roles y politica).
    for tabla in TABLAS_GLOBALES:
        op.execute(f"ALTER TABLE public.{tabla} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS {POLITICA} ON public.{tabla}")
        op.execute(f"""
            CREATE POLICY {POLITICA} ON public.{tabla}
                FOR ALL
                USING ({_FUERA_DE_LA_API})
                WITH CHECK ({_FUERA_DE_LA_API})
        """)
        for rol in ROLES_API:
            op.execute(f"""
                DO $$ BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{rol}') THEN
                        EXECUTE 'REVOKE ALL ON TABLE public.{tabla} FROM {rol}';
                    END IF;
                END $$
            """)  # noqa: S608


def downgrade() -> None:
    """Quita la politica y deshabilita RLS.

    No devuelve los privilegios a `anon`/`authenticated`: no los dio ninguna migracion (son los
    privilegios por defecto de Supabase) y devolverlos reabriria la exposicion.
    """
    for tabla in TABLAS_GLOBALES:
        op.execute(f"DROP POLICY IF EXISTS {POLITICA} ON public.{tabla}")
        op.execute(f"ALTER TABLE public.{tabla} DISABLE ROW LEVEL SECURITY")
