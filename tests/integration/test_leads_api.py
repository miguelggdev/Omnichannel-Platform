# ruff: noqa: F811
"""API de leads contra PostgreSQL real con RLS (Sprint 16, ADR-083).

Requiere base de datos: `pytest tests/ --run-db`. Lo que un doble no prueba: duplicados sobre
el indice ciego (incluida la carrera de dos peticiones), paginacion estable con empates, Kanban
con consultas fijas, el efecto de cada etapa terminal sobre el estado, el enlace con contactos
y que ningun tenant vea ni toque los leads de otro.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, text

from app.core.database import engine, tenant_session
from app.models.contact import Contact
from app.models.contact_identifier import ContactIdentifier
from tests.integration.lead_helpers import (
    activar_modulo,
    crear_etapa,
    crear_fuente,
    crear_lead,
    limpiar_leads,
)
from tests.integration.test_crm_api import Escenario, _cliente, dos_tenants, escenario  # noqa: F401

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

LEADS = "/api/v1/leads"


@pytest_asyncio.fixture
async def crm(escenario: Escenario) -> AsyncGenerator[Escenario, None]:
    await activar_modulo(escenario.client_id)
    yield escenario
    await limpiar_leads(escenario.client_id)


@pytest_asyncio.fixture
async def crm_dos(
    dos_tenants: tuple[Escenario, Escenario],
) -> AsyncGenerator[tuple[Escenario, Escenario], None]:
    for e in dos_tenants:
        await activar_modulo(e.client_id)
    yield dos_tenants
    for e in dos_tenants:
        await limpiar_leads(e.client_id)


async def _pipeline(esc: Escenario) -> dict[str, uuid.UUID]:
    """Etapas minimas: new, qualified y las tres terminales."""
    datos = [
        ("new", False),
        ("qualified", False),
        ("won", True),
        ("lost", True),
        ("disqualified", True),
    ]
    return {
        slug: await crear_etapa(esc.client_id, slug, i, is_terminal=term)
        for i, (slug, term) in enumerate(datos)
    }


async def _crear_contacto(
    esc: Escenario, email: str | None = None, whatsapp: str | None = None, **campos: Any
) -> uuid.UUID:
    async with tenant_session(esc.client_id) as s:
        contacto = Contact(client_id=esc.client_id, first_name="C", **campos)
        s.add(contacto)
        await s.flush()
        if email:
            s.add(
                ContactIdentifier(
                    client_id=esc.client_id,
                    contact_id=contacto.id,
                    channel="email",
                    identifier_value=email,
                )
            )
        if whatsapp:
            s.add(
                ContactIdentifier(
                    client_id=esc.client_id,
                    contact_id=contacto.id,
                    channel="whatsapp",
                    identifier_value=whatsapp,
                )
            )
        return contacto.id


async def _fila(esc: Escenario, sql: str, **params: Any) -> Any:
    async with tenant_session(esc.client_id) as s:
        return (await s.execute(text(sql), params)).one()


# ── Alta ────────────────────────────────────────────────────────────────────────────────────


class TestAlta:
    async def test_crea_en_la_etapa_new_con_valores_iniciales(self, crm: Escenario) -> None:
        etapas = await _pipeline(crm)
        async with _cliente(crm, "agent") as c:
            r = await c.post(
                LEADS,
                json={"first_name": "Ana", "email": "ana@example.com", "company_name": "ACME"},
            )
        assert r.status_code == 201, r.text
        lead = r.json()
        assert lead["pipeline_stage_id"] == str(etapas["new"])
        assert (lead["status"], lead["temperature"], lead["total_score"]) == ("active", "cold", 0)
        assert lead["email"] == "ana@example.com"
        assert lead["last_activity_at"] is not None

    async def test_sin_etapa_new_usa_la_primera_y_sin_pipeline_es_409(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            sin = await c.post(LEADS, json={"email": "a@example.com"})
            assert sin.status_code == 409
        primera = await crear_etapa(crm.client_id, "entrada", 0)
        async with _cliente(crm, "admin") as c:
            r = await c.post(LEADS, json={"email": "a@example.com"})
        assert r.json()["pipeline_stage_id"] == str(primera)

    async def test_una_etapa_explicita_y_las_referencias_se_validan_en_el_tenant(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        etapas_a = await _pipeline(a)
        etapa_b = await crear_etapa(b.client_id, "ajena", 0)
        fuente_b = await crear_fuente(b.client_id)
        async with _cliente(a, "admin") as c:
            ok = await c.post(
                LEADS,
                json={"email": "a@example.com", "pipeline_stage_id": str(etapas_a["qualified"])},
            )
            assert ok.json()["pipeline_stage_id"] == str(etapas_a["qualified"])
            for extra in (
                {"pipeline_stage_id": str(etapa_b)},
                {"source_id": str(fuente_b)},
                {"assigned_user_id": str(b.user_id)},
                {"assigned_user_id": str(uuid.uuid4())},
            ):
                r = await c.post(
                    LEADS, json={"email": f"x{uuid.uuid4().hex[:6]}@example.com", **extra}
                )
                assert r.status_code == 400, (extra, r.text)

    async def test_no_se_puede_asignar_a_un_usuario_desactivado_ni_medico(
        self, crm: Escenario
    ) -> None:
        await _pipeline(crm)
        async with tenant_session(crm.client_id) as s:
            await s.execute(
                text("UPDATE users SET is_active = false WHERE id = :u"), {"u": str(crm.user_id)}
            )
        async with _cliente(crm, "admin") as c:
            r = await c.post(
                LEADS, json={"email": "a@example.com", "assigned_user_id": str(crm.user_id)}
            )
        assert r.status_code == 400
        async with tenant_session(crm.client_id) as s:
            await s.execute(
                text("UPDATE users SET is_active = true, role = 'medical' WHERE id = :u"),
                {"u": str(crm.user_id)},
            )
        async with _cliente(crm, "admin") as c:
            r = await c.post(
                LEADS, json={"email": "b@example.com", "assigned_user_id": str(crm.user_id)}
            )
        assert r.status_code == 400

    async def test_el_email_y_el_telefono_repetidos_son_409_con_el_lead_existente(
        self, crm: Escenario
    ) -> None:
        await _pipeline(crm)
        async with _cliente(crm, "admin") as c:
            primero = (
                await c.post(LEADS, json={"email": "ana@example.com", "phone": "+57 300 111-2233"})
            ).json()["id"]
            por_email = await c.post(LEADS, json={"email": "  ANA@Example.com "})
            por_telefono = await c.post(LEADS, json={"phone": "573001112233"})
        for r, campo in ((por_email, "email"), (por_telefono, "telefono")):
            assert r.status_code == 409, r.text
            assert r.json()["error_code"] == "DUPLICATE"
            assert r.json()["details"] == {"existing_lead_id": primero, "field": campo}

    async def test_mismo_email_en_otro_tenant_o_tras_borrar_no_choca(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        await _pipeline(a)
        await _pipeline(b)
        async with _cliente(a, "admin") as ca, _cliente(b, "admin") as cb:
            lead = (await ca.post(LEADS, json={"email": "ana@example.com"})).json()["id"]
            assert (await cb.post(LEADS, json={"email": "ana@example.com"})).status_code == 201
            assert (await ca.delete(f"{LEADS}/{lead}")).status_code == 204
            assert (await ca.post(LEADS, json={"email": "ana@example.com"})).status_code == 201

    async def test_dos_altas_simultaneas_del_mismo_email_dejan_un_solo_lead(
        self, crm: Escenario
    ) -> None:
        await _pipeline(crm)

        async def alta() -> int:
            async with _cliente(crm, "admin") as c:
                return (await c.post(LEADS, json={"email": "carrera@example.com"})).status_code

        codigos = sorted(await asyncio.gather(alta(), alta(), alta()))
        assert codigos == [201, 409, 409]
        fila = await _fila(crm, "SELECT count(*) AS n FROM leads WHERE deleted_at IS NULL")
        assert fila.n == 1

    async def test_si_la_precomprobacion_no_ve_el_duplicado_el_indice_unico_lo_detiene_con_409(
        self, crm: Escenario, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La carrera real: las dos peticiones pasan la consulta previa antes de que ninguna escriba."""
        await _pipeline(crm)

        async def ciega(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr("app.api.v1.leads._comprobar_duplicado", ciega)
        async with _cliente(crm, "admin") as c:
            primero = await c.post(LEADS, json={"email": "carrera@example.com"})
            segundo = await c.post(LEADS, json={"email": "CARRERA@example.com"})
            por_telefono = await c.post(LEADS, json={"phone": "+57 300 999-0000"})
            repetido = await c.post(LEADS, json={"phone": "573009990000"})
        assert primero.status_code == 201
        assert segundo.status_code == 409
        assert segundo.json()["details"]["field"] == "email"
        assert por_telefono.status_code == 201
        assert repetido.status_code == 409
        assert repetido.json()["details"]["field"] == "telefono"
        fila = await _fila(crm, "SELECT count(*) AS n FROM leads")
        assert fila.n == 2

    async def test_exige_contacto_y_ignora_scores_y_tenant_enviados(self, crm: Escenario) -> None:
        await _pipeline(crm)
        async with _cliente(crm, "admin") as c:
            assert (await c.post(LEADS, json={"first_name": "Solo nombre"})).status_code == 422
            r = await c.post(
                LEADS,
                json={
                    "email": "a@example.com",
                    "total_score": 100,
                    "fit_score": 100,
                    "client_id": str(uuid.uuid4()),
                    "status": "won",
                },
            )
        assert r.status_code == 201
        assert (r.json()["total_score"], r.json()["fit_score"], r.json()["status"]) == (
            0,
            0,
            "active",
        )
        assert r.json()["client_id"] == str(crm.client_id)

    async def test_email_y_telefono_quedan_cifrados_en_la_base(self, crm: Escenario) -> None:
        await _pipeline(crm)
        async with _cliente(crm, "admin") as c:
            lead = (
                await c.post(LEADS, json={"email": "ana@example.com", "phone": "3001112233"})
            ).json()["id"]
        f = await _fila(
            crm, "SELECT email::text AS e, phone::text AS p FROM leads WHERE id = :i", i=lead
        )
        assert "ana@example.com" not in f.e
        assert "3001112233" not in f.p


# ── Enlace automatico con contactos al crear ─────────────────────────────────────────────────


class TestEnlaceAutomatico:
    async def test_un_contacto_inequivoco_se_enlaza_por_los_dos_lados(self, crm: Escenario) -> None:
        await _pipeline(crm)
        contacto = await _crear_contacto(crm, email="ana@example.com")
        async with _cliente(crm, "admin") as c:
            lead = (await c.post(LEADS, json={"email": "ANA@example.com"})).json()
        assert lead["contact_id"] == str(contacto)
        f = await _fila(crm, "SELECT lead_id, is_lead FROM contacts WHERE id = :i", i=contacto)
        assert (str(f.lead_id), f.is_lead) == (lead["id"], True)

    async def test_el_telefono_casa_con_o_sin_mas_con_un_contacto_de_whatsapp(
        self, crm: Escenario
    ) -> None:
        await _pipeline(crm)
        contacto = await _crear_contacto(crm, whatsapp="+573001112233")
        async with _cliente(crm, "admin") as c:
            lead = (await c.post(LEADS, json={"phone": "57 300 111 2233"})).json()
        assert lead["contact_id"] == str(contacto)

    async def test_si_hay_dos_candidatos_no_se_enlaza(self, crm: Escenario) -> None:
        await _pipeline(crm)
        await _crear_contacto(crm, email="ana@example.com")
        await _crear_contacto(crm, whatsapp="+573001112233")
        async with _cliente(crm, "admin") as c:
            lead = (
                await c.post(LEADS, json={"email": "ana@example.com", "phone": "+573001112233"})
            ).json()
        assert lead["contact_id"] is None

    async def test_un_contacto_ya_enlazado_a_otro_lead_no_se_reasigna(self, crm: Escenario) -> None:
        await _pipeline(crm)
        contacto = await _crear_contacto(crm, email="ana@example.com", whatsapp="+573001112233")
        async with _cliente(crm, "admin") as c:
            primero = (await c.post(LEADS, json={"email": "ana@example.com"})).json()
            segundo = (await c.post(LEADS, json={"phone": "+573001112233"})).json()
        assert primero["contact_id"] == str(contacto)
        assert segundo["contact_id"] is None

    async def test_no_enlaza_con_contactos_fusionados_ni_anonimizados_ni_ajenos(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        await _pipeline(a)
        destino = await _crear_contacto(a)
        await _crear_contacto(a, email="fusionado@example.com", merged_into_id=destino)
        await _crear_contacto(a, email="gdpr@example.com", is_gdpr_deleted=True)
        await _crear_contacto(b, email="ajeno@example.com")
        async with _cliente(a, "admin") as c:
            for correo in ("fusionado@example.com", "gdpr@example.com", "ajeno@example.com"):
                lead = (await c.post(LEADS, json={"email": correo})).json()
                assert lead["contact_id"] is None, correo


# ── Listado ─────────────────────────────────────────────────────────────────────────────────


async def _poblar(crm: Escenario) -> dict[str, Any]:
    etapas = await _pipeline(crm)
    fuente = await crear_fuente(crm.client_id, name="Web", source_type="web_form")
    ahora = datetime.now(timezone.utc)
    ids = {
        "ana": await crear_lead(
            crm.client_id,
            first_name="Ana",
            last_name="Gomez",
            company_name="ACME 100%",
            email="ana@example.com",
            phone="+57 300 111-2233",
            pipeline_stage_id=etapas["new"],
            source_id=fuente,
            assigned_user_id=crm.user_id,
            temperature="hot",
            total_score=90,
            estimated_value=Decimal("5000"),
            next_follow_up_at=ahora + timedelta(days=1),
        ),
        "beto": await crear_lead(
            crm.client_id,
            first_name="Beto",
            company_name="Beta SA",
            job_title="CTO",
            email="beto@example.com",
            pipeline_stage_id=etapas["qualified"],
            temperature="warm",
            total_score=50,
            next_follow_up_at=ahora + timedelta(days=10),
        ),
        "cata": await crear_lead(
            crm.client_id,
            first_name="Cata",
            company_name="Gamma",
            email="cata@example.com",
            pipeline_stage_id=etapas["won"],
            status="won",
            total_score=10,
            estimated_value=Decimal("100"),
        ),
        "borrado": await crear_lead(
            crm.client_id,
            first_name="Borrado",
            email="borrado@example.com",
            total_score=99,
            deleted_at=ahora,
        ),
    }
    return {"ids": ids, "etapas": etapas, "fuente": fuente}


def _nombres(r: Any) -> list[str]:
    return [item["first_name"] for item in r.json()["items"]]


class TestListado:
    async def test_pagina_y_total_sin_los_borrados(self, crm: Escenario) -> None:
        await _poblar(crm)
        async with _cliente(crm, "agent") as c:
            r = await c.get(
                LEADS, params={"page_size": 2, "sort_by": "total_score", "order": "desc"}
            )
            assert r.status_code == 200, r.text
            cuerpo = r.json()
            assert (cuerpo["total"], cuerpo["total_pages"], cuerpo["page"]) == (3, 2, 1)
            assert _nombres(r) == ["Ana", "Beto"]
            r2 = await c.get(LEADS, params={"page_size": 2, "page": 2, "sort_by": "total_score"})
            assert _nombres(r2) == ["Cata"]

    async def test_filtros(self, crm: Escenario) -> None:
        datos = await _poblar(crm)
        etapas = datos["etapas"]
        async with _cliente(crm, "admin") as c:

            async def q(**params: Any) -> list[str]:
                return sorted(_nombres(await c.get(LEADS, params=params)))

            assert await q(search="gom") == ["Ana"]
            assert await q(search="ACME 100%") == ["Ana"]
            assert await q(search="%") == ["Ana"]  # el % es texto, no comodin
            assert await q(search="_") == []  # el _ tampoco
            assert await q(search="cto") == ["Beto"]
            assert await q(email="ANA@example.com") == ["Ana"]
            assert await q(phone="573001112233") == ["Ana"]
            assert await q(stage_id=str(etapas["qualified"])) == ["Beto"]
            assert await q(stage_slug="won") == ["Cata"]
            assert await q(status="won") == ["Cata"]
            assert await q(temperature="hot") == ["Ana"]
            assert await q(source_id=str(datos["fuente"])) == ["Ana"]
            assert await q(assigned_user_id=str(crm.user_id)) == ["Ana"]
            assert await q(unassigned="true") == ["Beto", "Cata"]
            assert await q(min_score=50) == ["Ana", "Beto"]
            assert await q(max_score=50) == ["Beto", "Cata"]
            assert await q(min_score=20, max_score=60) == ["Beto"]
            manana = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
            assert await q(follow_up_before=manana) == ["Ana"]
            futuro = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            assert await q(created_from=futuro) == []
            assert await q(created_to=futuro) == ["Ana", "Beto", "Cata"]

    async def test_filtros_incoherentes_son_400_y_los_invalidos_422(self, crm: Escenario) -> None:
        await _pipeline(crm)
        async with _cliente(crm, "admin") as c:
            assert (
                await c.get(LEADS, params={"min_score": 80, "max_score": 20})
            ).status_code == 400
            r = await c.get(
                LEADS, params={"unassigned": "true", "assigned_user_id": str(crm.user_id)}
            )
            assert r.status_code == 400
            assert (await c.get(LEADS, params={"sort_by": "email"})).status_code == 422
            assert (await c.get(LEADS, params={"page_size": 101})).status_code == 422
            assert (await c.get(LEADS, params={"status": "raro"})).status_code == 422

    async def test_la_paginacion_con_empates_no_repite_ni_salta(self, crm: Escenario) -> None:
        await _pipeline(crm)
        for n in range(25):
            await crear_lead(
                crm.client_id, first_name=f"L{n:02d}", email=f"l{n}@example.com", total_score=50
            )
        vistos: list[str] = []
        async with _cliente(crm, "admin") as c:
            for pagina in (1, 2, 3):
                r = await c.get(
                    LEADS, params={"sort_by": "total_score", "page_size": 10, "page": pagina}
                )
                vistos += [item["id"] for item in r.json()["items"]]
        assert len(vistos) == 25
        assert len(set(vistos)) == 25

    async def test_los_nulos_van_al_final_en_ambos_sentidos(self, crm: Escenario) -> None:
        await _poblar(crm)
        async with _cliente(crm, "admin") as c:
            for orden in ("asc", "desc"):
                r = await c.get(LEADS, params={"sort_by": "estimated_value", "order": orden})
                assert _nombres(r)[-1] == "Beto"  # el unico sin valor estimado

    async def test_cada_tenant_ve_solo_lo_suyo(self, crm_dos: tuple[Escenario, Escenario]) -> None:
        a, b = crm_dos
        await _poblar(a)
        async with _cliente(b, "admin") as c:
            r = await c.get(LEADS)
        assert r.json()["total"] == 0
        assert r.json()["items"] == []


# ── Detalle, edicion y borrado ───────────────────────────────────────────────────────────────


class TestEdicionYBorrado:
    async def test_detalle_y_edicion_parcial(self, crm: Escenario) -> None:
        lead = await crear_lead(
            crm.client_id,
            first_name="Ana",
            email="ana@example.com",
            job_title="CEO",
            currency="USD",
        )
        async with _cliente(crm, "agent") as c:
            assert (await c.get(f"{LEADS}/{lead}")).json()["first_name"] == "Ana"
            r = await c.put(
                f"{LEADS}/{lead}",
                json={
                    "company_name": "ACME",
                    "job_title": None,
                    "temperature": "hot",
                    "estimated_value": "1500.50",
                    "currency": None,  # NOT NULL: no se borra
                    "total_score": 100,  # no editable
                    "pipeline_stage_id": str(uuid.uuid4()),  # no editable aqui
                },
            )
        assert r.status_code == 200, r.text
        cuerpo = r.json()
        assert (cuerpo["company_name"], cuerpo["job_title"], cuerpo["temperature"]) == (
            "ACME",
            None,
            "hot",
        )
        assert (cuerpo["estimated_value"], cuerpo["currency"], cuerpo["total_score"]) == (
            "1500.50",
            "USD",
            0,
        )
        assert cuerpo["first_name"] == "Ana"  # lo no enviado no cambia

    async def test_cambiar_el_email_recalcula_la_busqueda_y_no_choca_consigo_mismo(
        self, crm: Escenario
    ) -> None:
        otro = await crear_lead(crm.client_id, email="otro@example.com")
        lead = await crear_lead(crm.client_id, email="ana@example.com", phone="3001112233")
        async with _cliente(crm, "admin") as c:
            assert (
                await c.put(f"{LEADS}/{lead}", json={"email": "ana@example.com"})
            ).status_code == 200
            choque = await c.put(f"{LEADS}/{lead}", json={"email": "OTRO@example.com"})
            assert choque.status_code == 409
            assert choque.json()["details"]["existing_lead_id"] == str(otro)
            assert (
                await c.put(f"{LEADS}/{lead}", json={"email": "nuevo@example.com"})
            ).status_code == 200
            assert [
                i["id"]
                for i in (await c.get(LEADS, params={"email": "nuevo@example.com"})).json()["items"]
            ] == [str(lead)]
            assert (await c.get(LEADS, params={"email": "ana@example.com"})).json()["total"] == 0

    async def test_no_se_puede_quitar_el_ultimo_medio_de_contacto(self, crm: Escenario) -> None:
        lead = await crear_lead(crm.client_id, email="ana@example.com", phone="3001112233")
        async with _cliente(crm, "admin") as c:
            assert (await c.put(f"{LEADS}/{lead}", json={"email": None})).status_code == 200
            r = await c.put(f"{LEADS}/{lead}", json={"phone": None})
            assert r.status_code == 400
            ambos = await c.put(
                f"{LEADS}/{lead}", json={"phone": None, "linkedin_url": "https://linkedin.com/in/a"}
            )
            assert ambos.status_code == 200

    async def test_asignar_valida_al_usuario_y_editar_exige_un_campo(self, crm: Escenario) -> None:
        lead = await crear_lead(crm.client_id, email="ana@example.com")
        async with _cliente(crm, "admin") as c:
            assert (
                await c.put(f"{LEADS}/{lead}", json={"assigned_user_id": str(crm.user_id)})
            ).json()["assigned_user_id"] == str(crm.user_id)
            assert (
                await c.put(f"{LEADS}/{lead}", json={"assigned_user_id": str(uuid.uuid4())})
            ).status_code == 400
            assert (await c.put(f"{LEADS}/{lead}", json={"assigned_user_id": None})).json()[
                "assigned_user_id"
            ] is None
            assert (await c.put(f"{LEADS}/{lead}", json={})).status_code == 422

    async def test_soft_delete_lo_saca_de_todo_y_suelta_el_contacto(self, crm: Escenario) -> None:
        await _pipeline(crm)
        contacto = await _crear_contacto(crm, email="ana@example.com")
        async with _cliente(crm, "admin") as c:
            lead = (await c.post(LEADS, json={"email": "ana@example.com"})).json()["id"]
            assert (await c.delete(f"{LEADS}/{lead}")).status_code == 204
            assert (await c.get(f"{LEADS}/{lead}")).status_code == 404
            assert (await c.put(f"{LEADS}/{lead}", json={"first_name": "X"})).status_code == 404
            assert (await c.delete(f"{LEADS}/{lead}")).status_code == 404
            assert (await c.get(LEADS)).json()["total"] == 0
        f = await _fila(crm, "SELECT lead_id, is_lead FROM contacts WHERE id = :i", i=contacto)
        assert (f.lead_id, f.is_lead) == (None, False)
        g = await _fila(
            crm,
            "SELECT deleted_at IS NOT NULL AS borrado, email IS NULL AS sin FROM leads WHERE id = :i",
            i=lead,
        )
        assert (g.borrado, g.sin) == (True, False)  # sigue en la base: no es una supresion

    async def test_el_agente_no_borra(self, crm: Escenario) -> None:
        lead = await crear_lead(crm.client_id, email="ana@example.com")
        async with _cliente(crm, "agent") as c:
            assert (await c.delete(f"{LEADS}/{lead}")).status_code == 403
        async with _cliente(crm, "supervisor") as c:
            assert (await c.delete(f"{LEADS}/{lead}")).status_code == 204

    async def test_los_leads_de_otro_tenant_son_404_para_todo(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        etapas_b = await _pipeline(b)
        lead_b = await crear_lead(b.client_id, email="b@example.com", first_name="B")
        async with _cliente(a, "admin") as c:
            for r in (
                await c.get(f"{LEADS}/{lead_b}"),
                await c.put(f"{LEADS}/{lead_b}", json={"first_name": "X"}),
                await c.delete(f"{LEADS}/{lead_b}"),
                await c.patch(f"{LEADS}/{lead_b}/stage", json={"stage_id": str(etapas_b["won"])}),
                await c.put(f"{LEADS}/{lead_b}/contact", json={"contact_id": str(a.contact_id)}),
                await c.post(f"{LEADS}/{lead_b}/contact"),
                await c.delete(f"{LEADS}/{lead_b}/contact"),
            ):
                assert r.status_code == 404
        f = await _fila(b, "SELECT first_name, deleted_at FROM leads WHERE id = :i", i=lead_b)
        assert (f.first_name, f.deleted_at) == ("B", None)

    @pytest.mark.parametrize("rol", ["medical"])
    async def test_medical_no_accede(self, crm: Escenario, rol: str) -> None:
        async with _cliente(crm, rol) as c:
            assert (await c.get(LEADS)).status_code == 403
            assert (await c.post(LEADS, json={"email": "a@example.com"})).status_code == 403

    async def test_con_el_modulo_apagado_todo_es_403(self, crm: Escenario) -> None:
        lead = await crear_lead(crm.client_id, email="ana@example.com")
        await activar_modulo(crm.client_id, False)
        async with _cliente(crm, "admin") as c:
            for r in (
                await c.get(LEADS),
                await c.get(f"{LEADS}/{lead}"),
                await c.get(f"{LEADS}/kanban"),
                await c.post(LEADS, json={"email": "b@example.com"}),
            ):
                assert r.status_code == 403
                assert r.json()["error_code"] == "MODULE_DISABLED"


# ── Etapa ───────────────────────────────────────────────────────────────────────────────────


class TestEtapa:
    async def _mover(self, c: Any, lead: uuid.UUID, etapa: uuid.UUID, **extra: Any) -> Any:
        return await c.patch(f"{LEADS}/{lead}/stage", json={"stage_id": str(etapa), **extra})

    async def test_ganado_sella_la_conversion_y_reabrir_la_limpia(self, crm: Escenario) -> None:
        etapas = await _pipeline(crm)
        lead = await crear_lead(
            crm.client_id, email="a@example.com", pipeline_stage_id=etapas["new"]
        )
        async with _cliente(crm, "agent") as c:
            r = await self._mover(c, lead, etapas["won"])
            assert r.status_code == 200, r.text
            assert (r.json()["status"], r.json()["converted_at"] is not None) == ("won", True)
            assert r.json()["pipeline_stage_id"] == str(etapas["won"])
            abierto = await self._mover(c, lead, etapas["qualified"])
        assert (abierto.json()["status"], abierto.json()["converted_at"]) == ("active", None)

    async def test_perdido(self, crm: Escenario) -> None:
        etapas = await _pipeline(crm)
        lead = await crear_lead(
            crm.client_id, email="a@example.com", pipeline_stage_id=etapas["new"]
        )
        async with _cliente(crm, "admin") as c:
            r = await self._mover(c, lead, etapas["lost"])
        assert (r.json()["status"], r.json()["converted_at"]) == ("lost", None)

    async def test_descalificar_exige_motivo_y_reabrir_lo_borra(self, crm: Escenario) -> None:
        etapas = await _pipeline(crm)
        lead = await crear_lead(
            crm.client_id, email="a@example.com", pipeline_stage_id=etapas["new"]
        )
        async with _cliente(crm, "admin") as c:
            assert (await self._mover(c, lead, etapas["disqualified"])).status_code == 400
            assert (
                await self._mover(c, lead, etapas["disqualified"], reason="   ")
            ).status_code == 400
            ok = await self._mover(c, lead, etapas["disqualified"], reason="Sin presupuesto")
            assert ok.status_code == 200
            assert ok.json()["status"] == "disqualified"
            assert ok.json()["disqualified_reason"] == "Sin presupuesto"
            assert ok.json()["disqualified_at"] is not None
            reabierto = await self._mover(c, lead, etapas["new"])
        assert reabierto.json()["status"] == "active"
        assert (reabierto.json()["disqualified_reason"], reabierto.json()["disqualified_at"]) == (
            None,
            None,
        )

    async def test_una_etapa_terminal_propia_deja_el_estado_como_estaba(
        self, crm: Escenario
    ) -> None:
        etapas = await _pipeline(crm)
        archivo = await crear_etapa(crm.client_id, "archivo", 9, is_terminal=True)
        lead = await crear_lead(
            crm.client_id, email="a@example.com", pipeline_stage_id=etapas["new"], status="lost"
        )
        async with _cliente(crm, "admin") as c:
            r = await self._mover(c, lead, archivo)
        assert r.json()["status"] == "lost"

    async def test_la_etapa_debe_existir_en_el_tenant(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        etapas = await _pipeline(a)
        ajena = await crear_etapa(b.client_id, "ajena", 0)
        lead = await crear_lead(a.client_id, email="a@example.com", pipeline_stage_id=etapas["new"])
        async with _cliente(a, "admin") as c:
            assert (await self._mover(c, lead, ajena)).status_code == 400
            assert (await self._mover(c, lead, uuid.uuid4())).status_code == 400
            assert (await c.patch(f"{LEADS}/{lead}/stage", json={})).status_code == 422
        f = await _fila(a, "SELECT pipeline_stage_id FROM leads WHERE id = :i", i=lead)
        assert f.pipeline_stage_id == etapas["new"]


# ── Kanban ──────────────────────────────────────────────────────────────────────────────────


class TestKanban:
    async def test_una_columna_por_etapa_con_totales_reales(self, crm: Escenario) -> None:
        etapas = await _pipeline(crm)
        for n in range(5):
            await crear_lead(
                crm.client_id,
                first_name=f"N{n}",
                email=f"n{n}@example.com",
                pipeline_stage_id=etapas["new"],
                total_score=n * 10,
                estimated_value=Decimal(100 * (n + 1)),
            )
        await crear_lead(
            crm.client_id,
            email="borrado@example.com",
            pipeline_stage_id=etapas["new"],
            estimated_value=Decimal(99999),
            deleted_at=datetime.now(timezone.utc),
        )
        async with _cliente(crm, "agent") as c:
            r = await c.get(f"{LEADS}/kanban", params={"per_column": 3})
        assert r.status_code == 200, r.text
        columnas = r.json()
        assert [col["stage"]["slug"] for col in columnas] == [
            "new",
            "qualified",
            "won",
            "lost",
            "disqualified",
        ]
        nueva = columnas[0]
        assert [lead["first_name"] for lead in nueva["leads"]] == ["N4", "N3", "N2"]  # mayor score
        assert nueva["total"] == 5  # el real, no los 3 devueltos
        assert Decimal(nueva["total_value"]) == Decimal(1500)  # suma de los 5, sin el borrado
        assert all(col["leads"] == [] and col["total"] == 0 for col in columnas[1:])
        assert Decimal(columnas[1]["total_value"]) == 0

    async def test_filtros(self, crm: Escenario) -> None:
        datos = await _poblar(crm)
        async with _cliente(crm, "admin") as c:
            r = await c.get(f"{LEADS}/kanban", params={"temperature": "hot"})
            assert [lead["first_name"] for col in r.json() for lead in col["leads"]] == ["Ana"]
            r = await c.get(f"{LEADS}/kanban", params={"source_id": str(datos["fuente"])})
            assert sum(col["total"] for col in r.json()) == 1
            r = await c.get(f"{LEADS}/kanban", params={"assigned_user_id": str(crm.user_id)})
            assert sum(col["total"] for col in r.json()) == 1

    async def test_las_consultas_no_crecen_con_el_numero_de_etapas(self, crm: Escenario) -> None:
        async def contar(etapas_extra: int) -> int:
            for i in range(etapas_extra):
                await crear_etapa(crm.client_id, f"e{uuid.uuid4().hex[:6]}", 100 + i)
            sentencias: list[str] = []

            def registrar(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
                if "FROM leads" in statement or "FROM lead_pipeline_stages" in statement:
                    sentencias.append(statement)

            event.listen(engine.sync_engine, "before_cursor_execute", registrar)
            try:
                async with _cliente(crm, "admin") as c:
                    assert (await c.get(f"{LEADS}/kanban")).status_code == 200
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", registrar)
            return len(sentencias)

        await crear_etapa(crm.client_id, "base", 0)
        pocas = await contar(0)
        muchas = await contar(8)
        assert pocas == muchas
        assert pocas <= 4

    async def test_cada_tenant_ve_su_tablero(self, crm_dos: tuple[Escenario, Escenario]) -> None:
        a, b = crm_dos
        await _poblar(a)
        async with _cliente(b, "admin") as c:
            assert (await c.get(f"{LEADS}/kanban")).json() == []


# ── Enlace con contactos ─────────────────────────────────────────────────────────────────────


class TestContactos:
    async def test_enlazar_y_desenlazar(self, crm: Escenario) -> None:
        lead = await crear_lead(crm.client_id, email="ana@example.com")
        async with _cliente(crm, "agent") as c:
            r = await c.put(f"{LEADS}/{lead}/contact", json={"contact_id": str(crm.contact_id)})
            assert r.status_code == 200, r.text
            assert r.json()["contact_id"] == str(crm.contact_id)
            f = await _fila(
                crm, "SELECT lead_id, is_lead FROM contacts WHERE id = :i", i=crm.contact_id
            )
            assert (f.lead_id, f.is_lead) == (lead, True)
            suelto = await c.delete(f"{LEADS}/{lead}/contact")
            assert suelto.json()["contact_id"] is None
            assert (await c.delete(f"{LEADS}/{lead}/contact")).status_code == 200  # idempotente
        f = await _fila(
            crm, "SELECT lead_id, is_lead FROM contacts WHERE id = :i", i=crm.contact_id
        )
        assert (f.lead_id, f.is_lead) == (None, False)

    async def test_un_contacto_ya_enlazado_a_otro_lead_es_409_y_cambiar_de_contacto_suelta_el_anterior(
        self, crm: Escenario
    ) -> None:
        lead1 = await crear_lead(crm.client_id, email="uno@example.com")
        lead2 = await crear_lead(crm.client_id, email="dos@example.com")
        otro = await _crear_contacto(crm)
        async with _cliente(crm, "admin") as c:
            await c.put(f"{LEADS}/{lead1}/contact", json={"contact_id": str(crm.contact_id)})
            r = await c.put(f"{LEADS}/{lead2}/contact", json={"contact_id": str(crm.contact_id)})
            assert r.status_code == 409
            assert r.json()["details"]["existing_lead_id"] == str(lead1)
            await c.put(f"{LEADS}/{lead1}/contact", json={"contact_id": str(otro)})
        viejo = await _fila(
            crm, "SELECT lead_id, is_lead FROM contacts WHERE id = :i", i=crm.contact_id
        )
        nuevo = await _fila(crm, "SELECT lead_id, is_lead FROM contacts WHERE id = :i", i=otro)
        assert (viejo.lead_id, viejo.is_lead) == (None, False)
        assert (nuevo.lead_id, nuevo.is_lead) == (lead1, True)

    async def test_contactos_invalidos_o_ajenos_son_400(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        lead = await crear_lead(a.client_id, email="ana@example.com")
        destino = await _crear_contacto(a)
        fusionado = await _crear_contacto(a, merged_into_id=destino)
        anonimo = await _crear_contacto(a, is_gdpr_deleted=True)
        async with _cliente(a, "admin") as c:
            for contacto in (b.contact_id, fusionado, anonimo, uuid.uuid4()):
                r = await c.put(f"{LEADS}/{lead}/contact", json={"contact_id": str(contacto)})
                assert r.status_code == 400, contacto
        f = await _fila(b, "SELECT lead_id FROM contacts WHERE id = :i", i=b.contact_id)
        assert f.lead_id is None

    async def test_crear_un_contacto_desde_el_lead(self, crm: Escenario) -> None:
        from app.core.encryption import blind_index

        lead = await crear_lead(
            crm.client_id,
            first_name="Nueva",
            last_name="Persona",
            email="Nueva@Example.com",
            phone="+57 311 222-3344",
        )
        async with _cliente(crm, "agent") as c:
            r = await c.post(f"{LEADS}/{lead}/contact")
            assert r.status_code == 201, r.text
            contacto = uuid.UUID(r.json()["contact_id"])
            otra = await c.post(f"{LEADS}/{lead}/contact")
            assert otra.status_code == 409
        f = await _fila(
            crm,
            "SELECT first_name, last_name, display_name, lead_id, is_lead FROM contacts WHERE id = :i",
            i=contacto,
        )
        assert (f.first_name, f.display_name, f.lead_id, f.is_lead) == (
            "Nueva",
            "Nueva Persona",
            lead,
            True,
        )
        async with tenant_session(crm.client_id) as s:
            ident = (
                await s.execute(
                    text(
                        "SELECT channel, identifier_hash FROM contact_identifiers WHERE contact_id = :c"
                    ),
                    {"c": str(contacto)},
                )
            ).all()
        hashes = {i.channel: i.identifier_hash for i in ident}
        assert hashes["email"] == blind_index("nueva@example.com", crm.client_id)
        assert hashes["whatsapp"] == blind_index("+573112223344", crm.client_id)

    async def test_no_crea_un_contacto_duplicado_si_ya_existe_uno_con_ese_email(
        self, crm: Escenario
    ) -> None:
        await _crear_contacto(crm, email="ana@example.com")
        lead = await crear_lead(crm.client_id, email="ana@example.com")
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{LEADS}/{lead}/contact")
        assert r.status_code == 409
        assert "enlazalo" in r.json()["message"]
