# ruff: noqa: F811
"""Sprint 19, Dev B: API de deals, pipeline por moneda y alerta de deal ganado.

Requiere base de datos y Redis: `pytest tests/ --run-db`. Lo que un doble no prueba: RLS real entre
tenants, la sincronizacion deal <-> lead en la base, que el pipeline no mezcle monedas y que la
alerta salga una sola vez y despues del commit.
"""

import json
import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

from app.core.config import get_settings
from app.services import deal_alerts
from app.services.dedup import get_redis
from tests.integration.call_helpers import crear_deal_db
from tests.integration.lead_helpers import activar_modulo, crear_lead, limpiar_leads
from tests.integration.sequence_helpers import sql, uno, usuario
from tests.integration.test_crm_api import Escenario, _cliente, dos_tenants, escenario  # noqa: F401

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

DEALS = "/api/v1/deals"
CONFIG = "/api/v1/clients/lead-automation"


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


async def _lead(crm: Escenario, **campos: Any) -> uuid.UUID:
    return await crear_lead(
        crm.client_id, **{"first_name": "Ana", "email": f"{uuid.uuid4().hex[:8]}@x.com", **campos}
    )


def _cuerpo(lead: uuid.UUID, **extra: Any) -> dict[str, Any]:
    return {
        "lead_id": str(lead),
        "title": "Plan anual",
        "value": "1500.00",
        "currency": "USD",
        **extra,
    }


# ── CRUD ─────────────────────────────────────────────────────────────────────────────────────


class TestCrud:
    async def test_crear_y_leer(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        async with _cliente(crm, "agent") as c:
            r = await c.post(DEALS, json=_cuerpo(lead, expected_close_date="2026-12-01"))
            assert r.status_code == 201, r.text
            deal = r.json()
            detalle = (await c.get(f"{DEALS}/{deal['id']}")).json()

        assert deal["stage"] == "new_contact"
        assert deal["probability"] == 10  # la de la etapa
        assert deal["currency"] == "USD"
        assert Decimal(deal["value"]) == Decimal("1500.00")
        assert detalle == deal
        # Queda en el historial del lead.
        assert await uno(
            crm.client_id,
            "SELECT count(*) FROM lead_activities WHERE lead_id = :l AND activity_type = 'deal_created'",
            l=str(lead),
        ) == (1,)

    async def test_el_responsable_por_defecto_es_el_del_lead(self, crm: Escenario) -> None:
        lead = await _lead(crm, assigned_user_id=crm.user_id)
        async with _cliente(crm, "admin") as c:
            r = await c.post(DEALS, json=_cuerpo(lead))
        assert r.json()["assigned_user_id"] == str(crm.user_id)

    async def test_un_deal_no_nace_cerrado(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        async with _cliente(crm, "admin") as c:
            r = await c.post(DEALS, json=_cuerpo(lead, stage="closed_won"))
        assert r.status_code == 422

    @pytest.mark.parametrize(
        "malo",
        [
            {"currency": "DOLARES"},
            {"currency": "U$"},
            {"value": "-1"},
            {"value": "10000000000000"},
            {"title": ""},
            {"probability": 101},
            {"stage": "inexistente"},
            {"extra": 1},
        ],
    )
    async def test_cuerpos_invalidos(self, crm: Escenario, malo: dict[str, Any]) -> None:
        lead = await _lead(crm)
        async with _cliente(crm, "admin") as c:
            r = await c.post(DEALS, json=_cuerpo(lead, **malo))
        assert r.status_code == 422

    async def test_lead_inexistente_o_borrado_es_404(self, crm: Escenario) -> None:
        borrado = await _lead(crm)
        await sql(
            crm.client_id, "UPDATE leads SET deleted_at = now() WHERE id = :l", l=str(borrado)
        )
        async with _cliente(crm, "admin") as c:
            sin_lead = await c.post(DEALS, json=_cuerpo(uuid.uuid4()))
            lead_borrado = await c.post(DEALS, json=_cuerpo(borrado))
        assert (sin_lead.status_code, lead_borrado.status_code) == (404, 404)

    async def test_responsable_de_otro_tenant_es_400(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        lead = await _lead(a)
        async with _cliente(a, "admin") as c:
            r = await c.post(DEALS, json=_cuerpo(lead, assigned_user_id=str(b.user_id)))
        assert r.status_code == 400

    async def test_editar(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        deal = await crear_deal_db(crm.client_id, lead, assigned_user_id=crm.user_id)
        async with _cliente(crm, "supervisor") as c:
            r = await c.put(
                f"{DEALS}/{deal}",
                json={"title": "Plan bianual", "value": "3000", "currency": "eur"},
            )
        assert r.status_code == 200, r.text
        assert (r.json()["title"], r.json()["currency"]) == ("Plan bianual", "EUR")
        assert Decimal(r.json()["value"]) == Decimal(3000)

    async def test_editar_no_cambia_la_etapa(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        deal = await crear_deal_db(crm.client_id, lead)
        async with _cliente(crm, "admin") as c:
            r = await c.put(f"{DEALS}/{deal}", json={"stage": "closed_won"})
        assert r.status_code == 422

    async def test_listar_filtra_y_pagina(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        otro = await _lead(crm)
        for i in range(5):
            await crear_deal_db(
                crm.client_id,
                lead,
                title=f"D{i}",
                currency="USD" if i < 3 else "COP",
                value=Decimal(i + 1),
            )
        await crear_deal_db(crm.client_id, otro, title="Otro", stage="proposal")
        async with _cliente(crm, "agent") as c:
            todos = (await c.get(DEALS)).json()
            pag = (await c.get(DEALS, params={"page_size": 2, "page": 3})).json()
            usd = (await c.get(DEALS, params={"currency": "usd"})).json()
            del_lead = (await c.get(DEALS, params={"lead_id": str(otro)})).json()
            etapa = (await c.get(DEALS, params={"stage": "proposal"})).json()
            malo = await c.get(DEALS, params={"page_size": 101})

        assert todos["total"] == 6
        assert (pag["total_pages"], len(pag["items"])) == (3, 2)
        assert usd["total"] == 4  # tres D0-D2 y el de otro lead, en USD por defecto
        assert [d["title"] for d in del_lead["items"]] == ["Otro"]
        assert [d["title"] for d in etapa["items"]] == ["Otro"]
        assert malo.status_code == 422

    async def test_borrar_solo_los_abiertos(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        abierto = await crear_deal_db(crm.client_id, lead)
        ganado = await crear_deal_db(crm.client_id, lead)
        async with _cliente(crm, "admin") as c:
            await c.patch(f"{DEALS}/{ganado}/stage", json={"stage": "closed_won"})
            ok = await c.delete(f"{DEALS}/{abierto}")
            cerrado = await c.delete(f"{DEALS}/{ganado}")
            otra_vez = await c.get(f"{DEALS}/{abierto}")

        assert ok.status_code == 204
        assert cerrado.status_code == 409  # la historia de ingresos no se reescribe
        assert otra_vez.status_code == 404

    async def test_un_agente_no_borra(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        deal = await crear_deal_db(crm.client_id, lead)
        async with _cliente(crm, "agent") as c:
            r = await c.delete(f"{DEALS}/{deal}")
        assert r.status_code == 403

    async def test_modulo_apagado(self, crm: Escenario) -> None:
        await activar_modulo(crm.client_id, False)
        async with _cliente(crm, "admin") as c:
            assert (await c.get(DEALS)).status_code == 403
            assert (await c.get(f"{DEALS}/pipeline")).status_code == 403


# ── Permisos del agente ──────────────────────────────────────────────────────────────────────


class TestAgente:
    async def test_solo_actua_sobre_deals_suyos_o_sin_responsable(self, crm: Escenario) -> None:
        otro = await usuario(crm.client_id)
        lead = await _lead(crm)
        suyo = await crear_deal_db(crm.client_id, lead, assigned_user_id=crm.user_id)
        libre = await crear_deal_db(crm.client_id, lead)
        ajeno = await crear_deal_db(crm.client_id, lead, assigned_user_id=otro)
        async with _cliente(crm, "agent") as c:
            put = [
                (await c.put(f"{DEALS}/{d}", json={"notes": "x"})).status_code
                for d in (suyo, libre, ajeno)
            ]
            etapa = (
                await c.patch(f"{DEALS}/{ajeno}/stage", json={"stage": "qualified"})
            ).status_code

        assert put == [200, 200, 403]
        assert etapa == 403

    async def test_no_abre_deals_sobre_leads_de_otro_ni_para_otro(self, crm: Escenario) -> None:
        otro = await usuario(crm.client_id)
        ajeno = await _lead(crm, assigned_user_id=otro)
        libre = await _lead(crm)
        async with _cliente(crm, "agent") as c:
            sobre_ajeno = await c.post(DEALS, json=_cuerpo(ajeno))
            para_otro = await c.post(DEALS, json=_cuerpo(libre, assigned_user_id=str(otro)))
            para_si = await c.post(DEALS, json=_cuerpo(libre, assigned_user_id=str(crm.user_id)))

        assert (sobre_ajeno.status_code, para_otro.status_code, para_si.status_code) == (
            403,
            403,
            201,
        )

    async def test_no_reasigna_a_otro_pero_un_supervisor_si(self, crm: Escenario) -> None:
        otro = await usuario(crm.client_id)
        lead = await _lead(crm)
        deal = await crear_deal_db(crm.client_id, lead, assigned_user_id=crm.user_id)
        async with _cliente(crm, "agent") as c:
            agente = await c.put(f"{DEALS}/{deal}", json={"assigned_user_id": str(otro)})
        async with _cliente(crm, "supervisor") as c:
            supervisor = await c.put(f"{DEALS}/{deal}", json={"assigned_user_id": str(otro)})

        assert agente.status_code == 403
        assert supervisor.status_code == 200


# ── Etapas y sincronizacion con el lead ──────────────────────────────────────────────────────


class TestEtapas:
    async def test_ganar_convierte_al_lead(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        deal = await crear_deal_db(crm.client_id, lead)
        async with _cliente(crm, "agent") as c:
            r = await c.patch(f"{DEALS}/{deal}/stage", json={"stage": "closed_won"})

        assert r.status_code == 200, r.text
        assert (r.json()["stage"], r.json()["probability"]) == ("closed_won", 100)
        assert r.json()["won_at"] is not None
        assert r.json()["actual_close_date"] is not None
        estado, convertido = await uno(
            crm.client_id, "SELECT status, converted_at FROM leads WHERE id = :l", l=str(lead)
        )
        assert estado == "won"
        assert convertido is not None
        assert await uno(
            crm.client_id,
            "SELECT count(*) FROM lead_activities WHERE lead_id = :l AND activity_type = 'deal_stage_changed'",
            l=str(lead),
        ) == (1,)

    async def test_perder_guarda_el_motivo_y_no_toca_al_lead(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        deal = await crear_deal_db(crm.client_id, lead)
        async with _cliente(crm, "admin") as c:
            r = await c.patch(
                f"{DEALS}/{deal}/stage", json={"stage": "closed_lost", "lost_reason": "Precio"}
            )

        assert (r.json()["stage"], r.json()["probability"], r.json()["lost_reason"]) == (
            "closed_lost",
            0,
            "Precio",
        )
        assert (await uno(crm.client_id, "SELECT status FROM leads WHERE id = :l", l=str(lead)))[
            0
        ] == "active"

    async def test_reabrir_el_unico_ganado_devuelve_el_lead_a_activo(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        deal = await crear_deal_db(crm.client_id, lead)
        async with _cliente(crm, "admin") as c:
            await c.patch(f"{DEALS}/{deal}/stage", json={"stage": "closed_won"})
            r = await c.patch(f"{DEALS}/{deal}/stage", json={"stage": "negotiation"})

        assert r.json()["stage"] == "negotiation"
        assert (await uno(crm.client_id, "SELECT status FROM leads WHERE id = :l", l=str(lead)))[
            0
        ] == "active"

    async def test_con_otro_deal_ganado_el_lead_sigue_ganado(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        uno_, dos = (
            await crear_deal_db(crm.client_id, lead),
            await crear_deal_db(crm.client_id, lead),
        )
        async with _cliente(crm, "admin") as c:
            await c.patch(f"{DEALS}/{uno_}/stage", json={"stage": "closed_won"})
            await c.patch(f"{DEALS}/{dos}/stage", json={"stage": "closed_won"})
            await c.patch(f"{DEALS}/{uno_}/stage", json={"stage": "proposal"})

        assert (await uno(crm.client_id, "SELECT status FROM leads WHERE id = :l", l=str(lead)))[
            0
        ] == "won"

    @pytest.mark.parametrize(
        ("origen", "destino", "codigo"),
        [
            ("proposal", "proposal", 409),  # misma etapa
            ("closed_won", "closed_lost", 409),  # de cerrado se sale reabriendo
            ("closed_lost", "closed_won", 409),
        ],
    )
    async def test_transiciones_invalidas(
        self, crm: Escenario, origen: str, destino: str, codigo: int
    ) -> None:
        lead = await _lead(crm)
        deal = await crear_deal_db(crm.client_id, lead)
        async with _cliente(crm, "admin") as c:
            if origen.startswith("closed"):
                await c.patch(f"{DEALS}/{deal}/stage", json={"stage": origen})
            else:
                await c.patch(f"{DEALS}/{deal}/stage", json={"stage": origen})
            r = await c.patch(f"{DEALS}/{deal}/stage", json={"stage": destino})
        assert r.status_code == codigo

    @pytest.mark.parametrize(
        "cuerpo",
        [
            {"stage": "closed_won", "probability": 50},
            {"stage": "proposal", "lost_reason": "x"},
            {"stage": "inexistente"},
            {"stage": "proposal", "probability": 150},
        ],
    )
    async def test_cuerpos_invalidos(self, crm: Escenario, cuerpo: dict[str, Any]) -> None:
        lead = await _lead(crm)
        deal = await crear_deal_db(crm.client_id, lead)
        async with _cliente(crm, "admin") as c:
            r = await c.patch(f"{DEALS}/{deal}/stage", json=cuerpo)
        assert r.status_code == 422

    async def test_probabilidad_explicita_en_una_etapa_abierta(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        deal = await crear_deal_db(crm.client_id, lead)
        async with _cliente(crm, "admin") as c:
            r = await c.patch(
                f"{DEALS}/{deal}/stage", json={"stage": "proposal", "probability": 60}
            )
        assert r.json()["probability"] == 60

    async def test_un_lead_cerrado_a_mano_no_resucita(self, crm: Escenario) -> None:
        lead = await _lead(crm, status="lost")
        deal = await crear_deal_db(crm.client_id, lead)
        async with _cliente(crm, "admin") as c:
            await c.patch(f"{DEALS}/{deal}/stage", json={"stage": "closed_won"})
        assert (await uno(crm.client_id, "SELECT status FROM leads WHERE id = :l", l=str(lead)))[
            0
        ] == "lost"


# ── Pipeline ─────────────────────────────────────────────────────────────────────────────────


class TestPipeline:
    async def test_nunca_mezcla_monedas(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        await crear_deal_db(
            crm.client_id,
            lead,
            stage="proposal",
            value=Decimal(1000),
            probability=50,
            currency="USD",
        )
        await crear_deal_db(
            crm.client_id,
            lead,
            stage="proposal",
            value=Decimal(3000),
            probability=50,
            currency="USD",
        )
        await crear_deal_db(
            crm.client_id,
            lead,
            stage="proposal",
            value=Decimal(8000000),
            probability=25,
            currency="COP",
        )
        await crear_deal_db(
            crm.client_id, lead, stage="qualified", value=Decimal(500), currency="USD"
        )
        async with _cliente(crm, "agent") as c:
            r = await c.get(f"{DEALS}/pipeline")

        filas = {(f["stage"], f["currency"]): f for f in r.json()}
        assert set(filas) == {("qualified", "USD"), ("proposal", "COP"), ("proposal", "USD")}
        usd = filas[("proposal", "USD")]
        assert (usd["count"], Decimal(usd["total_value"]), Decimal(usd["weighted_value"])) == (
            2,
            Decimal(4000),
            Decimal(2000),
        )
        cop = filas[("proposal", "COP")]
        assert (cop["count"], Decimal(cop["total_value"]), Decimal(cop["weighted_value"])) == (
            1,
            Decimal(8000000),
            Decimal(2000000),
        )

    async def test_va_en_el_orden_del_embudo_y_solo_abiertos_por_defecto(
        self, crm: Escenario
    ) -> None:
        lead = await _lead(crm)
        for etapa in ("negotiation", "new_contact", "proposal"):
            await crear_deal_db(crm.client_id, lead, stage=etapa)
        ganado = await crear_deal_db(crm.client_id, lead)
        async with _cliente(crm, "admin") as c:
            await c.patch(f"{DEALS}/{ganado}/stage", json={"stage": "closed_won"})
            abiertos = (await c.get(f"{DEALS}/pipeline")).json()
            todos = (await c.get(f"{DEALS}/pipeline", params={"include_closed": "true"})).json()

        assert [f["stage"] for f in abiertos] == ["new_contact", "proposal", "negotiation"]
        assert [f["stage"] for f in todos][-1] == "closed_won"

    async def test_dias_en_etapa(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        deal = await crear_deal_db(crm.client_id, lead, stage="proposal")
        await sql(
            crm.client_id,
            "UPDATE deals SET stage_changed_at = now() - interval '10 days' WHERE id = :d",
            d=str(deal),
        )
        async with _cliente(crm, "agent") as c:
            fila = (await c.get(f"{DEALS}/pipeline")).json()[0]
        assert 9.9 <= fila["avg_days_in_stage"] <= 10.1

    async def test_pipeline_vacio(self, crm: Escenario) -> None:
        async with _cliente(crm, "agent") as c:
            assert (await c.get(f"{DEALS}/pipeline")).json() == []


# ── Aislamiento entre tenants ────────────────────────────────────────────────────────────────


class TestAislamiento:
    async def test_un_tenant_no_ve_ni_toca_los_deals_de_otro(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        lead = await _lead(a)
        deal = await crear_deal_db(a.client_id, lead, stage="proposal")
        async with _cliente(b, "admin") as c:
            lista = (await c.get(DEALS)).json()
            pipeline = (await c.get(f"{DEALS}/pipeline")).json()
            respuestas = [
                await c.get(f"{DEALS}/{deal}"),
                await c.put(f"{DEALS}/{deal}", json={"title": "x"}),
                await c.patch(f"{DEALS}/{deal}/stage", json={"stage": "closed_won"}),
                await c.delete(f"{DEALS}/{deal}"),
                await c.post(DEALS, json=_cuerpo(lead)),
            ]

        assert lista["total"] == 0
        assert pipeline == []
        assert [r.status_code for r in respuestas] == [404, 404, 404, 404, 404]
        assert (await uno(a.client_id, "SELECT stage FROM deals WHERE id = :d", d=str(deal)))[
            0
        ] == "proposal"


# ── Alerta de deal ganado ────────────────────────────────────────────────────────────────────


class TelegramFalso:
    """Bot de Telegram de mentira."""

    enviados: list[tuple[str, str]] = []  # noqa: RUF012
    falla = False

    async def send_message(self, to: str, content: Any, channel_config: dict[str, Any]) -> str:
        if TelegramFalso.falla:
            raise RuntimeError("telegram caido")
        assert channel_config == {"bot_token": "token-de-prueba"}
        TelegramFalso.enviados.append((to, content.text))
        return "1"


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch) -> type[TelegramFalso]:
    TelegramFalso.enviados = []
    TelegramFalso.falla = False
    monkeypatch.setattr(deal_alerts, "TelegramProvider", TelegramFalso)
    monkeypatch.setattr(get_settings(), "TELEGRAM_CHANNEL_BOT_TOKEN", "token-de-prueba")
    return TelegramFalso


async def _alertas(crm: Escenario, **extra: Any) -> None:
    config = {
        "deal_alerts": {
            "enabled": True,
            "telegram_chat_id": "-1001234567890",
            "thresholds": {"USD": "5000", "COP": "20000000"},
            **extra,
        }
    }
    async with _cliente(crm, "admin") as c:
        r = await c.put(CONFIG, json=config)
    assert r.status_code == 200, r.text


@pytest_asyncio.fixture(autouse=True)
async def _sin_marcas_de_alerta() -> AsyncGenerator[None, None]:
    yield
    redis = get_redis()
    async for clave in redis.scan_iter("deal-alert:*"):
        await redis.delete(clave)


class TestAlerta:
    async def test_la_configuracion_se_guarda_y_se_lee(self, crm: Escenario) -> None:
        await _alertas(crm)
        async with _cliente(crm, "supervisor") as c:
            r = await c.get(CONFIG)
        assert r.json()["deal_alerts"]["thresholds"] == {"USD": "5000", "COP": "20000000"}
        assert r.json()["deal_alerts"]["telegram_chat_id"] == "-1001234567890"

    async def test_configuracion_invalida_es_422(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            r = await c.put(
                CONFIG, json={"deal_alerts": {"enabled": True, "thresholds": {"USD": "5000"}}}
            )
        assert r.status_code == 422

    async def test_sale_al_ganar_un_deal_grande_con_titulo_importe_y_responsable(
        self, crm: Escenario, telegram: type[TelegramFalso]
    ) -> None:
        await _alertas(crm)
        lead = await _lead(crm)
        deal = await crear_deal_db(
            crm.client_id,
            lead,
            title="Plan anual",
            value=Decimal("12500"),
            assigned_user_id=crm.user_id,
        )
        async with _cliente(crm, "agent") as c:
            r = await c.patch(f"{DEALS}/{deal}/stage", json={"stage": "closed_won"})

        assert r.status_code == 200
        assert len(telegram.enviados) == 1
        chat, texto = telegram.enviados[0]
        assert chat == "-1001234567890"
        assert "Plan anual" in texto
        assert "12,500.00 USD" in texto
        assert "Responsable:" in texto

    @pytest.mark.parametrize(
        ("valor", "moneda"),
        [("4999.99", "USD"), ("5000", "COP"), ("999999", "EUR")],
    )
    async def test_no_sale_bajo_el_umbral_de_su_moneda(
        self, crm: Escenario, telegram: type[TelegramFalso], valor: str, moneda: str
    ) -> None:
        await _alertas(crm)
        lead = await _lead(crm)
        deal = await crear_deal_db(crm.client_id, lead, value=Decimal(valor), currency=moneda)
        async with _cliente(crm, "admin") as c:
            await c.patch(f"{DEALS}/{deal}/stage", json={"stage": "closed_won"})
        assert telegram.enviados == []

    async def test_sale_en_el_umbral_exacto(
        self, crm: Escenario, telegram: type[TelegramFalso]
    ) -> None:
        await _alertas(crm)
        lead = await _lead(crm)
        deal = await crear_deal_db(crm.client_id, lead, value=Decimal(5000))
        async with _cliente(crm, "admin") as c:
            await c.patch(f"{DEALS}/{deal}/stage", json={"stage": "closed_won"})
        assert len(telegram.enviados) == 1

    async def test_apagada_o_sin_configurar_no_sale(
        self, crm: Escenario, telegram: type[TelegramFalso]
    ) -> None:
        lead = await _lead(crm)
        sin_config = await crear_deal_db(crm.client_id, lead, value=Decimal(10**6))
        async with _cliente(crm, "admin") as c:
            await c.patch(f"{DEALS}/{sin_config}/stage", json={"stage": "closed_won"})
            await c.put(
                CONFIG,
                json={
                    "deal_alerts": {
                        "enabled": False,
                        "telegram_chat_id": "123",
                        "thresholds": {"USD": "1"},
                    }
                },
            )
            apagada = await crear_deal_db(crm.client_id, lead, value=Decimal(10**6))
            await c.patch(f"{DEALS}/{apagada}/stage", json={"stage": "closed_won"})
        assert telegram.enviados == []

    async def test_otro_tenant_no_dispara_la_alerta_de_este(
        self, crm_dos: tuple[Escenario, Escenario], telegram: type[TelegramFalso]
    ) -> None:
        a, b = crm_dos
        await _alertas(a)
        lead = await _lead(b)
        deal = await crear_deal_db(b.client_id, lead, value=Decimal(10**6))
        async with _cliente(b, "admin") as c:
            await c.patch(f"{DEALS}/{deal}/stage", json={"stage": "closed_won"})
        assert telegram.enviados == []

    async def test_un_deal_abierto_no_alerta_aunque_supere_el_umbral(
        self, crm: Escenario, telegram: type[TelegramFalso]
    ) -> None:
        """La alerta es de deals ganados: llamarla sobre uno abierto o perdido no envia nada."""
        await _alertas(crm)
        lead = await _lead(crm)
        abierto = await crear_deal_db(
            crm.client_id, lead, stage="negotiation", value=Decimal(10**6)
        )
        async with _cliente(crm, "admin") as c:
            await c.patch(
                f"{DEALS}/{abierto}/stage", json={"stage": "closed_lost", "lost_reason": "Precio"}
            )
        perdido = abierto

        assert await deal_alerts.alertar_deal_ganado(crm.client_id, perdido) is False
        nuevo = await crear_deal_db(crm.client_id, lead, stage="proposal", value=Decimal(10**6))
        assert await deal_alerts.alertar_deal_ganado(crm.client_id, nuevo) is False
        assert await deal_alerts.alertar_deal_ganado(crm.client_id, uuid.uuid4()) is False
        assert telegram.enviados == []

    async def test_una_sola_vez_por_deal(
        self, crm: Escenario, telegram: type[TelegramFalso]
    ) -> None:
        """Reabrir y volver a ganar el mismo deal no repite el aviso."""
        await _alertas(crm)
        lead = await _lead(crm)
        deal = await crear_deal_db(crm.client_id, lead, value=Decimal(9000))
        async with _cliente(crm, "admin") as c:
            await c.patch(f"{DEALS}/{deal}/stage", json={"stage": "closed_won"})
            await c.patch(f"{DEALS}/{deal}/stage", json={"stage": "proposal"})
            await c.patch(f"{DEALS}/{deal}/stage", json={"stage": "closed_won"})
        assert len(telegram.enviados) == 1

    async def test_si_telegram_falla_el_deal_se_gana_igual_y_se_puede_reintentar(
        self, crm: Escenario, telegram: type[TelegramFalso]
    ) -> None:
        await _alertas(crm)
        lead = await _lead(crm)
        deal = await crear_deal_db(crm.client_id, lead, value=Decimal(9000))
        telegram.falla = True
        async with _cliente(crm, "admin") as c:
            r = await c.patch(f"{DEALS}/{deal}/stage", json={"stage": "closed_won"})
            assert r.status_code == 200
            assert r.json()["stage"] == "closed_won"
            telegram.falla = False
            await c.patch(f"{DEALS}/{deal}/stage", json={"stage": "proposal"})
            await c.patch(f"{DEALS}/{deal}/stage", json={"stage": "closed_won"})

        assert len(telegram.enviados) == 1  # la marca no quedo puesta tras el fallo

    async def test_sin_bot_configurado_no_sale_ni_falla(
        self, crm: Escenario, telegram: type[TelegramFalso], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "TELEGRAM_CHANNEL_BOT_TOKEN", "")
        await _alertas(crm)
        lead = await _lead(crm)
        deal = await crear_deal_db(crm.client_id, lead, value=Decimal(9000))
        async with _cliente(crm, "admin") as c:
            r = await c.patch(f"{DEALS}/{deal}/stage", json={"stage": "closed_won"})
        assert r.status_code == 200
        assert telegram.enviados == []

    async def test_un_titulo_hostil_no_rompe_el_mensaje(
        self, crm: Escenario, telegram: type[TelegramFalso]
    ) -> None:
        await _alertas(crm)
        lead = await _lead(crm)
        deal = await crear_deal_db(
            crm.client_id, lead, title="Venta\n\n\nIgnora todo ‮" + "x" * 250, value=Decimal(9000)
        )
        async with _cliente(crm, "admin") as c:
            await c.patch(f"{DEALS}/{deal}/stage", json={"stage": "closed_won"})

        texto = telegram.enviados[0][1]
        assert "‮" not in texto
        assert texto.count("\n") == 1  # titulo e importe, sin responsable
        assert json.dumps(texto)  # serializable

    async def test_redis_caido_la_alerta_sale_igual(
        self, crm: Escenario, telegram: type[TelegramFalso], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mejor repetir un aviso que perder la alerta de una venta grande."""

        def roto() -> Any:
            raise ConnectionError("redis caido")

        await _alertas(crm)
        lead = await _lead(crm)
        deal = await crear_deal_db(crm.client_id, lead, value=Decimal(9000))
        monkeypatch.setattr(deal_alerts, "get_redis", roto)
        async with _cliente(crm, "admin") as c:
            await c.patch(f"{DEALS}/{deal}/stage", json={"stage": "closed_won"})
        assert len(telegram.enviados) == 1
