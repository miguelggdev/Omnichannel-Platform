"""`admin_list_clients()` — listado de tenants para el super_admin (Sprint 15, fase 2).

El panel de plataforma tiene que listar **todos** los clientes con unas cifras de uso, y la RLS
de `clients` (`id = current_setting('app.current_client_id')`) solo deja ver el propio. Es la
misma situacion que el login (BUG-025, migracion 007) y los barridos de Celery
(`list_active_client_ids()`, migracion 015) y se resuelve igual: una funcion `SECURITY DEFINER`
que es el unico punto con acceso entre tenants y que solo hace una cosa.

Lo que hace y lo que no
-----------------------
- Devuelve una pagina de tenants (nombre, slug, plan, estado y cuatro cifras de uso). No devuelve
  usuarios, contactos, mensajes ni ningun contenido: solo conteos y la fecha del ultimo mensaje.
- Excluye los sandbox (`clients.is_sandbox`, migracion 022): no son clientes.
- `p_search` se compara con `ILIKE ... ESCAPE '\\'`: el llamador escapa `%`, `_` y `\\`, asi que
  un texto del usuario nunca actua como patron.
- La RLS del resto de tablas queda intacta; el detalle de un cliente y el cambio de estado no
  usan esta funcion, sino `tenant_session(id)` con el id del tenant elegido.

Como `auth_lookup_user()`, solo se salta la RLS si su dueno tiene `BYPASSRLS` (o es superusuario);
la migracion lo comprueba y aborta en vez de dejar una funcion que devuelve cero filas en silencio.

Revision ID: 024_admin_list_clients
Revises: 023_active_clients_skip_sandbox
Create Date: 2026-10-04
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "024_admin_list_clients"
down_revision: str | None = "023_active_clients_skip_sandbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Crea `public.admin_list_clients(text, boolean, integer, integer)`."""
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles
                WHERE rolname = current_user AND (rolbypassrls OR rolsuper)
            ) THEN
                RAISE EXCEPTION
                    'El rol % no tiene BYPASSRLS ni es superusuario: admin_list_clients() '
                    'quedaria sujeta a la RLS de clients y devolveria solo el propio tenant. '
                    'Ejecuta las migraciones con el rol propietario (DATABASE_URL_DIRECT).',
                    current_user;
            END IF;
        END $$;
    """)
    op.execute(r"""
        CREATE OR REPLACE FUNCTION public.admin_list_clients(
            p_search text, p_active boolean, p_limit integer, p_offset integer
        )
        RETURNS TABLE (
            id uuid, name text, slug text, plan text, is_active boolean,
            created_at timestamptz, suspended_at timestamptz,
            users_count bigint, conversations_30d bigint, messages_30d bigint,
            last_message_at timestamptz, total_count bigint
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public, pg_temp
        AS $$
            WITH pagina AS (
                SELECT c.*, count(*) OVER () AS total_count
                FROM public.clients c
                WHERE NOT c.is_sandbox
                  AND (p_active IS NULL OR c.is_active = p_active)
                  AND (
                      p_search IS NULL
                      OR c.name ILIKE '%' || p_search || '%' ESCAPE '\'
                      OR c.slug ILIKE '%' || p_search || '%' ESCAPE '\'
                  )
                ORDER BY c.created_at DESC, c.id
                LIMIT p_limit OFFSET p_offset
            )
            SELECT
                p.id, p.name::text, p.slug::text, p.plan::text, p.is_active,
                p.created_at, p.suspended_at,
                (SELECT count(*) FROM public.users u WHERE u.client_id = p.id),
                (SELECT count(*) FROM public.conversations v
                   WHERE v.client_id = p.id AND v.created_at >= now() - interval '30 days'),
                (SELECT count(*) FROM public.messages m
                   WHERE m.client_id = p.id AND m.created_at >= now() - interval '30 days'),
                (SELECT max(m.created_at) FROM public.messages m WHERE m.client_id = p.id),
                p.total_count
            FROM pagina p
            ORDER BY p.created_at DESC, p.id
        $$;
    """)


def downgrade() -> None:
    """Elimina la funcion."""
    op.execute("DROP FUNCTION IF EXISTS public.admin_list_clients(text, boolean, integer, integer)")
