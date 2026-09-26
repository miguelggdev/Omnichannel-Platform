"""list_active_client_ids — las tareas periodicas solo veian DEFAULT_CLIENT_ID (BUG-045).

El problema
-----------
Las tareas de Celery Beat que recorren todos los tenants (auto-cierre,
expiracion de CSAT, purga de logs de webhooks salientes y, desde el Sprint 12,
el despacho de campanas programadas) sacan la lista de
`app/tasks/auto_close.py::_load_active_client_ids()`, que hace
`SELECT id FROM clients`. La politica de RLS de `clients` filtra por
`app.current_client_id`, que sin contexto de tenant no esta definido: la
consulta falla y la funcion cae a `DEFAULT_CLIENT_ID`.

Mientras todo el trafico entrante del MVP se resolvia a ese tenant no se
notaba, pero el CRUD de campanas es por tenant (JWT): una campana programada de
cualquier otro tenant no salia nunca, y sus conversaciones no se auto-cerraban.

La solucion
-----------
Mismo patron que `auth_lookup_user()` (migracion 007, ADR-046): una funcion
`SECURITY DEFINER` con una sola responsabilidad y nada mas. Devuelve **solo los
UUID** de los tenants activos: ni nombres, ni planes, ni ninguna otra columna,
ni nada de otras tablas. El resto de la RLS queda intacta y cada tarea sigue
trabajando tenant por tenant con `tenant_session()`.

Exponer los UUID no abre nada: la RLS la aplica la aplicacion al fijar
`app.current_client_id`, no el secreto de esos identificadores.

Igual que en la 007, la funcion solo esquiva la RLS si su dueno tiene
`BYPASSRLS` o es superusuario (`FORCE ROW LEVEL SECURITY` aplica tambien al
dueno de la tabla). Si no, devolveria cero filas en silencio y las tareas no
barrerian a nadie, asi que la migracion **aborta el despliegue**.

Revision ID: 015_list_active_tenants_function
Revises: 014_invoice_amounts_bigint
Create Date: 2026-09-26
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "015_list_active_tenants_function"
down_revision: str | None = "014_invoice_amounts_bigint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ─── Precondicion: el dueno de la funcion tiene que poder saltarse la RLS ──
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles
                WHERE rolname = current_user
                  AND (rolbypassrls OR rolsuper)
            ) THEN
                RAISE EXCEPTION
                    'El rol % no tiene BYPASSRLS ni es superusuario. '
                    'list_active_client_ids() quedaria sujeta a la politica de RLS de '
                    'clients y las tareas periodicas no barrerian a ningun tenant, sin '
                    'ningun error visible. Ejecuta las migraciones con el rol propietario '
                    'del schema (DATABASE_URL_DIRECT), no con el rol de la aplicacion.',
                    current_user;
            END IF;
        END $$;
    """)

    # - STABLE: solo lee.
    # - SET search_path fijo y nombres calificados: sin el, quien llama podria
    #   anteponer un schema con una tabla `clients` falsa (ver la 007).
    # - Solo `id`, solo `is_active = true`: nada mas sale de la funcion.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.list_active_client_ids()
        RETURNS SETOF uuid
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public, pg_temp
        AS $$
            SELECT c.id FROM public.clients c WHERE c.is_active = true ORDER BY c.id
        $$;
    """)

    op.execute("""
        COMMENT ON FUNCTION public.list_active_client_ids() IS
        'BUG-045: lista de tenants activos para las tareas periodicas cross-tenant '
        '(auto_close._load_active_client_ids). Solo devuelve UUID; cada tarea sigue '
        'operando tenant por tenant bajo RLS.';
    """)
    # EXECUTE queda en el default (PUBLIC), por la misma razon que en la 007: el
    # rol de la aplicacion se crea despues de correr las migraciones.


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.list_active_client_ids()")
