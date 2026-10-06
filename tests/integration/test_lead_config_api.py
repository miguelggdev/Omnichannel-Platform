# ruff: noqa: F811
"""Modulo de leads, etapas y fuentes por la API contra PostgreSQL real con RLS (Sprint 16).

Requiere base de datos: `pytest tests/ --run-db`. Lo que un doble no prueba: que apagar el
modulo corte la API de verdad pero conserve los datos, que reordenar pase por el UNIQUE
diferible, que el token de captura quede solo como hash y rotarlo invalide el anterior, y que
cada tenant solo vea y toque lo suyo.
"""

import hashlib
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.database import AsyncSessionLocal, tenant_session
from app.services.lead_pipeline import DEFAULT_STAGES, hash_capture_token
from tests.integration.lead_helpers import (
    activar_modulo,
    crear_etapa,
    crear_fuente,
    crear_lead,
    limpiar_leads,
)
from tests.integration.test_crm_api import Escenario, _cliente, dos_tenants, escenario  # noqa: F401

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

STAGES = "/api/v1/lead-pipeline-stages"
SOURCES = "/api/v1/lead-sources"
STATUS = "/api/v1/lead-management/status"


@pytest_asyncio.fixture
async def crm(escenario: Escenario) -> AsyncGenerator[Escenario, None]:
    """Tenant del CRM con el modulo de leads activo y limpio al terminar."""
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


async def _slugs(c, esc: Escenario) -> list[str]:
    return [e["slug"] for e in (await c.get(STAGES)).json()]


# ── Modulo ──────────────────────────────────────────────────────────────────────────────────


class TestModulo:
    async def test_un_tenant_sin_el_modulo_recibe_403_module_disabled(
        self, escenario: Escenario
    ) -> None:
        async with _cliente(escenario, "admin") as c:
            for url in (STAGES, SOURCES):
                r = await c.get(url)
                assert r.status_code == 403
                assert r.json()["error_code"] == "MODULE_DISABLED"
            assert (await c.post(STAGES, json={"name": "X", "slug": "x"})).status_code == 403

    async def test_el_estado_se_puede_consultar_aunque_este_apagado(
        self, escenario: Escenario
    ) -> None:
        async with _cliente(escenario, "agent") as c:
            assert (await c.get(STATUS)).json() == {"enabled": False, "stages": 0}

    async def test_el_rol_se_comprueba_antes_que_el_modulo(self, escenario: Escenario) -> None:
        async with _cliente(escenario, "medical") as c:
            r = await c.get(STAGES)
        assert r.status_code == 403
        assert r.json()["error_code"] == "FORBIDDEN"

    async def test_el_super_admin_lo_activa_y_se_crean_las_etapas_por_defecto(
        self, escenario: Escenario
    ) -> None:
        url = f"/api/v1/platform/clients/{escenario.client_id}/lead-management"
        async with _cliente(escenario, "super_admin") as c:
            r = await c.put(url, json={"enabled": True})
            assert r.status_code == 200, r.text
            assert r.json() == {
                "client_id": str(escenario.client_id),
                "enabled": True,
                "stages_created": len(DEFAULT_STAGES),
            }
            otra = await c.put(url, json={"enabled": True})
            assert otra.json()["stages_created"] == 0  # idempotente
        async with _cliente(escenario, "agent") as c:
            assert (await c.get(STATUS)).json() == {"enabled": True, "stages": len(DEFAULT_STAGES)}
            assert len(await _slugs(c, escenario)) == len(DEFAULT_STAGES)
        await limpiar_leads(escenario.client_id)

    async def test_apagarlo_no_borra_nada_y_corta_la_api(self, crm: Escenario) -> None:
        await crear_etapa(crm.client_id, "new", 0)
        url = f"/api/v1/platform/clients/{crm.client_id}/lead-management"
        async with _cliente(crm, "super_admin") as c:
            assert (await c.put(url, json={"enabled": False})).json()["enabled"] is False
        async with _cliente(crm, "admin") as c:
            assert (await c.get(STAGES)).status_code == 403
        async with tenant_session(crm.client_id) as s:
            n = (await s.execute(text("SELECT count(*) FROM lead_pipeline_stages"))).scalar_one()
        assert n == 1
        async with _cliente(crm, "super_admin") as c:
            await c.put(url, json={"enabled": True})
        async with _cliente(crm, "admin") as c:
            assert await _slugs(c, crm) == ["new"]  # no pisa el pipeline que ya habia

    @pytest.mark.parametrize("rol", ["admin", "supervisor", "agent", "medical"])
    async def test_solo_el_super_admin_puede_activarlo(self, crm: Escenario, rol: str) -> None:
        url = f"/api/v1/platform/clients/{crm.client_id}/lead-management"
        async with _cliente(crm, rol) as c:
            assert (await c.put(url, json={"enabled": False})).status_code == 403

    async def test_un_cliente_inexistente_es_404(self, crm: Escenario) -> None:
        url = f"/api/v1/platform/clients/{uuid.uuid4()}/lead-management"
        async with _cliente(crm, "super_admin") as c:
            assert (await c.put(url, json={"enabled": True})).status_code == 404


# ── Etapas ──────────────────────────────────────────────────────────────────────────────────


class TestEtapas:
    async def test_crear_al_final_en_medio_y_con_hueco(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            for slug in ("a", "b", "c"):
                r = await c.post(STAGES, json={"name": slug.upper(), "slug": slug})
                assert r.status_code == 201, r.text
            assert await _slugs(c, crm) == ["a", "b", "c"]
            r = await c.post(STAGES, json={"name": "M", "slug": "m", "position": 1})
            assert r.json()["position"] == 1
            lista = (await c.get(STAGES)).json()
            assert [(e["slug"], e["position"]) for e in lista] == [
                ("a", 0),
                ("m", 1),
                ("b", 2),
                ("c", 3),
            ]
            hueco = await c.post(STAGES, json={"name": "Z", "slug": "z", "position": 9})
            assert hueco.status_code == 400

    async def test_el_slug_repetido_es_409_y_uno_invalido_422(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            await c.post(STAGES, json={"name": "A", "slug": "a"})
            dup = await c.post(STAGES, json={"name": "Otra", "slug": "a"})
            assert dup.status_code == 409
            assert dup.json()["error_code"] == "CONFLICT"
            assert (
                await c.post(STAGES, json={"name": "X", "slug": "Con Espacio"})
            ).status_code == 422
            assert len(await _slugs(c, crm)) == 1  # el duplicado no desplazo nada

    async def test_reordenar(self, crm: Escenario) -> None:
        ids = [await crear_etapa(crm.client_id, s, i) for i, s in enumerate(("a", "b", "c"))]
        async with _cliente(crm, "admin") as c:
            r = await c.put(
                f"{STAGES}/order", json={"stage_ids": [str(ids[2]), str(ids[0]), str(ids[1])]}
            )
            assert r.status_code == 200, r.text
            assert [(e["slug"], e["position"]) for e in r.json()] == [("c", 0), ("a", 1), ("b", 2)]
            assert await _slugs(c, crm) == ["c", "a", "b"]

    async def test_reordenar_exige_todas_las_etapas_y_solo_las_del_tenant(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        ids = [await crear_etapa(a.client_id, s, i) for i, s in enumerate(("a", "b"))]
        ajena = await crear_etapa(b.client_id, "ajena", 0)
        async with _cliente(a, "admin") as c:
            faltan = await c.put(f"{STAGES}/order", json={"stage_ids": [str(ids[0])]})
            sobra = await c.put(
                f"{STAGES}/order", json={"stage_ids": [str(ids[0]), str(ids[1]), str(ajena)]}
            )
            repetida = await c.put(
                f"{STAGES}/order", json={"stage_ids": [str(ids[0]), str(ids[0])]}
            )
            assert (faltan.status_code, sobra.status_code, repetida.status_code) == (400, 400, 422)
            assert await _slugs(c, a) == ["a", "b"]

    async def test_editar_una_etapa(self, crm: Escenario) -> None:
        etapa = await crear_etapa(crm.client_id, "a", 0, color="#112233")
        async with _cliente(crm, "admin") as c:
            r = await c.put(
                f"{STAGES}/{etapa}", json={"name": "Nueva", "color": None, "is_terminal": True}
            )
            assert r.status_code == 200, r.text
            assert (r.json()["name"], r.json()["color"], r.json()["is_terminal"]) == (
                "Nueva",
                None,
                True,
            )
            assert r.json()["slug"] == "a"  # el slug no cambia
            assert (await c.put(f"{STAGES}/{etapa}", json={})).status_code == 422
            assert (await c.put(f"{STAGES}/{etapa}", json={"color": "rojo"})).status_code == 422

    async def test_borrar_cierra_el_hueco_y_se_niega_si_hay_leads(self, crm: Escenario) -> None:
        a = await crear_etapa(crm.client_id, "a", 0)
        b = await crear_etapa(crm.client_id, "b", 1)
        await crear_etapa(crm.client_id, "c", 2)
        await crear_lead(crm.client_id, email="x@example.com", pipeline_stage_id=b)
        async with _cliente(crm, "admin") as c:
            ocupada = await c.delete(f"{STAGES}/{b}")
            assert ocupada.status_code == 409
            assert "1 lead" in ocupada.json()["message"]
            assert (await c.delete(f"{STAGES}/{a}")).status_code == 204
            lista = (await c.get(STAGES)).json()
            assert [(e["slug"], e["position"]) for e in lista] == [("b", 0), ("c", 1)]

    async def test_los_leads_con_soft_delete_tambien_impiden_borrar_la_etapa(
        self, crm: Escenario
    ) -> None:
        from datetime import datetime, timezone

        etapa = await crear_etapa(crm.client_id, "a", 0)
        await crear_lead(
            crm.client_id,
            email="x@example.com",
            pipeline_stage_id=etapa,
            deleted_at=datetime.now(timezone.utc),
        )
        async with _cliente(crm, "admin") as c:
            assert (await c.delete(f"{STAGES}/{etapa}")).status_code == 409

    @pytest.mark.parametrize("rol", ["supervisor", "agent"])
    async def test_los_roles_operativos_leen_pero_no_configuran(
        self, crm: Escenario, rol: str
    ) -> None:
        etapa = await crear_etapa(crm.client_id, "a", 0)
        async with _cliente(crm, rol) as c:
            assert (await c.get(STAGES)).status_code == 200
            assert (await c.post(STAGES, json={"name": "X", "slug": "x"})).status_code == 403
            assert (await c.put(f"{STAGES}/{etapa}", json={"name": "Y"})).status_code == 403
            assert (await c.delete(f"{STAGES}/{etapa}")).status_code == 403
            assert (
                await c.put(f"{STAGES}/order", json={"stage_ids": [str(etapa)]})
            ).status_code == 403

    async def test_cada_tenant_solo_ve_y_toca_sus_etapas(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        etapa_b = await crear_etapa(b.client_id, "b", 0)
        async with _cliente(a, "admin") as c:
            assert (await c.get(STAGES)).json() == []
            assert (await c.put(f"{STAGES}/{etapa_b}", json={"name": "X"})).status_code == 404
            assert (await c.delete(f"{STAGES}/{etapa_b}")).status_code == 404
            # Los mismos slugs y posiciones en otro tenant no chocan.
            assert (await c.post(STAGES, json={"name": "B", "slug": "b"})).status_code == 201


# ── Fuentes ─────────────────────────────────────────────────────────────────────────────────


async def _buscar_token(token: str) -> list[object]:
    async with AsyncSessionLocal() as s, s.begin():
        return list(
            (
                await s.execute(
                    text("SELECT * FROM public.capture_lookup_source(:h)"),
                    {"h": hash_capture_token(token)},
                )
            ).all()
        )


class TestFuentes:
    async def test_crear_sin_captura_no_genera_token(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            r = await c.post(SOURCES, json={"name": "Manual", "source_type": "manual"})
        assert r.status_code == 201, r.text
        cuerpo = r.json()
        assert cuerpo["capture_enabled"] is False
        assert cuerpo["capture_token"] is None
        assert "capture_token_hash" not in cuerpo

    async def test_el_token_se_ve_una_vez_y_en_la_base_solo_queda_su_hash(
        self, crm: Escenario
    ) -> None:
        async with _cliente(crm, "admin") as c:
            r = await c.post(
                SOURCES, json={"name": "Web", "source_type": "web_form", "enable_capture": True}
            )
            token = r.json()["capture_token"]
            fuente = r.json()["id"]
            assert token
            assert r.json()["capture_enabled"] is True
            for url in (f"{SOURCES}/{fuente}", SOURCES):
                texto = (await c.get(url)).text
                assert token not in texto
                assert "capture_token" not in texto.replace("capture_token_hash", "")
        async with tenant_session(crm.client_id) as s:
            guardado = (
                await s.execute(text("SELECT capture_token_hash FROM lead_sources"))
            ).scalar_one()
        assert guardado == hashlib.sha256(token.encode()).hexdigest()
        assert guardado != token
        (fila,) = await _buscar_token(token)
        assert str(fila.source_id) == fuente  # type: ignore[attr-defined]

    async def test_rotar_invalida_el_token_anterior(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            r = await c.post(
                SOURCES, json={"name": "Web", "source_type": "web_form", "enable_capture": True}
            )
            viejo, fuente = r.json()["capture_token"], r.json()["id"]
            nuevo = (await c.post(f"{SOURCES}/{fuente}/capture-token")).json()["capture_token"]
        assert nuevo != viejo
        assert await _buscar_token(viejo) == []
        assert len(await _buscar_token(nuevo)) == 1

    async def test_apagar_la_captura_invalida_el_token(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            r = await c.post(
                SOURCES, json={"name": "Web", "source_type": "web_form", "enable_capture": True}
            )
            token, fuente = r.json()["capture_token"], r.json()["id"]
            r = await c.delete(f"{SOURCES}/{fuente}/capture-token")
        assert r.json()["capture_enabled"] is False
        assert await _buscar_token(token) == []

    async def test_el_conteo_de_leads_ignora_los_borrados(self, crm: Escenario) -> None:
        from datetime import datetime, timezone

        fuente = await crear_fuente(crm.client_id, name="Web", source_type="web_form")
        await crear_lead(crm.client_id, email="a@example.com", source_id=fuente)
        await crear_lead(crm.client_id, email="b@example.com", source_id=fuente)
        await crear_lead(
            crm.client_id,
            email="c@example.com",
            source_id=fuente,
            deleted_at=datetime.now(timezone.utc),
        )
        async with _cliente(crm, "admin") as c:
            lista = (await c.get(SOURCES)).json()
            assert lista[0]["leads_count"] == 2
            assert (await c.get(f"{SOURCES}/{fuente}")).json()["leads_count"] == 2

    async def test_editar_y_desactivar(self, crm: Escenario) -> None:
        fuente = await crear_fuente(crm.client_id, name="Web", source_type="web_form")
        async with _cliente(crm, "admin") as c:
            r = await c.put(
                f"{SOURCES}/{fuente}",
                json={"name": "Landing", "is_active": False, "utm_tracking": {"utm_source": "g"}},
            )
            assert r.status_code == 200, r.text
            assert (r.json()["name"], r.json()["is_active"]) == ("Landing", False)
            assert r.json()["utm_tracking"] == {"utm_source": "g"}
            assert (await c.put(f"{SOURCES}/{fuente}", json={})).status_code == 422

    async def test_borrar_una_fuente_conserva_sus_leads(self, crm: Escenario) -> None:
        fuente = await crear_fuente(crm.client_id)
        lead = await crear_lead(crm.client_id, email="a@example.com", source_id=fuente)
        async with _cliente(crm, "admin") as c:
            assert (await c.delete(f"{SOURCES}/{fuente}")).status_code == 204
            assert (await c.get(f"{SOURCES}/{fuente}")).status_code == 404
        async with tenant_session(crm.client_id) as s:
            fila = (
                await s.execute(text("SELECT source_id FROM leads WHERE id = :i"), {"i": str(lead)})
            ).one()
        assert fila.source_id is None

    async def test_un_tipo_desconocido_es_422(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            r = await c.post(SOURCES, json={"name": "X", "source_type": "tiktok"})
        assert r.status_code == 422

    @pytest.mark.parametrize("rol", ["supervisor", "agent"])
    async def test_los_roles_operativos_leen_pero_no_configuran(
        self, crm: Escenario, rol: str
    ) -> None:
        fuente = await crear_fuente(crm.client_id)
        async with _cliente(crm, rol) as c:
            assert (await c.get(SOURCES)).status_code == 200
            assert (await c.get(f"{SOURCES}/{fuente}")).status_code == 200
            assert (
                await c.post(SOURCES, json={"name": "X", "source_type": "manual"})
            ).status_code == 403
            assert (await c.put(f"{SOURCES}/{fuente}", json={"name": "Y"})).status_code == 403
            assert (await c.delete(f"{SOURCES}/{fuente}")).status_code == 403
            assert (await c.post(f"{SOURCES}/{fuente}/capture-token")).status_code == 403
            assert (await c.delete(f"{SOURCES}/{fuente}/capture-token")).status_code == 403

    async def test_cada_tenant_solo_ve_y_toca_sus_fuentes(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        fuente_b = await crear_fuente(b.client_id, capture_token_hash="f" * 64)
        async with _cliente(a, "admin") as c:
            assert (await c.get(SOURCES)).json() == []
            for r in (
                await c.get(f"{SOURCES}/{fuente_b}"),
                await c.put(f"{SOURCES}/{fuente_b}", json={"name": "X"}),
                await c.delete(f"{SOURCES}/{fuente_b}"),
                await c.post(f"{SOURCES}/{fuente_b}/capture-token"),
                await c.delete(f"{SOURCES}/{fuente_b}/capture-token"),
            ):
                assert r.status_code == 404
        async with tenant_session(b.client_id) as s:
            assert (
                await s.execute(text("SELECT capture_token_hash FROM lead_sources"))
            ).scalar_one() == "f" * 64  # nadie lo roto desde A
