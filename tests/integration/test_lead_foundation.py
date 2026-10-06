"""Cimientos del modulo de leads contra PostgreSQL real con RLS (Sprint 16, ADR-082).

Requiere base de datos: `pytest tests/ --run-db`. Prueba lo que ningun doble puede: que las tres
tablas aislen por tenant (lectura, escritura y `WITH CHECK`), que email y telefono esten cifrados
en la base pero se lean en claro desde el ORM, que la deduplicacion funcione sobre el indice
ciego (y no bloquee tras un soft delete), que el orden de etapas se pueda reordenar, que las
etapas por defecto se creen una sola vez aun con llamadas concurrentes y que la funcion de
captura publica encuentre la fuente sin contexto de tenant — con el rol de la app, sujeto a RLS.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.database import AsyncSessionLocal, tenant_session
from app.models.contact import Contact
from app.models.lead import Lead
from app.models.lead_pipeline_stage import LeadPipelineStage
from app.models.lead_source import LeadSource
from app.services.lead_pipeline import (
    DEFAULT_STAGES,
    ensure_default_stages,
    generate_capture_token,
    hash_capture_token,
)
from app.services.lead_scoring import compute_total_score

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


class Tenant:
    def __init__(self) -> None:
        self.id = uuid.uuid4()


async def _crear(*, sandbox: bool = False, leads: bool = True, activo: bool = True) -> Tenant:
    t = Tenant()
    async with tenant_session(t.id) as s:
        await s.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active, is_sandbox, "
                "lead_management_enabled) VALUES (:id, 'T', :slug, 'free', :act, :sb, :lm)"
            ),
            {
                "id": str(t.id),
                "slug": f"lead-{t.id.hex[:10]}",
                "act": activo,
                "sb": sandbox,
                "lm": leads,
            },
        )
    return t


async def _limpiar(t: Tenant) -> None:
    async with tenant_session(t.id) as s:
        await s.execute(
            text("UPDATE contacts SET lead_id = NULL WHERE client_id = :c"), {"c": str(t.id)}
        )
        for tabla in ("leads", "lead_sources", "lead_pipeline_stages", "contacts"):
            await s.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :c"),  # noqa: S608
                {"c": str(t.id)},
            )
        await s.execute(text("DELETE FROM clients WHERE id = :c"), {"c": str(t.id)})


@pytest_asyncio.fixture
async def tenant() -> AsyncGenerator[Tenant, None]:
    t = await _crear()
    yield t
    await _limpiar(t)


@pytest_asyncio.fixture
async def dos_tenants() -> AsyncGenerator[tuple[Tenant, Tenant], None]:
    a, b = await _crear(), await _crear()
    yield a, b
    await _limpiar(a)
    await _limpiar(b)


async def _lead(t: Tenant, **campos: object) -> uuid.UUID:
    async with tenant_session(t.id) as s:
        lead = Lead(client_id=t.id, **campos)
        s.add(lead)
        await s.flush()
        return lead.id


# ── RLS ─────────────────────────────────────────────────────────────────────────────────────


class TestAislamiento:
    async def test_cada_tenant_solo_ve_lo_suyo_en_las_tres_tablas(
        self, dos_tenants: tuple[Tenant, Tenant]
    ) -> None:
        a, b = dos_tenants
        async with tenant_session(a.id) as s:
            etapa = LeadPipelineStage(client_id=a.id, name="Nuevo", slug="new", position=0)
            fuente = LeadSource(client_id=a.id, name="Web", source_type="web_form")
            s.add_all([etapa, fuente])
            await s.flush()
            s.add(
                Lead(
                    client_id=a.id,
                    first_name="Ana",
                    email="ana@example.com",
                    pipeline_stage_id=etapa.id,
                    source_id=fuente.id,
                )
            )
        for modelo in (Lead, LeadPipelineStage, LeadSource):
            async with tenant_session(b.id) as s:
                assert (await s.execute(select(modelo))).scalars().all() == []
            async with tenant_session(a.id) as s:
                assert len((await s.execute(select(modelo))).scalars().all()) == 1

    async def test_otro_tenant_no_puede_modificar_ni_borrar(
        self, dos_tenants: tuple[Tenant, Tenant]
    ) -> None:
        a, b = dos_tenants
        await _lead(a, first_name="Ana", email="ana@example.com")
        async with tenant_session(b.id) as s:
            upd = await s.execute(text("UPDATE leads SET first_name = 'X'"))
            dele = await s.execute(text("DELETE FROM leads"))
            assert (upd.rowcount, dele.rowcount) == (0, 0)  # type: ignore[attr-defined]
        async with tenant_session(a.id) as s:
            lead = (await s.execute(select(Lead))).scalar_one()
            assert lead.first_name == "Ana"

    async def test_with_check_impide_escribir_en_otro_tenant(
        self, dos_tenants: tuple[Tenant, Tenant]
    ) -> None:
        a, b = dos_tenants
        for modelo, campos in (
            (Lead, {"first_name": "X"}),
            (LeadPipelineStage, {"name": "N", "slug": "n", "position": 0}),
            (LeadSource, {"name": "W", "source_type": "manual"}),
        ):
            async with tenant_session(b.id) as s:
                s.add(modelo(client_id=a.id, **campos))
                with pytest.raises(DBAPIError):
                    await s.flush()

    async def test_las_tres_tablas_tienen_rls_forzada(self) -> None:
        async with AsyncSessionLocal() as s:
            filas = (
                await s.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname IN ('leads', 'lead_sources', 'lead_pipeline_stages')"
                    )
                )
            ).all()
        assert {f.relname for f in filas} == {"leads", "lead_sources", "lead_pipeline_stages"}
        assert all(f.relrowsecurity and f.relforcerowsecurity for f in filas)


# ── Cifrado y deduplicacion ─────────────────────────────────────────────────────────────────


class TestCifradoYDeduplicacion:
    async def test_email_y_telefono_estan_cifrados_en_la_base_y_en_claro_en_el_orm(
        self, tenant: Tenant
    ) -> None:
        lead_id = await _lead(tenant, email="ana@example.com", phone="+57 300 111-2233")
        async with tenant_session(tenant.id) as s:
            crudo = (
                await s.execute(
                    text("SELECT email::text AS e, phone::text AS p FROM leads WHERE id = :i"),
                    {"i": str(lead_id)},
                )
            ).one()
            lead = (await s.execute(select(Lead).where(Lead.id == lead_id))).scalar_one()
        assert "ana@example.com" not in crudo.e
        assert "3001112233" not in crudo.p.replace("-", "").replace(" ", "")
        assert lead.email == "ana@example.com"
        assert lead.phone == "+57 300 111-2233"

    async def test_el_mismo_email_en_el_mismo_tenant_es_duplicado_aunque_cambie_el_formato(
        self, tenant: Tenant
    ) -> None:
        await _lead(tenant, email="ana@example.com")
        with pytest.raises(IntegrityError):
            await _lead(tenant, email="  ANA@Example.com ")

    async def test_el_mismo_telefono_en_otro_formato_es_duplicado(self, tenant: Tenant) -> None:
        await _lead(tenant, phone="+57 300 111-2233")
        with pytest.raises(IntegrityError):
            await _lead(tenant, phone="573001112233")

    async def test_el_mismo_email_en_otro_tenant_no_choca(
        self, dos_tenants: tuple[Tenant, Tenant]
    ) -> None:
        a, b = dos_tenants
        await _lead(a, email="ana@example.com")
        await _lead(b, email="ana@example.com")

    async def test_tras_un_soft_delete_se_puede_volver_a_capturar(self, tenant: Tenant) -> None:
        primero = await _lead(tenant, email="ana@example.com")
        async with tenant_session(tenant.id) as s:
            lead = (await s.execute(select(Lead).where(Lead.id == primero))).scalar_one()
            lead.deleted_at = datetime.now(timezone.utc)
        await _lead(tenant, email="ana@example.com")  # no lanza
        with pytest.raises(IntegrityError):
            await _lead(tenant, email="ana@example.com")

    async def test_cambiar_el_email_recalcula_el_hash_y_libera_el_anterior(
        self, tenant: Tenant
    ) -> None:
        lead_id = await _lead(tenant, email="ana@example.com")
        async with tenant_session(tenant.id) as s:
            lead = (await s.execute(select(Lead).where(Lead.id == lead_id))).scalar_one()
            hash_viejo = lead.email_hash
            lead.email = "otra@example.com"
        async with tenant_session(tenant.id) as s:
            lead = (await s.execute(select(Lead).where(Lead.id == lead_id))).scalar_one()
            assert lead.email_hash != hash_viejo
        await _lead(tenant, email="ana@example.com")  # el viejo ya esta libre

    async def test_se_puede_buscar_por_el_indice_ciego(self, tenant: Tenant) -> None:
        from app.core.encryption import blind_index

        lead_id = await _lead(tenant, email="ana@example.com", first_name="Ana")
        await _lead(tenant, email="otro@example.com")
        async with tenant_session(tenant.id) as s:
            encontrado = (
                await s.execute(
                    select(Lead.id).where(
                        Lead.email_hash == blind_index("ANA@example.com", tenant.id)
                    )
                )
            ).scalar_one()
        assert encontrado == lead_id

    async def test_varios_leads_sin_email_ni_telefono_no_chocan(self, tenant: Tenant) -> None:
        for n in range(3):
            await _lead(tenant, first_name=f"L{n}", linkedin_url=f"https://linkedin.com/in/{n}")


# ── Restricciones ───────────────────────────────────────────────────────────────────────────


class TestRestricciones:
    @pytest.mark.parametrize(
        "campos",
        [
            {"fit_score": 101},
            {"ai_score": -1},
            {"total_score": 101},
            {"status": "archived"},
            {"temperature": "tibio"},
            {"estimated_value": -5},
        ],
    )
    async def test_los_check_de_leads(self, tenant: Tenant, campos: dict[str, object]) -> None:
        with pytest.raises(IntegrityError):
            await _lead(tenant, email="a@b.co", **campos)

    async def test_un_score_calculado_por_la_app_cumple_el_check(self, tenant: Tenant) -> None:
        total = compute_total_score(100, 100, 100)
        lead_id = await _lead(
            tenant,
            email="a@b.co",
            fit_score=100,
            behavioral_score=100,
            ai_score=100,
            total_score=total,
        )
        async with tenant_session(tenant.id) as s:
            assert (
                await s.execute(select(Lead.total_score).where(Lead.id == lead_id))
            ).scalar_one() == 100

    async def test_valores_por_defecto(self, tenant: Tenant) -> None:
        lead_id = await _lead(tenant, email="a@b.co")
        async with tenant_session(tenant.id) as s:
            lead = (await s.execute(select(Lead).where(Lead.id == lead_id))).scalar_one()
        assert (lead.fit_score, lead.behavioral_score, lead.ai_score, lead.total_score) == (
            0,
            0,
            0,
            0,
        )
        assert (lead.status, lead.temperature, lead.currency) == ("active", "cold", "USD")
        assert lead.enrichment_data == {}
        assert lead.deleted_at is None

    async def test_el_slug_de_una_etapa_es_unico_por_tenant_pero_no_entre_tenants(
        self, dos_tenants: tuple[Tenant, Tenant]
    ) -> None:
        a, b = dos_tenants
        async with tenant_session(b.id) as s:
            s.add(LeadPipelineStage(client_id=b.id, name="N", slug="new", position=0))
        async with tenant_session(a.id) as s:
            s.add(LeadPipelineStage(client_id=a.id, name="N", slug="new", position=0))
        with pytest.raises(IntegrityError):
            async with tenant_session(a.id) as s:
                s.add(LeadPipelineStage(client_id=a.id, name="Otra", slug="new", position=1))

    async def test_el_color_de_una_etapa_se_valida_en_la_base(self, tenant: Tenant) -> None:
        with pytest.raises(IntegrityError):
            async with tenant_session(tenant.id) as s:
                s.add(
                    LeadPipelineStage(
                        client_id=tenant.id, name="N", slug="n", position=0, color="rojo"
                    )
                )

    async def test_dos_etapas_pueden_intercambiar_posicion_en_una_transaccion(
        self, tenant: Tenant
    ) -> None:
        """El UNIQUE de posicion es DEFERRABLE: reordenar pasa por un estado repetido."""
        async with tenant_session(tenant.id) as s:
            s.add_all(
                [
                    LeadPipelineStage(client_id=tenant.id, name="A", slug="a", position=0),
                    LeadPipelineStage(client_id=tenant.id, name="B", slug="b", position=1),
                ]
            )
        async with tenant_session(tenant.id) as s:
            await s.execute(
                text(
                    "UPDATE lead_pipeline_stages SET position = CASE slug "
                    "WHEN 'a' THEN 1 WHEN 'b' THEN 0 END"
                )
            )
        async with tenant_session(tenant.id) as s:
            orden = (
                (
                    await s.execute(
                        select(LeadPipelineStage.slug).order_by(LeadPipelineStage.position)
                    )
                )
                .scalars()
                .all()
            )
        assert orden == ["b", "a"]

    async def test_pero_al_cerrar_la_transaccion_no_puede_quedar_una_posicion_repetida(
        self, tenant: Tenant
    ) -> None:
        async with tenant_session(tenant.id) as s:
            s.add_all(
                [
                    LeadPipelineStage(client_id=tenant.id, name="A", slug="a", position=0),
                    LeadPipelineStage(client_id=tenant.id, name="B", slug="b", position=1),
                ]
            )
        with pytest.raises(IntegrityError):
            async with tenant_session(tenant.id) as s:
                await s.execute(text("UPDATE lead_pipeline_stages SET position = 0"))

    async def test_no_se_puede_borrar_una_etapa_con_leads(self, tenant: Tenant) -> None:
        async with tenant_session(tenant.id) as s:
            etapa = LeadPipelineStage(client_id=tenant.id, name="N", slug="n", position=0)
            s.add(etapa)
            await s.flush()
            etapa_id = etapa.id
        await _lead(tenant, email="a@b.co", pipeline_stage_id=etapa_id)
        with pytest.raises(IntegrityError):
            async with tenant_session(tenant.id) as s:
                await s.execute(text("DELETE FROM lead_pipeline_stages"))

    async def test_borrar_fuente_o_contacto_no_borra_el_lead(self, tenant: Tenant) -> None:
        async with tenant_session(tenant.id) as s:
            fuente = LeadSource(client_id=tenant.id, name="W", source_type="web_form")
            contacto = Contact(client_id=tenant.id, first_name="Ana")
            s.add_all([fuente, contacto])
            await s.flush()
            fuente_id, contacto_id = fuente.id, contacto.id
        lead_id = await _lead(tenant, email="a@b.co", source_id=fuente_id, contact_id=contacto_id)
        async with tenant_session(tenant.id) as s:
            await s.execute(text("DELETE FROM lead_sources WHERE id = :i"), {"i": str(fuente_id)})
            await s.execute(text("DELETE FROM contacts WHERE id = :i"), {"i": str(contacto_id)})
        async with tenant_session(tenant.id) as s:
            lead = (await s.execute(select(Lead).where(Lead.id == lead_id))).scalar_one()
        assert (lead.source_id, lead.contact_id) == (None, None)

    async def test_el_vinculo_contacto_lead_funciona_en_los_dos_sentidos(
        self, tenant: Tenant
    ) -> None:
        async with tenant_session(tenant.id) as s:
            contacto = Contact(client_id=tenant.id, first_name="Ana")
            s.add(contacto)
            await s.flush()
            contacto_id = contacto.id
        lead_id = await _lead(tenant, email="a@b.co", contact_id=contacto_id)
        async with tenant_session(tenant.id) as s:
            contacto = (
                await s.execute(select(Contact).where(Contact.id == contacto_id))
            ).scalar_one()
            assert (contacto.lead_id, contacto.is_lead) == (None, False)
            contacto.lead_id, contacto.is_lead = lead_id, True
        async with tenant_session(tenant.id) as s:
            await s.execute(text("DELETE FROM leads WHERE id = :i"), {"i": str(lead_id)})
        async with tenant_session(tenant.id) as s:
            contacto = (
                await s.execute(select(Contact).where(Contact.id == contacto_id))
            ).scalar_one()
            assert contacto.lead_id is None  # ON DELETE SET NULL


# ── Etapas por defecto ──────────────────────────────────────────────────────────────────────


class TestEtapasPorDefecto:
    async def test_crea_las_once_etapas_en_orden(self, tenant: Tenant) -> None:
        async with tenant_session(tenant.id) as s:
            etapas = await ensure_default_stages(s, tenant.id)
        assert [e.slug for e in etapas] == [d[0] for d in DEFAULT_STAGES]
        assert [e.position for e in etapas] == list(range(len(DEFAULT_STAGES)))
        assert {e.slug for e in etapas if e.is_terminal} == {"won", "lost", "disqualified"}

    async def test_es_idempotente(self, tenant: Tenant) -> None:
        async with tenant_session(tenant.id) as s:
            primeras = await ensure_default_stages(s, tenant.id)
        async with tenant_session(tenant.id) as s:
            segundas = await ensure_default_stages(s, tenant.id)
        assert [e.id for e in primeras] == [e.id for e in segundas]

    async def test_si_el_tenant_ya_configuro_su_pipeline_no_se_toca(self, tenant: Tenant) -> None:
        async with tenant_session(tenant.id) as s:
            s.add(LeadPipelineStage(client_id=tenant.id, name="Mi etapa", slug="mia", position=0))
        async with tenant_session(tenant.id) as s:
            etapas = await ensure_default_stages(s, tenant.id)
        assert [e.slug for e in etapas] == ["mia"]

    async def test_dos_llamadas_a_la_vez_no_duplican_ni_fallan(self, tenant: Tenant) -> None:
        async def una() -> int:
            async with tenant_session(tenant.id) as s:
                return len(await ensure_default_stages(s, tenant.id))

        resultados = await asyncio.gather(una(), una(), una())
        assert resultados == [len(DEFAULT_STAGES)] * 3
        async with tenant_session(tenant.id) as s:
            n = (await s.execute(text("SELECT count(*) FROM lead_pipeline_stages"))).scalar_one()
        assert n == len(DEFAULT_STAGES)

    async def test_no_cruza_tenants(self, dos_tenants: tuple[Tenant, Tenant]) -> None:
        a, b = dos_tenants
        async with tenant_session(a.id) as s:
            await ensure_default_stages(s, a.id)
        async with tenant_session(b.id) as s:
            assert (await s.execute(select(LeadPipelineStage))).scalars().all() == []


# ── Captura publica ─────────────────────────────────────────────────────────────────────────


async def _fuente(t: Tenant, *, activa: bool = True) -> tuple[uuid.UUID, str]:
    token, hash_ = generate_capture_token()
    async with tenant_session(t.id) as s:
        fuente = LeadSource(
            client_id=t.id,
            name="Formulario",
            source_type="web_form",
            capture_token_hash=hash_,
            is_active=activa,
        )
        s.add(fuente)
        await s.flush()
        return fuente.id, token


async def _buscar(token: str) -> list[object]:
    """La busqueda de la captura publica: sin contexto de tenant y con el rol de la app."""
    async with AsyncSessionLocal() as s, s.begin():
        return list(
            (
                await s.execute(
                    text("SELECT * FROM public.capture_lookup_source(:h)"),
                    {"h": hash_capture_token(token)},
                )
            ).all()
        )


class TestCapturaPublica:
    async def test_la_funcion_encuentra_la_fuente_y_su_tenant_sin_contexto(
        self, tenant: Tenant
    ) -> None:
        fuente_id, token = await _fuente(tenant)
        (fila,) = await _buscar(token)
        assert fila.source_id == fuente_id  # type: ignore[attr-defined]
        assert fila.client_id == tenant.id  # type: ignore[attr-defined]
        assert fila.source_type == "web_form"  # type: ignore[attr-defined]
        assert fila.source_active is True  # type: ignore[attr-defined]
        assert fila.client_active is True  # type: ignore[attr-defined]
        assert fila.leads_enabled is True  # type: ignore[attr-defined]

    async def test_sin_contexto_la_tabla_directa_ni_se_puede_leer(self, tenant: Tenant) -> None:
        """La razon de la funcion: sin tenant fijado, la RLS no deja ni consultar `lead_sources`."""
        await _fuente(tenant)
        async with AsyncSessionLocal() as s, s.begin():
            with pytest.raises(DBAPIError):
                await s.execute(text("SELECT count(*) FROM lead_sources"))

    async def test_un_token_desconocido_no_encuentra_nada(self, tenant: Tenant) -> None:
        await _fuente(tenant)
        assert await _buscar("token-que-no-existe") == []
        assert await _buscar("") == []

    async def test_el_token_en_claro_no_esta_guardado(self, tenant: Tenant) -> None:
        _, token = await _fuente(tenant)
        async with tenant_session(tenant.id) as s:
            filas = (
                (await s.execute(text("SELECT row_to_json(t)::text FROM lead_sources t")))
                .scalars()
                .all()
            )
        assert token not in "".join(filas)

    async def test_distingue_fuente_inactiva_tenant_inactivo_y_modulo_apagado(self) -> None:
        creados: list[Tenant] = []
        try:
            inactivo = await _crear(activo=False)
            apagado = await _crear(leads=False)
            creados += [inactivo, apagado]
            _, tok_inactivo = await _fuente(inactivo)
            _, tok_apagado = await _fuente(apagado)
            (fila_i,) = await _buscar(tok_inactivo)
            (fila_a,) = await _buscar(tok_apagado)
            assert fila_i.client_active is False  # type: ignore[attr-defined]
            assert fila_a.leads_enabled is False  # type: ignore[attr-defined]
        finally:
            for t in creados:
                await _limpiar(t)

    async def test_una_fuente_desactivada_se_ve_como_tal(self, tenant: Tenant) -> None:
        _, token = await _fuente(tenant, activa=False)
        (fila,) = await _buscar(token)
        assert fila.source_active is False  # type: ignore[attr-defined]

    async def test_un_tenant_sandbox_no_es_alcanzable_por_captura(self) -> None:
        sandbox = await _crear(sandbox=True)
        try:
            _, token = await _fuente(sandbox)
            assert await _buscar(token) == []
        finally:
            await _limpiar(sandbox)

    async def test_el_hash_del_token_es_unico_en_toda_la_plataforma(
        self, dos_tenants: tuple[Tenant, Tenant]
    ) -> None:
        a, b = dos_tenants
        _, hash_ = generate_capture_token()
        async with tenant_session(a.id) as s:
            s.add(LeadSource(client_id=a.id, name="A", source_type="api", capture_token_hash=hash_))
        with pytest.raises(IntegrityError):
            async with tenant_session(b.id) as s:
                s.add(
                    LeadSource(
                        client_id=b.id, name="B", source_type="api", capture_token_hash=hash_
                    )
                )

    async def test_varias_fuentes_sin_captura_conviven(self, tenant: Tenant) -> None:
        async with tenant_session(tenant.id) as s:
            s.add_all(
                [
                    LeadSource(client_id=tenant.id, name=f"S{i}", source_type="manual")
                    for i in range(3)
                ]
            )


class TestColumnasNuevasDeClients:
    async def test_defaults_de_icp_y_pesos(self, tenant: Tenant) -> None:
        async with tenant_session(tenant.id) as s:
            fila = (
                await s.execute(
                    text("SELECT icp_config, lead_scoring_weights FROM clients WHERE id = :c"),
                    {"c": str(tenant.id)},
                )
            ).one()
        assert fila.icp_config == {}
        assert fila.lead_scoring_weights == {"fit": 40, "behavioral": 30, "ai": 30}
