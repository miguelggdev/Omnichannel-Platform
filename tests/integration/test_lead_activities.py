# ruff: noqa: F811
"""Historial de leads y deduplicacion por LinkedIn contra PostgreSQL real (Sprint 16, ADR-084).

Requiere base de datos: `pytest tests/ --run-db`. Lo que un doble no prueba: que cada accion deje
su rastro con quien y cuando, que el historial **no copie datos personales** (ni el valor de un
email ni el motivo de una descalificacion), que sobreviva a la anonimizacion RGPD sin exponer nada,
que RLS lo aisle por tenant, y que la URL de LinkedIn se canonice y deduplique en todos los
caminos de alta (API, captura publica e importacion).
"""

import io
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.database import tenant_session
from app.main import create_app
from app.models.lead import canonicalizar_linkedin
from app.services.lead_pipeline import generate_capture_token
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


async def _etapas(esc: Escenario) -> dict[str, uuid.UUID]:
    datos = [("new", False), ("qualified", False), ("won", True), ("disqualified", True)]
    return {
        slug: await crear_etapa(esc.client_id, slug, i, is_terminal=term)
        for i, (slug, term) in enumerate(datos)
    }


async def _actividades(esc: Escenario, lead: str | uuid.UUID) -> list[Any]:
    async with tenant_session(esc.client_id) as s:
        return list(
            (
                await s.execute(
                    text(
                        "SELECT activity_type, user_id, metadata FROM lead_activities "
                        "WHERE lead_id = :l ORDER BY created_at, id"
                    ),
                    {"l": str(lead)},
                )
            ).all()
        )


def _tipos(filas: list[Any]) -> list[str]:
    return [f.activity_type for f in filas]


# ── Eventos de la API ──────────────────────────────────────────────────────────────────────────


class TestEventos:
    async def test_crear_registra_quien_etapa_y_fuente(self, crm: Escenario) -> None:
        await _etapas(crm)
        fuente = await crear_fuente(crm.client_id)
        async with _cliente(crm, "agent") as c:
            lead = (
                await c.post(LEADS, json={"email": "ana@example.com", "source_id": str(fuente)})
            ).json()["id"]
        (act,) = await _actividades(crm, lead)
        assert act.activity_type == "created"
        assert act.user_id == crm.user_id
        assert act.metadata == {"stage": "new", "source_id": str(fuente)}

    async def test_el_auto_enlace_con_un_contacto_queda_anotado_como_automatico(
        self, crm: Escenario
    ) -> None:
        from app.models.contact import Contact
        from app.models.contact_identifier import ContactIdentifier

        await _etapas(crm)
        async with tenant_session(crm.client_id) as s:
            contacto = Contact(client_id=crm.client_id, first_name="C")
            s.add(contacto)
            await s.flush()
            s.add(
                ContactIdentifier(
                    client_id=crm.client_id,
                    contact_id=contacto.id,
                    channel="email",
                    identifier_value="ana@example.com",
                )
            )
            contacto_id = contacto.id
        async with _cliente(crm, "admin") as c:
            lead = (await c.post(LEADS, json={"email": "ana@example.com"})).json()["id"]
        tipos = await _actividades(crm, lead)
        assert _tipos(tipos) == ["created", "contact_linked"]
        assert tipos[1].metadata == {"contact_id": str(contacto_id), "automatic": True}

    async def test_cambiar_de_etapa_guarda_de_donde_a_donde_pero_no_el_motivo(
        self, crm: Escenario
    ) -> None:
        etapas = await _etapas(crm)
        lead = await crear_lead(
            crm.client_id, email="a@example.com", pipeline_stage_id=etapas["new"]
        )
        async with _cliente(crm, "supervisor") as c:
            await c.patch(f"{LEADS}/{lead}/stage", json={"stage_id": str(etapas["qualified"])})
            await c.patch(
                f"{LEADS}/{lead}/stage",
                json={"stage_id": str(etapas["disqualified"]), "reason": "MOTIVO-PRIVADO-DE-ANA"},
            )
        filas = await _actividades(crm, lead)
        assert _tipos(filas) == ["stage_changed", "stage_changed"]
        assert filas[0].metadata == {
            "from_stage": "new",
            "to_stage": "qualified",
            "status": "active",
        }
        assert filas[1].metadata == {
            "from_stage": "qualified",
            "to_stage": "disqualified",
            "status": "disqualified",
        }
        assert all(f.user_id == crm.user_id for f in filas)
        assert "MOTIVO-PRIVADO" not in str([f.metadata for f in filas])

    async def test_asignar_y_desasignar(self, crm: Escenario) -> None:
        lead = await crear_lead(crm.client_id, email="a@example.com")
        async with _cliente(crm, "admin") as c:
            await c.put(f"{LEADS}/{lead}", json={"assigned_user_id": str(crm.user_id)})
            await c.put(
                f"{LEADS}/{lead}", json={"assigned_user_id": str(crm.user_id)}
            )  # sin cambio
            await c.put(f"{LEADS}/{lead}", json={"assigned_user_id": None})
        filas = await _actividades(crm, lead)
        assert _tipos(filas) == ["assigned", "unassigned"]
        assert filas[0].metadata == {"from_user": None, "to_user": str(crm.user_id)}
        assert filas[1].metadata == {"from_user": str(crm.user_id), "to_user": None}

    async def test_editar_guarda_los_nombres_de_los_campos_y_nunca_sus_valores(
        self, crm: Escenario
    ) -> None:
        lead = await crear_lead(
            crm.client_id, email="ana@example.com", company_name="ACME", job_title="CEO"
        )
        async with _cliente(crm, "admin") as c:
            await c.put(
                f"{LEADS}/{lead}",
                json={
                    "company_name": "NUEVA-EMPRESA-SECRETA",
                    "job_title": "CEO",  # igual: no cuenta
                    "email": "otro-privado@example.com",
                },
            )
            await c.put(f"{LEADS}/{lead}", json={"job_title": "CEO"})  # nada cambia: sin actividad
        (fila,) = await _actividades(crm, lead)
        assert fila.activity_type == "updated"
        assert fila.metadata == {"fields": ["company_name", "email"]}
        assert "SECRETA" not in str(fila.metadata)
        assert "privado" not in str(fila.metadata)

    async def test_enlazar_desenlazar_crear_contacto_y_borrar(self, crm: Escenario) -> None:
        lead = await crear_lead(crm.client_id, email="ana@example.com", first_name="Ana")
        async with _cliente(crm, "admin") as c:
            await c.put(f"{LEADS}/{lead}/contact", json={"contact_id": str(crm.contact_id)})
            await c.delete(f"{LEADS}/{lead}/contact")
            await c.delete(f"{LEADS}/{lead}/contact")  # ya suelto: sin actividad
            await c.post(f"{LEADS}/{lead}/contact")
            await c.delete(f"{LEADS}/{lead}")
        filas = await _actividades(crm, lead)
        assert _tipos(filas) == [
            "contact_linked",
            "contact_unlinked",
            "contact_created",
            "deleted",
        ]
        assert filas[0].metadata == {"contact_id": str(crm.contact_id)}


# ── Linea de tiempo ────────────────────────────────────────────────────────────────────────────


class TestLineaDeTiempo:
    async def test_devuelve_lo_mas_reciente_primero_y_pagina(self, crm: Escenario) -> None:
        etapas = await _etapas(crm)
        lead = await crear_lead(
            crm.client_id, email="a@example.com", pipeline_stage_id=etapas["new"]
        )
        async with _cliente(crm, "agent") as c:
            for destino in ("qualified", "new", "qualified"):
                await c.patch(f"{LEADS}/{lead}/stage", json={"stage_id": str(etapas[destino])})
            r = await c.get(f"{LEADS}/{lead}/activities", params={"page_size": 2})
            assert r.status_code == 200, r.text
            cuerpo = r.json()
            assert (cuerpo["total"], cuerpo["total_pages"], len(cuerpo["items"])) == (3, 2, 2)
            assert cuerpo["items"][0]["metadata"]["to_stage"] == "qualified"  # la ultima
            assert cuerpo["items"][1]["metadata"]["to_stage"] == "new"
            pag2 = (
                await c.get(f"{LEADS}/{lead}/activities", params={"page_size": 2, "page": 2})
            ).json()
            assert [i["metadata"]["to_stage"] for i in pag2["items"]] == ["qualified"]
            assert set(cuerpo["items"][0]) == {
                "id",
                "activity_type",
                "user_id",
                "description",
                "metadata",
                "created_at",
            }

    async def test_404_si_el_lead_es_de_otro_tenant_o_esta_borrado_y_403_a_medical(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        from datetime import datetime, timezone

        a, b = crm_dos
        lead_b = await crear_lead(b.client_id, email="b@example.com")
        borrado = await crear_lead(
            a.client_id, email="x@example.com", deleted_at=datetime.now(timezone.utc)
        )
        async with _cliente(a, "admin") as c:
            assert (await c.get(f"{LEADS}/{lead_b}/activities")).status_code == 404
            assert (await c.get(f"{LEADS}/{borrado}/activities")).status_code == 404
        async with _cliente(a, "medical") as c:
            assert (await c.get(f"{LEADS}/{lead_b}/activities")).status_code == 403

    async def test_con_el_modulo_apagado_es_403(self, crm: Escenario) -> None:
        lead = await crear_lead(crm.client_id, email="a@example.com")
        await activar_modulo(crm.client_id, False)
        async with _cliente(crm, "admin") as c:
            assert (await c.get(f"{LEADS}/{lead}/activities")).status_code == 403

    async def test_rls_aisla_el_historial_y_no_deja_escribir_en_otro_tenant(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        lead_a = await crear_lead(a.client_id, email="a@example.com")
        async with _cliente(a, "admin") as c:
            await c.put(f"{LEADS}/{lead_a}", json={"company_name": "X"})
        async with tenant_session(b.client_id) as s:
            assert (await s.execute(text("SELECT count(*) FROM lead_activities"))).scalar_one() == 0
        async with tenant_session(b.client_id) as s:
            with pytest.raises(DBAPIError):
                await s.execute(
                    text(
                        "INSERT INTO lead_activities (client_id, lead_id, activity_type) "
                        "VALUES (:c, :l, 'x')"
                    ),
                    {"c": str(a.client_id), "l": str(lead_a)},
                )


# ── Captura, importacion y RGPD ────────────────────────────────────────────────────────────────


class TestOtrasEntradas:
    async def test_captura_y_recaptura(self, crm: Escenario) -> None:
        await _etapas(crm)
        token, hash_ = generate_capture_token()
        fuente = await crear_fuente(crm.client_id, source_type="web_form", capture_token_hash=hash_)
        n = uuid.uuid4().int
        ip = {"x-forwarded-for": f"10.{n % 250}.{(n >> 8) % 250}.9"}
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t") as c:
            await c.post(
                f"/api/v1/capture/{token}",
                json={"email": "ana@example.com", "consent": True},
                headers=ip,
            )
            await c.post(
                f"/api/v1/capture/{token}",
                json={"email": "ANA@example.com", "consent": True},
                headers=ip,
            )
        async with tenant_session(crm.client_id) as s:
            lead = (await s.execute(text("SELECT id FROM leads"))).scalar_one()
        filas = await _actividades(crm, lead)
        assert _tipos(filas) == ["captured", "recaptured"]
        assert all(f.user_id is None for f in filas)  # la hizo el sistema
        assert filas[0].metadata == {"source_id": str(fuente)}

    async def test_importar_deja_una_actividad_por_lead_con_el_mismo_lote_y_dry_run_ninguna(
        self, crm: Escenario
    ) -> None:
        await _etapas(crm)
        csv_texto = b"email\na@example.com\nb@example.com\nc@example.com\n"
        async with _cliente(crm, "admin") as c:
            await c.post(
                "/api/v1/leads/import",
                files={"file": ("l.csv", csv_texto)},
                data={"dry_run": "true"},
            )
            async with tenant_session(crm.client_id) as s:
                assert (
                    await s.execute(text("SELECT count(*) FROM lead_activities"))
                ).scalar_one() == 0
            await c.post("/api/v1/leads/import", files={"file": ("l.csv", csv_texto)})
        async with tenant_session(crm.client_id) as s:
            filas = (
                await s.execute(
                    text(
                        "SELECT activity_type, user_id, metadata->>'batch_id' AS lote FROM lead_activities"
                    )
                )
            ).all()
        assert [f.activity_type for f in filas] == ["imported"] * 3
        assert len({f.lote for f in filas}) == 1
        assert {f.user_id for f in filas} == {crm.user_id}

    async def test_la_anonimizacion_deja_su_rastro_y_el_historial_no_expone_nada(
        self, crm: Escenario
    ) -> None:
        etapas = await _etapas(crm)
        lead = await crear_lead(
            crm.client_id,
            first_name="Ana",
            email="ana@example.com",
            contact_id=crm.contact_id,
            pipeline_stage_id=etapas["new"],
        )
        async with _cliente(crm, "admin") as c:
            await c.put(f"{LEADS}/{lead}", json={"company_name": "ACME"})
            r = await c.delete(f"/api/v1/admin/contacts/{crm.contact_id}/gdpr-delete")
            assert r.json()["leads_anonymized"] == 1
        filas = await _actividades(crm, lead)
        assert _tipos(filas) == ["updated", "gdpr_anonymized"]
        assert filas[1].metadata == {"via": "contact"}
        assert "ana@example.com" not in str([f.metadata for f in filas]).lower()

    async def test_la_supresion_de_un_lead_suelto_tambien_deja_rastro(self, crm: Escenario) -> None:
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@example.com")
        async with _cliente(crm, "admin") as c:
            await c.delete(f"/api/v1/admin/leads/{lead}/gdpr-delete")
        filas = await _actividades(crm, lead)
        assert _tipos(filas) == ["gdpr_anonymized"]
        assert filas[0].metadata == {"via": "lead"}


# ── LinkedIn: canonizacion y deduplicacion ─────────────────────────────────────────────────────


class TestLinkedIn:
    async def test_el_sql_de_la_migracion_y_la_funcion_de_python_dan_lo_mismo(
        self, crm: Escenario
    ) -> None:
        import importlib.util
        from pathlib import Path

        ruta = (
            Path(__file__).parents[2]
            / "migrations"
            / "versions"
            / "026_lead_activities_linkedin.py"
        )
        spec = importlib.util.spec_from_file_location("m026", ruta)
        assert spec is not None
        assert spec.loader is not None
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        muestras = [
            "HTTP://LinkedIn.com/in/Ana/?trk=x#y",
            "https://www.linkedin.com/in/ana///",
            "  https://linkedin.com/in/ana  ",
            "https://linkedin.com/in/jos%C3%A9-p",
            "https://linkedin.com/in/ana",
        ]
        async with tenant_session(crm.client_id) as s:
            for m in muestras:
                sql = modulo.CANONICA_SQL.replace("linkedin_url", ":u")
                en_sql = (await s.execute(text(f"SELECT {sql}"), {"u": m})).scalar_one()
                assert en_sql == canonicalizar_linkedin(m), m

    async def test_se_guarda_canonica_y_duplica_en_la_api(self, crm: Escenario) -> None:
        await _etapas(crm)
        async with _cliente(crm, "admin") as c:
            uno = await c.post(LEADS, json={"linkedin_url": "https://LinkedIn.com/in/Ana/?trk=1"})
            assert uno.status_code == 201, uno.text
            assert uno.json()["linkedin_url"] == "https://linkedin.com/in/ana"
            dup = await c.post(LEADS, json={"linkedin_url": "https://www.linkedin.com/in/ana"})
            assert dup.status_code == 201  # www. es otro host: no se adivina que sea el mismo
            igual = await c.post(LEADS, json={"linkedin_url": "https://linkedin.com/in/ana/"})
            assert igual.status_code == 409
            assert igual.json()["details"] == {
                "existing_lead_id": uno.json()["id"],
                "field": "linkedin",
            }

    async def test_editar_a_la_url_de_otro_lead_es_409_y_a_la_propia_no(
        self, crm: Escenario
    ) -> None:
        otro = await crear_lead(crm.client_id, linkedin_url="https://linkedin.com/in/otro")
        lead = await crear_lead(crm.client_id, linkedin_url="https://linkedin.com/in/ana")
        async with _cliente(crm, "admin") as c:
            propia = await c.put(
                f"{LEADS}/{lead}", json={"linkedin_url": "https://linkedin.com/in/ana/"}
            )
            assert propia.status_code == 200
            ajena = await c.put(
                f"{LEADS}/{lead}", json={"linkedin_url": "https://LINKEDIN.com/in/otro"}
            )
            assert ajena.status_code == 409
            assert ajena.json()["details"]["existing_lead_id"] == str(otro)

    async def test_la_misma_url_en_otro_tenant_o_tras_borrar_no_choca(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        await _etapas(a)
        await _etapas(b)
        url = {"linkedin_url": "https://linkedin.com/in/ana"}
        async with _cliente(a, "admin") as ca, _cliente(b, "admin") as cb:
            lead = (await ca.post(LEADS, json=url)).json()["id"]
            assert (await cb.post(LEADS, json=url)).status_code == 201
            await ca.delete(f"{LEADS}/{lead}")
            assert (await ca.post(LEADS, json=url)).status_code == 201

    async def test_la_captura_publica_no_duplica_por_linkedin(self, crm: Escenario) -> None:
        await _etapas(crm)
        token, hash_ = generate_capture_token()
        await crear_fuente(crm.client_id, source_type="web_form", capture_token_hash=hash_)
        n = uuid.uuid4().int
        ip = {"x-forwarded-for": f"10.{n % 250}.{(n >> 8) % 250}.7"}
        cuerpo = {"linkedin_url": "https://linkedin.com/in/ana", "consent": True}
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t") as c:
            for url in ("https://linkedin.com/in/ana", "HTTPS://LINKEDIN.COM/in/ana/?x=1"):
                r = await c.post(
                    f"/api/v1/capture/{token}", json={**cuerpo, "linkedin_url": url}, headers=ip
                )
                assert r.status_code == 202
        async with tenant_session(crm.client_id) as s:
            assert (await s.execute(text("SELECT count(*) FROM leads"))).scalar_one() == 1

    async def test_la_importacion_deduplica_por_linkedin_contra_la_base_y_dentro_del_archivo(
        self, crm: Escenario
    ) -> None:
        await _etapas(crm)
        await crear_lead(crm.client_id, linkedin_url="https://linkedin.com/in/ana")
        csv_texto = (
            b"nombre,linkedin\n"
            b"Ana,http://www.linkedin.com/in/ana\n"  # www.: otro host, entra
            b"Ana2,https://linkedin.com/in/ana/\n"  # igual que el existente
            b"Beto,https://linkedin.com/in/beto\n"
            b"Beto2,https://LinkedIn.com/in/beto?trk=a\n"  # repetida en el archivo
        )
        async with _cliente(crm, "admin") as c:
            r = await c.post("/api/v1/leads/import", files={"file": ("l.csv", csv_texto)})
        cuerpo = r.json()
        assert (cuerpo["imported"], cuerpo["duplicates"]) == (2, 2)
        assert {e["row"]: e["error"] for e in cuerpo["errors"]} == {
            3: "ya existe un lead con ese email, telefono o LinkedIn",
            5: "repetida en el archivo",
        }


# ── Importacion de Excel ───────────────────────────────────────────────────────────────────────


def _xlsx(filas: list[list[object]]) -> bytes:
    libro = Workbook()
    hoja = libro.active
    assert hoja is not None
    for fila in filas:
        hoja.append(fila)
    salida = io.BytesIO()
    libro.save(salida)
    return salida.getvalue()


class TestImportarExcel:
    async def test_importa_un_xlsx_igual_que_un_csv(self, crm: Escenario) -> None:
        await _etapas(crm)
        contenido = _xlsx(
            [
                ["Nombre", "Correo", "Celular", "Empresa"],
                ["Ana", "ana@example.com", 573001112233.0, "ACME"],
                ["Beto", "mal-email", None, None],
                ["Cata", "cata@example.com", None, None],
            ]
        )
        async with _cliente(crm, "admin") as c:
            r = await c.post(
                "/api/v1/leads/import",
                files={
                    "file": (
                        "leads.xlsx",
                        contenido,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
            )
        assert r.status_code == 200, r.text
        cuerpo = r.json()
        assert (cuerpo["total_rows"], cuerpo["imported"], cuerpo["invalid"]) == (3, 2, 1)
        async with tenant_session(crm.client_id) as s:
            tel = (
                await s.execute(
                    text(
                        "SELECT phone::text IS NOT NULL AS hay FROM leads WHERE first_name = 'Ana'"
                    )
                )
            ).scalar_one()
        assert tel is True

    async def test_el_formato_se_decide_por_los_bytes_no_por_el_nombre(
        self, crm: Escenario
    ) -> None:
        await _etapas(crm)
        async with _cliente(crm, "admin") as c:
            r = await c.post(
                "/api/v1/leads/import",
                files={"file": ("datos.csv", _xlsx([["email"], ["x@example.com"]]), "text/csv")},
            )
        assert r.json()["imported"] == 1

    async def test_un_excel_con_macros_o_una_bomba_son_400_y_no_crean_nada(
        self, crm: Escenario
    ) -> None:
        import zipfile

        await _etapas(crm)

        def zip_de(entradas: dict[str, bytes]) -> bytes:
            salida = io.BytesIO()
            with zipfile.ZipFile(salida, "w", zipfile.ZIP_DEFLATED) as z:
                for nombre, datos in entradas.items():
                    z.writestr(nombre, datos)
            return salida.getvalue()

        macros = zip_de({"xl/workbook.xml": b"<x/>", "xl/vbaProject.bin": b"MZ"})
        bomba = zip_de({"xl/workbook.xml": b"<x/>", "xl/relleno.bin": b"\x00" * (25 * 1024 * 1024)})
        async with _cliente(crm, "admin") as c:
            for contenido in (macros, bomba, b"PK\x03\x04basura"):
                r = await c.post("/api/v1/leads/import", files={"file": ("x.xlsx", contenido)})
                assert r.status_code == 400, r.text
        async with tenant_session(crm.client_id) as s:
            assert (await s.execute(text("SELECT count(*) FROM leads"))).scalar_one() == 0


async def test_registrar_actividad_rechaza_metadata_con_datos_personales() -> None:
    from app.services.lead_activity import registrar_actividad

    for clave in (
        "email",
        "phone",
        "first_name",
        "linkedin_url",
        "reason",
        "disqualified_reason",
        "Email",
    ):
        with pytest.raises(ValueError, match="datos personales"):
            registrar_actividad(
                None,  # type: ignore[arg-type]
                client_id=uuid.uuid4(),
                lead_id=uuid.uuid4(),
                tipo="updated",
                **{clave: "x"},
            )
