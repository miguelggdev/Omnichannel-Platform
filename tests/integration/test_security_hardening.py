"""Endurecimiento de la migracion 030 contra PostgreSQL real (ADR-088).

- Las funciones `SECURITY DEFINER` no las puede ejecutar `PUBLIC` (ni, en Supabase, `anon`).
- `audit_trigger_function()` tiene el `search_path` fijado.
- El presupuesto de tokens del mes se crea bajo demanda heredando el anterior: sin fila, el
  consumo era ilimitado desde el segundo mes del tenant.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.agents.nodes.token_budget import asegurar_presupuesto_del_mes
from app.core.config import get_settings
from app.core.database import tenant_session
from app.middleware.token_budget import TokenBudgetGuard

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

FUNCIONES = (
    "public.auth_lookup_user(text)",
    "public.list_active_client_ids()",
    "public.admin_list_clients(text, boolean, integer, integer)",
    "public.capture_lookup_source(text)",
)


async def _escalar(client_id: uuid.UUID, sql: str, **params: object) -> object:
    async with tenant_session(client_id) as s:
        return (await s.execute(text(sql), params)).scalar()


@pytest_asyncio.fixture
async def tenant() -> AsyncGenerator[uuid.UUID, None]:
    client_id = uuid.uuid4()
    async with tenant_session(client_id) as s:
        await s.execute(
            text("INSERT INTO clients (id, name, slug, plan) VALUES (:id, 'T', :slug, 'free')"),
            {"id": str(client_id), "slug": f"sec-{client_id.hex[:10]}"},
        )
    yield client_id
    async with tenant_session(client_id) as s:
        for tabla in ("token_usage_logs", "token_budgets"):
            await s.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :c"),  # noqa: S608
                {"c": str(client_id)},
            )
        await s.execute(text("DELETE FROM clients WHERE id = :c"), {"c": str(client_id)})


class TestFuncionesSecurityDefiner:
    @pytest.mark.parametrize("funcion", FUNCIONES)
    async def test_public_no_puede_ejecutarlas(self, tenant: uuid.UUID, funcion: str) -> None:
        """Con `EXECUTE` para `PUBLIC`, la API REST de Supabase leia hashes con la clave anon."""
        concedidos = await _escalar(
            tenant,
            "SELECT count(*) FROM pg_proc p, "
            "aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
            "WHERE p.oid = CAST(:f AS regprocedure) AND a.grantee = 0 "
            "AND a.privilege_type = 'EXECUTE'",
            f=funcion,
        )
        assert concedidos == 0

    @pytest.mark.parametrize("funcion", FUNCIONES)
    async def test_la_app_si_puede_ejecutarlas(self, tenant: uuid.UUID, funcion: str) -> None:
        assert await _escalar(
            tenant, "SELECT has_function_privilege(current_user, :f, 'EXECUTE')", f=funcion
        )

    async def test_el_trigger_de_auditoria_tiene_search_path_fijo(self, tenant: uuid.UUID) -> None:
        config = await _escalar(
            tenant,
            "SELECT proconfig FROM pg_proc "
            "WHERE oid = CAST('public.audit_trigger_function()' AS regprocedure)",
        )
        assert config == ["search_path=pg_catalog, public, pg_temp"]


class TestPresupuestoDelMes:
    async def _fila(self, client_id: uuid.UUID, mes: str) -> tuple[object, ...] | None:
        async with tenant_session(client_id) as s:
            fila = (
                await s.execute(
                    text(
                        "SELECT total_budget, used_tokens, model_default FROM token_budgets "
                        "WHERE client_id = :c AND month = :m"
                    ),
                    {"c": str(client_id), "m": mes},
                )
            ).first()
        return tuple(fila) if fila else None

    async def test_el_mes_nuevo_hereda_el_presupuesto_del_ultimo(self, tenant: uuid.UUID) -> None:
        async with tenant_session(tenant) as s:
            for mes, total in (("2026-07", 50_000), ("2026-08", 123_456)):
                await s.execute(
                    text(
                        "INSERT INTO token_budgets (client_id, month, total_budget, used_tokens, "
                        "model_default) VALUES (:c, :m, :t, 99, 'gpt-4o-mini')"
                    ),
                    {"c": str(tenant), "m": mes, "t": total},
                )
        async with tenant_session(tenant) as s:
            await asegurar_presupuesto_del_mes(s, tenant, "2026-10")

        assert await self._fila(tenant, "2026-10") == (123_456, 0, "gpt-4o-mini")

    async def test_sin_historia_usa_el_presupuesto_por_defecto(self, tenant: uuid.UUID) -> None:
        async with tenant_session(tenant) as s:
            await asegurar_presupuesto_del_mes(s, tenant, "2026-10")

        defecto = get_settings().TOKEN_BUDGET_DEFAULT_MONTHLY
        assert await self._fila(tenant, "2026-10") == (defecto, 0, "gpt-4o")

    async def test_no_pisa_la_fila_existente_ni_duplica_en_carrera(self, tenant: uuid.UUID) -> None:
        async def asegurar() -> None:
            async with tenant_session(tenant) as s:
                await asegurar_presupuesto_del_mes(s, tenant, "2026-10")

        await asyncio.gather(*(asegurar() for _ in range(5)))
        async with tenant_session(tenant) as s:
            await s.execute(
                text("UPDATE token_budgets SET used_tokens = 77 WHERE client_id = :c"),
                {"c": str(tenant)},
            )
        await asegurar()

        filas = await _escalar(
            tenant, "SELECT count(*) FROM token_budgets WHERE client_id = :c", c=str(tenant)
        )
        assert filas == 1
        assert (await self._fila(tenant, "2026-10") or ())[1] == 77

    async def test_el_consumo_se_suma_aunque_el_mes_no_tuviera_fila(
        self, tenant: uuid.UUID
    ) -> None:
        """Antes el UPDATE no encontraba fila y el consumo se perdia: presupuesto infinito."""
        await TokenBudgetGuard.record_usage(
            client_id=tenant,
            conversation_id=None,
            model="gpt-4o-mini",
            prompt_tokens=100,
            completion_tokens=20,
            operation="rag_query",
        )

        mes = datetime.now(timezone.utc).strftime("%Y-%m")
        fila = await self._fila(tenant, mes)
        assert fila is not None
        assert fila[1] == 120


async def _tenant_nuevo() -> uuid.UUID:
    client_id = uuid.uuid4()
    async with tenant_session(client_id) as s:
        await s.execute(
            text("INSERT INTO clients (id, name, slug, plan) VALUES (:id, 'T', :slug, 'free')"),
            {"id": str(client_id), "slug": f"sec-{client_id.hex[:10]}"},
        )
    return client_id


async def _borrar(client_id: uuid.UUID) -> None:
    async with tenant_session(client_id) as s:
        for tabla in ("patient_consents", "contacts"):
            await s.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :c"),  # noqa: S608
                {"c": str(client_id)},
            )
        await s.execute(text("DELETE FROM clients WHERE id = :c"), {"c": str(client_id)})


async def _contacto(client_id: uuid.UUID, nombre: str = "Paciente") -> uuid.UUID:
    contacto = uuid.uuid4()
    async with tenant_session(client_id) as s:
        await s.execute(
            text("INSERT INTO contacts (id, client_id, display_name) VALUES (:id, :c, :n)"),
            {"id": str(contacto), "c": str(client_id), "n": nombre},
        )
    return contacto


class TestReferenciasEntreTenants:
    async def test_un_consentimiento_no_enlaza_un_contacto_de_otro_tenant(
        self, tenant: uuid.UUID
    ) -> None:
        """La FK se comprueba sin RLS: sin la validacion, el id ajeno se guardaba."""
        from app.core.habeas_data import HabeasDataCompliance, HabeasDataError

        otro = await _tenant_nuevo()
        try:
            ajeno = await _contacto(otro)
            with pytest.raises(HabeasDataError, match="contacto"):
                async with tenant_session(tenant) as s:
                    await HabeasDataCompliance.register_consent(
                        s, tenant, "CC", "1020304050", "verbal", contact_id=ajeno
                    )
            propio = await _contacto(tenant)
            async with tenant_session(tenant) as s:
                r = await HabeasDataCompliance.register_consent(
                    s, tenant, "CC", "1020304050", "verbal", contact_id=propio
                )
            assert r["registered"] is True
        finally:
            await _borrar(otro)
            async with tenant_session(tenant) as s:
                for tabla in ("patient_consents", "contacts"):
                    await s.execute(
                        text(f"DELETE FROM {tabla} WHERE client_id = :c"),  # noqa: S608
                        {"c": str(tenant)},
                    )

    async def test_la_busqueda_trata_el_porcentaje_como_texto(self, tenant: uuid.UUID) -> None:
        from sqlalchemy import select

        from app.core.like import ESCAPE, patron_contiene
        from app.models.contact import Contact

        await _contacto(tenant, "Descuento 100% real")
        await _contacto(tenant, "Ana")
        try:
            async with tenant_session(tenant) as s:
                nombres = (
                    (
                        await s.execute(
                            select(Contact.display_name).where(
                                Contact.client_id == tenant,
                                Contact.display_name.ilike(patron_contiene("%"), escape=ESCAPE),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
        finally:
            async with tenant_session(tenant) as s:
                await s.execute(
                    text("DELETE FROM contacts WHERE client_id = :c"), {"c": str(tenant)}
                )
        assert nombres == ["Descuento 100% real"]


class TestRolDeLaApp:
    async def test_el_rol_de_la_app_no_salta_la_rls_y_no_hay_aviso(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        from app.core.database import init_db

        with caplog.at_level(logging.CRITICAL, logger="app.core.database"):
            await init_db()
        assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL]
