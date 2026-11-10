"""RLS de las tablas globales de `public` (migracion 034, ADR-093) contra PostgreSQL real.

- Ninguna tabla de `public` queda sin RLS: en Supabase, PostgREST expone el esquema y `anon`
  tenia acceso completo a las que no la tenian (Security Advisor: `rls_disabled_in_public`).
- Cada tabla global lleva la politica `backend_only`, que excluye solo a los roles de la API.
- El rol de la app (sujeto a RLS en CI) sigue leyendo y escribiendo esas tablas: sin la
  politica, RLS lo dejaria fuera y se romperian plantillas, catalogos y checkpointer.
"""

import pytest
from sqlalchemy import text

from app.core.database import AsyncSessionLocal

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

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
ROLES_API = ("anon", "authenticated")


async def _filas(sql: str, **params: object) -> list[tuple]:
    async with AsyncSessionLocal() as s, s.begin():
        return [tuple(f) for f in (await s.execute(text(sql), params)).all()]


async def test_ninguna_tabla_de_public_queda_sin_rls() -> None:
    sin_rls = await _filas(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relrowsecurity "
        "ORDER BY 1"
    )
    assert sin_rls == [], f"Tablas de public sin RLS (expuestas en la API de Supabase): {sin_rls}"


@pytest.mark.parametrize("tabla", TABLAS_GLOBALES)
async def test_la_politica_excluye_solo_a_los_roles_de_la_api(tabla: str) -> None:
    politicas = await _filas(
        "SELECT policyname, cmd, roles::text[], qual, with_check FROM pg_policies "
        "WHERE schemaname = 'public' AND tablename = :t",
        t=tabla,
    )
    assert len(politicas) == 1, politicas
    nombre, cmd, roles, qual, with_check = politicas[0]
    assert (nombre, cmd, roles) == ("backend_only", "ALL", ["public"])
    for expresion in (qual, with_check):
        assert "CURRENT_USER" in expresion.upper()
        assert all(f"'{rol}'" in expresion for rol in ROLES_API), expresion


@pytest.mark.parametrize("rol", ROLES_API)
@pytest.mark.parametrize("tabla", TABLAS_GLOBALES)
async def test_los_roles_de_la_api_no_tienen_privilegios(rol: str, tabla: str) -> None:
    """Solo aplica donde existen los roles de Supabase; en el Postgres del CI no hay."""
    if not (await _filas("SELECT 1 FROM pg_roles WHERE rolname = :r", r=rol)):
        pytest.skip(f"El rol {rol} no existe (no es Supabase)")
    privilegios = await _filas(
        "SELECT p FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) p "
        "WHERE has_table_privilege(:r, CAST(:t AS regclass), p)",
        r=rol,
        t=f"public.{tabla}",
    )
    assert privilegios == []


async def test_el_rol_de_la_app_lee_y_escribe_las_tablas_globales() -> None:
    """Con RLS y sin politica, el INSERT fallaria y la lectura volveria vacia para `app_user`."""
    async with AsyncSessionLocal() as s:
        await s.begin()
        try:
            await s.execute(
                text(
                    "INSERT INTO cie10_catalog (code, description, description_norm) "
                    "VALUES ('ZZ9.T34', 'Prueba 034', 'prueba 034')"
                )
            )
            leidas = (
                (
                    await s.execute(
                        text("SELECT description FROM cie10_catalog WHERE code = 'ZZ9.T34'")
                    )
                )
                .scalars()
                .all()
            )
            assert leidas == ["Prueba 034"]
        finally:
            await s.rollback()
