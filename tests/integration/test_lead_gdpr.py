# ruff: noqa: F811
"""RGPD de leads contra PostgreSQL real con RLS (Sprint 16, ADR-083).

Requiere base de datos: `pytest tests/ --run-db`. Lo que no puede probar un doble: que el export
y la supresion de un contacto alcancen a sus leads (tambien los que tienen soft delete), que la
anonimizacion deje de verdad la base sin el dato (email y telefono a NULL, hashes incluidos, lo
que ademas libera la deduplicacion) y que nada cruce tenants.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.database import tenant_session
from app.models.lead_score import LeadScore
from tests.integration.lead_helpers import crear_lead, fila_cruda, limpiar_leads
from tests.integration.test_crm_api import Escenario, _cliente, dos_tenants, escenario  # noqa: F401

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

CONTACTO = "/api/v1/admin/contacts"
LEAD = "/api/v1/admin/leads"


@pytest_asyncio.fixture
async def crm(escenario: Escenario) -> AsyncGenerator[Escenario, None]:
    """El tenant del CRM, con sus leads limpiados antes de borrar el tenant."""
    yield escenario
    await limpiar_leads(escenario.client_id)


@pytest_asyncio.fixture
async def crm_dos(
    dos_tenants: tuple[Escenario, Escenario],
) -> AsyncGenerator[tuple[Escenario, Escenario], None]:
    yield dos_tenants
    for e in dos_tenants:
        await limpiar_leads(e.client_id)


async def _lead_completo(esc: Escenario, **extra: object) -> uuid.UUID:
    campos: dict[str, object] = {
        "contact_id": esc.contact_id,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "phone": "+57 300 111-2233",
        "linkedin_url": "https://linkedin.com/in/ada",
        "job_title": "CTO",
        "company_name": "Analytical Engines",
        "industry": "Software",
        "disqualified_reason": "No tiene presupuesto",
        "enrichment_data": {"bio": "Matematica", "twitter": "@ada"},
        "fit_score": 70,
    }
    campos.update(extra)
    return await crear_lead(esc.client_id, **campos)


class TestExportDeContacto:
    async def test_incluye_los_leads_vinculados_incluso_los_borrados(self, crm: Escenario) -> None:
        vivo = await _lead_completo(crm)
        borrado = await _lead_completo(
            crm,
            email="otro@example.com",
            phone=None,
            deleted_at=datetime.now(timezone.utc),
        )
        await crear_lead(crm.client_id, email="ajeno@example.com")  # sin contacto
        async with _cliente(crm, "admin") as c:
            r = await c.get(f"{CONTACTO}/{crm.contact_id}/export")
        assert r.status_code == 200, r.text
        leads = {item["id"]: item for item in r.json()["leads"]}
        assert set(leads) == {str(vivo), str(borrado)}
        assert leads[str(vivo)]["email"] == "ada@example.com"
        assert leads[str(vivo)]["phone"] == "+57 300 111-2233"
        assert leads[str(vivo)]["enrichment_data"] == {"bio": "Matematica", "twitter": "@ada"}
        assert leads[str(borrado)]["deleted_at"] is not None

    async def test_un_contacto_sin_leads_exporta_una_lista_vacia(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            r = await c.get(f"{CONTACTO}/{crm.contact_id}/export")
        assert r.json()["leads"] == []

    async def test_no_exporta_leads_de_otro_tenant(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        await _lead_completo(b)
        async with _cliente(a, "admin") as c:
            r = await c.get(f"{CONTACTO}/{a.contact_id}/export")
        assert r.json()["leads"] == []


class TestSupresionDeContacto:
    async def test_anonimiza_los_leads_vinculados_y_deja_lo_demas(self, crm: Escenario) -> None:
        lead = await _lead_completo(crm)
        borrado = await _lead_completo(
            crm, email=None, phone="+57 311 000-0000", deleted_at=datetime.now(timezone.utc)
        )
        ajeno = await crear_lead(crm.client_id, first_name="Grace", email="grace@example.com")
        async with _cliente(crm, "admin") as c:
            r = await c.delete(f"{CONTACTO}/{crm.contact_id}/gdpr-delete")
        assert r.status_code == 200, r.text
        assert r.json()["leads_anonymized"] == 2

        for lid in (lead, borrado):
            f = await fila_cruda(crm.client_id, lid)
            assert (f.first_name, f.last_name) == ("[ELIMINADO]", "[ELIMINADO]")
            assert f.sin_email is True
            assert f.sin_telefono is True
            assert (f.email_hash, f.phone_hash) == (None, None)
            assert (f.linkedin_url, f.job_title) == (None, None)
            assert f.enrichment_data == {}
        f = await fila_cruda(crm.client_id, lead)
        assert f.disqualified_reason == "[MOTIVO ELIMINADO POR SOLICITUD RGPD]"
        # Lo que no es dato personal se conserva para las estadisticas.
        assert (f.company_name,) == ("Analytical Engines",)

        intacto = await fila_cruda(crm.client_id, ajeno)
        assert intacto.first_name == "Grace"
        assert intacto.sin_email is False

    async def test_tras_anonimizar_se_puede_volver_a_capturar_ese_email(
        self, crm: Escenario
    ) -> None:
        await _lead_completo(crm)
        async with _cliente(crm, "admin") as c:
            await c.delete(f"{CONTACTO}/{crm.contact_id}/gdpr-delete")
        await crear_lead(crm.client_id, email="ada@example.com")  # no choca

    async def test_un_contacto_sin_leads_informa_cero(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            r = await c.delete(f"{CONTACTO}/{crm.contact_id}/gdpr-delete")
        assert r.json()["leads_anonymized"] == 0

    async def test_no_toca_leads_de_otro_tenant(self, crm_dos: tuple[Escenario, Escenario]) -> None:
        a, b = crm_dos
        lead_b = await _lead_completo(b)
        async with _cliente(a, "admin") as c:
            await c.delete(f"{CONTACTO}/{a.contact_id}/gdpr-delete")
        f = await fila_cruda(b.client_id, lead_b)
        assert f.first_name == "Ada"
        assert f.sin_email is False


class TestLeadSuelto:
    async def test_exportar_un_lead_incluso_con_soft_delete(self, crm: Escenario) -> None:
        lead = await _lead_completo(crm, deleted_at=datetime.now(timezone.utc))
        async with _cliente(crm, "admin") as c:
            r = await c.get(f"{LEAD}/{lead}/export")
        assert r.status_code == 200
        assert r.json()["lead"]["email"] == "ada@example.com"
        assert r.json()["lead"]["scores"]["fit"] == 70

    async def test_anonimizar_un_lead_sin_contacto_y_no_repetir(self, crm: Escenario) -> None:
        lead = await crear_lead(
            crm.client_id, first_name="Ada", email="ada@example.com", phone="3001112233"
        )
        async with _cliente(crm, "admin") as c:
            r = await c.delete(f"{LEAD}/{lead}/gdpr-delete")
            assert r.status_code == 200, r.text
            f = await fila_cruda(crm.client_id, lead)
            assert f.first_name == "[ELIMINADO]"
            assert (f.sin_email, f.sin_telefono) == (True, True)
            otra = await c.delete(f"{LEAD}/{lead}/gdpr-delete")
        assert otra.status_code == 400

    async def test_un_lead_de_otro_tenant_es_404(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        lead_b = await _lead_completo(b)
        async with _cliente(a, "admin") as c:
            assert (await c.get(f"{LEAD}/{lead_b}/export")).status_code == 404
            assert (await c.delete(f"{LEAD}/{lead_b}/gdpr-delete")).status_code == 404
        assert (await fila_cruda(b.client_id, lead_b)).first_name == "Ada"

    @pytest.mark.parametrize("rol", ["supervisor", "agent", "medical"])
    async def test_solo_admin_y_super_admin(self, crm: Escenario, rol: str) -> None:
        lead = await _lead_completo(crm)
        async with _cliente(crm, rol) as c:
            assert (await c.get(f"{LEAD}/{lead}/export")).status_code == 403
            assert (await c.delete(f"{LEAD}/{lead}/gdpr-delete")).status_code == 403
        assert (await fila_cruda(crm.client_id, lead)).first_name == "Ada"

    async def test_no_queda_el_texto_en_claro_en_ningun_sitio_de_la_fila(
        self, crm: Escenario
    ) -> None:
        lead = await _lead_completo(crm)
        async with _cliente(crm, "admin") as c:
            await c.delete(f"{LEAD}/{lead}/gdpr-delete")
        async with tenant_session(crm.client_id) as s:
            fila = (
                await s.execute(
                    text("SELECT row_to_json(t)::text FROM leads t WHERE id = :i"), {"i": str(lead)}
                )
            ).scalar_one()
        for dato in ("ada@example.com", "300 111", "linkedin.com/in/ada", "CTO", "Matematica"):
            assert dato not in fila, dato


async def _con_score(esc: Escenario, lead_id: uuid.UUID, razonamiento: str) -> None:
    """Le da al lead una fila de historial de scores con texto (como hara el AI score)."""
    async with tenant_session(esc.client_id) as s:
        s.add(
            LeadScore(
                client_id=esc.client_id, lead_id=lead_id, score_type="ai", score=80,
                previous_score=0, trigger="scheduled", factors={"reasoning": razonamiento},
            )
        )  # fmt: skip


async def _scores_en_base(esc: Escenario, lead_id: uuid.UUID) -> int:
    async with tenant_session(esc.client_id) as s:
        return int(
            (
                await s.execute(
                    text("SELECT count(*) FROM lead_scores WHERE lead_id = :i"), {"i": str(lead_id)}
                )
            ).scalar_one()
        )


class TestHistorialDeScores:
    """El historial de scores es perfilado de la persona (Sprint 17): se exporta y se suprime."""

    async def test_el_export_del_contacto_lo_incluye(self, crm: Escenario) -> None:
        lead = await _lead_completo(crm)
        await _con_score(crm, lead, "Ada pregunto por precios dos veces")
        async with _cliente(crm, "admin") as c:
            r = await c.get(f"{CONTACTO}/{crm.contact_id}/export")
        [exportado] = r.json()["leads"]
        assert [s["factors"]["reasoning"] for s in exportado["score_history"]] == [
            "Ada pregunto por precios dos veces"
        ]

    async def test_el_export_de_un_lead_suelto_lo_incluye(self, crm: Escenario) -> None:
        lead = await crear_lead(crm.client_id, email="suelto@example.com")
        await _con_score(crm, lead, "x")
        async with _cliente(crm, "admin") as c:
            r = await c.get(f"{LEAD}/{lead}/export")
        assert len(r.json()["lead"]["score_history"]) == 1
        assert r.json()["lead"]["scores"]["ai"] == 0  # los vigentes siguen donde estaban

    async def test_la_supresion_del_contacto_lo_borra(self, crm: Escenario) -> None:
        lead = await _lead_completo(crm)
        ajeno = await crear_lead(crm.client_id, email="ajeno@example.com")
        await _con_score(crm, lead, "x")
        await _con_score(crm, ajeno, "y")
        async with _cliente(crm, "admin") as c:
            assert (await c.delete(f"{CONTACTO}/{crm.contact_id}/gdpr-delete")).status_code == 200
        assert await _scores_en_base(crm, lead) == 0
        assert await _scores_en_base(crm, ajeno) == 1

    async def test_la_supresion_de_un_lead_suelto_lo_borra(self, crm: Escenario) -> None:
        lead = await crear_lead(crm.client_id, email="suelto@example.com", fit_score=70)
        await _con_score(crm, lead, "x")
        async with _cliente(crm, "admin") as c:
            assert (await c.delete(f"{LEAD}/{lead}/gdpr-delete")).status_code == 200
        assert await _scores_en_base(crm, lead) == 0
