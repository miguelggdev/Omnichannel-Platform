"""`list_active_client_ids()` deja de listar los sandbox — Sprint 14c (revision de codigo).

Las tareas periodicas de Celery Beat (auto-cierre, expiracion de CSAT, purga de
logs de webhooks salientes, despacho de campanas) recorren los tenants con
`public.list_active_client_ids()` (migracion 015). Los sandbox (`clients.is_sandbox`,
migracion 022) se crean con `is_active = true`, asi que entraban en todos esos
barridos: trabajo inutil sobre un tenant de pruebas, y cada sandbox contaba como un
tenant mas en cualquier recorrido cross-tenant.

Se mantiene todo lo demas de la funcion tal como la dejo la 015: `SECURITY DEFINER`,
`search_path` fijo, solo `id`, ordenada. Solo cambia el filtro.

Revision ID: 023_active_clients_skip_sandbox
Revises: 022_sandbox
Create Date: 2026-10-03
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "023_active_clients_skip_sandbox"
down_revision: str | None = "022_sandbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _funcion(filtro: str) -> str:
    """El `CREATE OR REPLACE FUNCTION` de la 015 con el `WHERE` indicado.

    Args:
        filtro: Condicion SQL sobre `c` (la tabla `clients`).

    Returns:
        La sentencia SQL.
    """
    return f"""
        CREATE OR REPLACE FUNCTION public.list_active_client_ids()
        RETURNS SETOF uuid
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public, pg_temp
        AS $$
            SELECT c.id FROM public.clients c WHERE {filtro} ORDER BY c.id
        $$;
    """


def upgrade() -> None:
    """Excluye los tenants sandbox de la lista de tenants activos."""
    op.execute(_funcion("c.is_active = true AND NOT c.is_sandbox"))


def downgrade() -> None:
    """Vuelve a la funcion de la migracion 015 (los sandbox vuelven a listarse)."""
    op.execute(_funcion("c.is_active = true"))
