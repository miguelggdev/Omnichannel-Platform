# ruff: noqa: F811
"""Sprint 19, Dev B: analitica de leads, pipeline, ingresos y canales.

Requiere base de datos y Redis: `pytest tests/ --run-db`. Lo que un doble no prueba: que las
agregaciones SQL den los numeros que dan a mano, que las monedas no se mezclen, que el rango use el
dia local del negocio, que RLS aisle a los tenants y que el pre-calculo en Redis no cruce tenants.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from app.services import lead_analytics
from app.services.dedup import get_redis
from tests.integration.call_helpers import crear_deal_db, crear_llamada_db
from tests.integration.lead_helpers import (
    activar_modulo,
    crear_etapa,
    crear_fuente,
    crear_lead,
    limpiar_leads,
)
from tests.integration.sequence_helpers import sql, uno
from tests.integration.test_crm_api import Escenario, _cliente, dos_tenants, escenario  # noqa: F401

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

ANALYTICS = "/api/v1/analytics"
BOGOTA = ZoneInfo("America/Bogota")


def _hoy() -> date:
    return datetime.now(BOGOTA).date()


def _hace(dias: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=dias)


RANGO = {"date_from": (_hoy() - timedelta(days=10)).isoformat(), "date_to": _hoy().isoformat()}


async def _limpiar_cache() -> None:
    redis = get_redis()
    async for clave in redis.scan_iter("analytics:*"):
        await redis.delete(clave)
    async for clave in redis.scan_iter("analytics-rate:*"):
        await redis.delete(clave)


@pytest_asyncio.fixture
async def crm(escenario: Escenario) -> AsyncGenerator[Escenario, None]:
    await activar_modulo(escenario.client_id)
    await _limpiar_cache()
    yield escenario
    await limpiar_leads(escenario.client_id)
    await _limpiar_cache()


@pytest_asyncio.fixture
async def crm_dos(
    dos_tenants: tuple[Escenario, Escenario],
) -> AsyncGenerator[tuple[Escenario, Escenario], None]:
    for e in dos_tenants:
        await activar_modulo(e.client_id)
    await _limpiar_cache()
    yield dos_tenants
    for e in dos_tenants:
        await limpiar_leads(e.client_id)
    await _limpiar_cache()


async def _get(crm: Escenario, ruta: str, rol: str = "supervisor", **params: Any) -> Any:
    async with _cliente(crm, rol) as c:
        return await c.get(f"{ANALYTICS}/{ruta}", params=params)


# ── Datos de prueba ──────────────────────────────────────────────────────────────────────────


async def _sembrar_leads(crm: Escenario) -> None:
    cid = crm.client_id
    nuevo = await crear_etapa(cid, "new", 0)
    calificado = await crear_etapa(cid, "qualified", 1)
    ganado = await crear_etapa(cid, "won", 2, is_terminal=True)
    web = await crear_fuente(cid, name="Web", source_type="web_form")
    referido = await crear_fuente(cid, name="Referidos", source_type="referral")

    async def lead(
        nombre: str,
        *,
        fuente: uuid.UUID | None,
        etapa: uuid.UUID | None,
        estado: str,
        temperatura: str,
        score: int,
        asignado: uuid.UUID | None = None,
        hace: float = 3,
    ) -> uuid.UUID:
        lead_id = await crear_lead(
            cid,
            first_name=nombre,
            email=f"{nombre.lower()}@x.com",
            source_id=fuente,
            pipeline_stage_id=etapa,
            assigned_user_id=asignado,
        )
        await sql(
            cid,
            "UPDATE leads SET status = :s, temperature = :t, total_score = :p, created_at = :c WHERE id = :i",
            s=estado,
            t=temperatura,
            p=score,
            c=_hace(hace),
            i=str(lead_id),
        )
        return lead_id

    await lead(
        "L1",
        fuente=web,
        etapa=ganado,
        estado="won",
        temperatura="hot",
        score=80,
        asignado=crm.user_id,
    )
    await lead("L2", fuente=web, etapa=calificado, estado="active", temperatura="warm", score=50)
    await lead("L3", fuente=referido, etapa=nuevo, estado="active", temperatura="cold", score=20)
    await lead("L4", fuente=referido, etapa=nuevo, estado="lost", temperatura="cold", score=10)
    await lead("L5", fuente=None, etapa=None, estado="active", temperatura="cold", score=30)
    await lead(
        "L6", fuente=web, etapa=nuevo, estado="won", temperatura="hot", score=99, hace=60
    )  # fuera del rango
    borrado = await lead(
        "L7", fuente=web, etapa=nuevo, estado="active", temperatura="hot", score=99
    )
    await sql(cid, "UPDATE leads SET deleted_at = now() WHERE id = :i", i=str(borrado))


async def _sembrar_deals(crm: Escenario) -> dict[str, uuid.UUID]:
    cid = crm.client_id
    lead = await crear_lead(cid, first_name="Deals", email="deals@x.com")
    ahora = datetime.now(timezone.utc)

    async def abierto(
        titulo: str, etapa: str, valor: int, prob: int, moneda: str, dias_en_etapa: float = 1
    ) -> uuid.UUID:
        d = await crear_deal_db(
            cid,
            lead,
            title=titulo,
            stage=etapa,
            value=Decimal(valor),
            probability=prob,
            currency=moneda,
        )
        await sql(
            cid,
            "UPDATE deals SET stage_changed_at = :t WHERE id = :i",
            t=_hace(dias_en_etapa),
            i=str(d),
        )
        return d

    async def ganado(
        titulo: str,
        valor: int,
        moneda: str,
        cerrado_hace: int,
        ciclo: int,
        usuario: uuid.UUID | None,
    ) -> uuid.UUID:
        cierre = _hace(cerrado_hace)
        return await crear_deal_db(
            cid,
            lead,
            title=titulo,
            stage="closed_won",
            value=Decimal(valor),
            probability=100,
            currency=moneda,
            assigned_user_id=usuario,
            created_at=cierre - timedelta(days=ciclo),
            won_at=cierre,
            actual_close_date=cierre.astimezone(BOGOTA).date(),
        )

    ids = {
        "A": await abierto("A", "proposal", 1000, 50, "USD"),
        "B": await abierto("B", "proposal", 3000, 50, "USD", dias_en_etapa=30),
        "C": await abierto("C", "proposal", 8_000_000, 25, "COP"),
        "D": await abierto("D", "new_contact", 500, 10, "USD", dias_en_etapa=20),
        "W1": await ganado("W1", 2000, "USD", 5, 10, crm.user_id),
        "W2": await ganado("W2", 1000, "USD", 3, 6, None),
        "WC": await ganado("WC", 5_000_000, "COP", 4, 4, crm.user_id),
    }
    perdido_en = _hace(2)
    ids["X"] = await crear_deal_db(
        cid,
        lead,
        title="X",
        stage="closed_lost",
        value=Decimal(700),
        probability=0,
        currency="USD",
        lost_at=perdido_en,
        actual_close_date=perdido_en.astimezone(BOGOTA).date(),
        created_at=ahora - timedelta(days=9),
    )
    return ids


async def _actividad(
    crm: Escenario, lead: uuid.UUID, tipo: str, canal: str | None, hace: float = 1
) -> None:
    meta = f'{{"channel": "{canal}"}}' if canal else "{}"
    await sql(
        crm.client_id,
        "INSERT INTO lead_activities (client_id, lead_id, activity_type, metadata, created_at) "
        "VALUES (:c, :l, :t, CAST(:m AS jsonb), :f)",
        c=str(crm.client_id),
        l=str(lead),
        t=tipo,
        m=meta,
        f=_hace(hace),
    )


async def _sembrar_canales(crm: Escenario) -> None:
    lead = await crear_lead(crm.client_id, first_name="Canales", email="canales@x.com")
    cuentas = [
        ("sequence_message_sent", "email", 3),
        ("sequence_message_sent", "whatsapp", 1),
        ("sequence_reply", "whatsapp", 1),
        ("sequence_reply", "email", 1),
        ("message_sent", "email", 2),
        ("call_reminder_sent", "email", 4),
        ("inbound_message", "whatsapp", 2),
    ]
    for tipo, canal, n in cuentas:
        for _ in range(n):
            await _actividad(crm, lead, tipo, canal)
    await _actividad(crm, lead, "sequence_message_sent", "email", hace=60)  # fuera del rango
    await _actividad(crm, lead, "sequence_message_sent", None)  # sin canal
    await _actividad(crm, lead, "created", "email")  # otro tipo de actividad

    ia = {"call_type": "ai_voice", "ai_voice_provider": "fakevoice"}
    for estado, extra, tipo in (
        ("completed", {}, {}),
        ("completed", {}, {}),
        ("no_show", {}, {}),
        ("cancelled", {}, {}),
        ("pending", {}, {}),
        ("rescheduled", {}, {}),
        ("pending", {}, ia),
    ):
        await crear_llamada_db(
            crm.client_id,
            lead,
            status=estado,
            scheduled_at=_hace(2),
            assigned_user_id=crm.user_id if not tipo else None,
            **({"completed_at": _hace(2)} if estado == "completed" else {}),
            **({"cancelled_at": _hace(2)} if estado == "cancelled" else {}),
            **extra,
            **tipo,
        )


# ── Leads ────────────────────────────────────────────────────────────────────────────────────


class TestLeads:
    async def test_la_cohorte_del_rango_a_mano(self, crm: Escenario) -> None:
        await _sembrar_leads(crm)

        r = await _get(crm, "leads", **RANGO)

        assert r.status_code == 200, r.text
        d = r.json()
        assert d["created"] == 5  # L6 fuera del rango y L7 borrado no cuentan
        assert d["unassigned"] == 4
        assert d["avg_total_score"] == 38.0  # (80 + 50 + 20 + 10 + 30) / 5
        assert {x["key"]: x["count"] for x in d["by_status"]} == {"active": 3, "lost": 1, "won": 1}
        assert {x["key"]: x["count"] for x in d["by_temperature"]} == {
            "cold": 3,
            "warm": 1,
            "hot": 1,
        }
        assert d["conversion_rate"] == 20.0
        assert d["cached"] is False

    async def test_por_fuente_con_la_conversion_de_cada_una(self, crm: Escenario) -> None:
        await _sembrar_leads(crm)

        d = (await _get(crm, "leads", **RANGO)).json()

        assert d["by_source"] == [
            {"source_type": "referral", "created": 2, "won": 0, "conversion_rate": 0.0},
            {"source_type": "web_form", "created": 2, "won": 1, "conversion_rate": 50.0},
            {"source_type": "unknown", "created": 1, "won": 0, "conversion_rate": 0.0},
        ]

    async def test_por_etapa_en_el_orden_del_pipeline(self, crm: Escenario) -> None:
        await _sembrar_leads(crm)

        d = (await _get(crm, "leads", **RANGO)).json()

        assert [(e["slug"], e["count"]) for e in d["by_stage"]] == [
            ("new", 2),
            ("qualified", 1),
            ("won", 1),
        ]

    async def test_sin_leads_las_tasas_son_nulas_y_no_cero(self, crm: Escenario) -> None:
        d = (await _get(crm, "leads", **RANGO)).json()
        assert (d["created"], d["conversion_rate"], d["avg_total_score"]) == (0, None, None)
        assert d["by_source"] == []

    async def test_el_rango_usa_el_dia_local_del_negocio(self, crm: Escenario) -> None:
        """Un lead creado a las 22:00 en Bogota ya es del dia siguiente en UTC."""
        hoy = _hoy()
        a_las_22_locales = datetime.combine(hoy, datetime.min.time(), tzinfo=BOGOTA).replace(
            hour=22
        )
        lead = await crear_lead(crm.client_id, first_name="Noche", email="noche@x.com")
        await sql(
            crm.client_id,
            "UPDATE leads SET created_at = :c WHERE id = :i",
            c=a_las_22_locales,
            i=str(lead),
        )

        hoy_solo = (
            await _get(crm, "leads", date_from=hoy.isoformat(), date_to=hoy.isoformat())
        ).json()
        ayer_solo = (
            await _get(
                crm,
                "leads",
                date_from=(hoy - timedelta(days=1)).isoformat(),
                date_to=(hoy - timedelta(days=1)).isoformat(),
            )
        ).json()

        assert hoy_solo["created"] == 1
        assert ayer_solo["created"] == 0
        assert hoy_solo["range"]["timezone"] == "America/Bogota"

    async def test_por_defecto_son_los_ultimos_30_dias(self, crm: Escenario) -> None:
        d = (await _get(crm, "leads")).json()
        assert d["range"]["date_to"] == _hoy().isoformat()
        assert d["range"]["date_from"] == (_hoy() - timedelta(days=29)).isoformat()


# ── Pipeline ─────────────────────────────────────────────────────────────────────────────────


class TestPipeline:
    async def test_a_mano(self, crm: Escenario) -> None:
        await _sembrar_deals(crm)

        d = (await _get(crm, "pipeline", **RANGO)).json()

        abiertos = {(f["stage"], f["currency"]): f for f in d["open_by_stage"]}
        assert set(abiertos) == {("new_contact", "USD"), ("proposal", "USD"), ("proposal", "COP")}
        assert Decimal(abiertos[("proposal", "USD")]["total_value"]) == 4000
        assert Decimal(abiertos[("proposal", "USD")]["weighted_value"]) == 2000
        # Lo previsto va por moneda: 500 + 1500 + 50 en USD, 2.000.000 en COP.
        previsto = {f["currency"]: Decimal(f["value"]) for f in d["weighted_forecast"]}
        assert previsto == {"COP": Decimal(2_000_000), "USD": Decimal(2050)}
        assert d["win_rates"] == [
            {"currency": "COP", "won": 1, "lost": 0, "win_rate": 100.0},
            {"currency": "USD", "won": 2, "lost": 1, "win_rate": 66.7},
        ]
        assert d["cycle_times"] == [
            {"currency": "COP", "won": 1, "avg_days": 4.0},
            {"currency": "USD", "won": 2, "avg_days": 8.0},  # (10 + 6) / 2
        ]

    async def test_atascados_por_moneda(self, crm: Escenario) -> None:
        await _sembrar_deals(crm)

        d = (await _get(crm, "pipeline", **RANGO)).json()

        assert d["stalled_days"] == 14
        assert d["stalled"] == [{"currency": "USD", "count": 2, "total_value": "3500.00"}]  # B y D

    async def test_stalled_days_a_medida(self, crm: Escenario) -> None:
        await _sembrar_deals(crm)

        d = (await _get(crm, "pipeline", stalled_days=25, **RANGO)).json()
        todos = (await _get(crm, "pipeline", stalled_days=1, **RANGO)).json()
        ninguno = (await _get(crm, "pipeline", stalled_days=365, **RANGO)).json()

        assert d["stalled"] == [{"currency": "USD", "count": 1, "total_value": "3000.00"}]  # solo B
        assert sum(s["count"] for s in todos["stalled"]) == 4
        assert ninguno["stalled"] == []
        assert d["cached"] is False

    async def test_los_cierres_fuera_del_rango_no_cuentan(self, crm: Escenario) -> None:
        await _sembrar_deals(crm)
        d = (
            await _get(
                crm,
                "pipeline",
                date_from=(_hoy() - timedelta(days=400)).isoformat(),
                date_to=(_hoy() - timedelta(days=300)).isoformat(),
            )
        ).json()
        assert d["win_rates"] == []
        assert d["cycle_times"] == []
        assert len(d["open_by_stage"]) == 3  # los abiertos son los de ahora, no dependen del rango

    async def test_sin_cierres_no_hay_tasa(self, crm: Escenario) -> None:
        lead = await crear_lead(crm.client_id, first_name="A", email="a@x.com")
        await crear_deal_db(crm.client_id, lead, stage="proposal")
        d = (await _get(crm, "pipeline", **RANGO)).json()
        assert d["win_rates"] == []
        assert d["weighted_forecast"] != []


# ── Ingresos ─────────────────────────────────────────────────────────────────────────────────


class TestIngresos:
    async def test_totales_por_moneda(self, crm: Escenario) -> None:
        await _sembrar_deals(crm)

        d = (await _get(crm, "revenue", **RANGO)).json()

        assert d["totals"] == [
            {"currency": "COP", "deals": 1, "revenue": "5000000.00", "avg_deal_size": "5000000.00"},
            {"currency": "USD", "deals": 2, "revenue": "3000.00", "avg_deal_size": "1500.00"},
        ]

    async def test_por_mes_suma_lo_mismo_que_el_total(self, crm: Escenario) -> None:
        await _sembrar_deals(crm)

        d = (await _get(crm, "revenue", **RANGO)).json()

        por_moneda: dict[str, Decimal] = {}
        for m in d["by_month"]:
            por_moneda[m["currency"]] = por_moneda.get(m["currency"], Decimal(0)) + Decimal(
                m["revenue"]
            )
        assert por_moneda == {"COP": Decimal(5_000_000), "USD": Decimal(3000)}
        assert all(len(m["month"]) == 7 for m in d["by_month"])

    async def test_por_responsable_sin_mezclar_monedas(self, crm: Escenario) -> None:
        await _sembrar_deals(crm)
        nombre = (
            await uno(
                crm.client_id, "SELECT first_name FROM users WHERE id = :u", u=str(crm.user_id)
            )
        )[0]

        d = (await _get(crm, "revenue", **RANGO)).json()

        assert d["by_user"] == [
            {
                "user_id": str(crm.user_id),
                "first_name": nombre,
                "currency": "COP",
                "deals": 1,
                "revenue": "5000000.00",
            },
            {
                "user_id": str(crm.user_id),
                "first_name": nombre,
                "currency": "USD",
                "deals": 1,
                "revenue": "2000.00",
            },
            {
                "user_id": None,
                "first_name": None,
                "currency": "USD",
                "deals": 1,
                "revenue": "1000.00",
            },
        ]

    async def test_lo_perdido_y_lo_abierto_no_es_ingreso(self, crm: Escenario) -> None:
        lead = await crear_lead(crm.client_id, first_name="A", email="a@x.com")
        await crear_deal_db(crm.client_id, lead, stage="proposal", value=Decimal(999))
        d = (await _get(crm, "revenue", **RANGO)).json()
        assert d["totals"] == []
        assert d["by_month"] == []

    async def test_un_ganado_fuera_del_rango_no_cuenta(self, crm: Escenario) -> None:
        await _sembrar_deals(crm)
        d = (
            await _get(
                crm,
                "revenue",
                date_from=(_hoy() - timedelta(days=100)).isoformat(),
                date_to=(_hoy() - timedelta(days=50)).isoformat(),
            )
        ).json()
        assert d["totals"] == []


# ── Canales ──────────────────────────────────────────────────────────────────────────────────


class TestCanales:
    async def test_a_mano(self, crm: Escenario) -> None:
        await _sembrar_canales(crm)

        d = (await _get(crm, "channels", **RANGO)).json()

        assert d["channels"] == [
            {
                "channel": "email",
                "sequence_sent": 3,
                "sequence_replies": 1,
                "reply_rate": 33.3,
                "manual_sent": 2,
                "reminders_sent": 4,
                "inbound_messages": 0,
            },
            {
                "channel": "whatsapp",
                "sequence_sent": 1,
                "sequence_replies": 1,
                "reply_rate": 100.0,
                "manual_sent": 0,
                "reminders_sent": 0,
                "inbound_messages": 2,
            },
        ]

    async def test_llamadas_por_tipo_sin_contar_las_reprogramadas(self, crm: Escenario) -> None:
        await _sembrar_canales(crm)

        d = (await _get(crm, "channels", **RANGO)).json()

        assert d["calls"] == [
            {
                "call_type": "ai_voice",
                "scheduled": 1,
                "completed": 0,
                "no_show": 0,
                "cancelled": 0,
                "completion_rate": 0.0,
            },
            # 5 = 2 completadas + 1 no-show + 1 cancelada + 1 pendiente; la reprogramada se cuenta por la nueva.
            {
                "call_type": "human",
                "scheduled": 5,
                "completed": 2,
                "no_show": 1,
                "cancelled": 1,
                "completion_rate": 50.0,
            },  # 2 / (5 - 1 cancelada)
        ]

    async def test_sin_envios_no_hay_tasa_de_respuesta(self, crm: Escenario) -> None:
        lead = await crear_lead(crm.client_id, first_name="A", email="a@x.com")
        await _actividad(crm, lead, "inbound_message", "whatsapp")
        d = (await _get(crm, "channels", **RANGO)).json()
        assert d["channels"][0]["reply_rate"] is None
        assert d["calls"] == []


# ── Pre-calculo ──────────────────────────────────────────────────────────────────────────────


class TestPrecalculo:
    async def test_el_precalculo_se_sirve_con_cached_y_su_hora(self, crm: Escenario) -> None:
        await _sembrar_leads(crm)
        guardadas = await lead_analytics.precalcular_tenant(crm.client_id)

        d = (await _get(crm, "leads")).json()

        assert guardadas == 4
        assert d["cached"] is True
        assert d["computed_at"] is not None
        assert d["created"] == 5

    @pytest.mark.parametrize("ruta", ["leads", "pipeline", "revenue", "channels"])
    async def test_las_cuatro_se_precalculan(self, crm: Escenario, ruta: str) -> None:
        await lead_analytics.precalcular_tenant(crm.client_id)
        assert (await _get(crm, ruta)).json()["cached"] is True

    async def test_un_rango_propio_se_calcula_en_vivo(self, crm: Escenario) -> None:
        await _sembrar_leads(crm)
        await lead_analytics.precalcular_tenant(crm.client_id)

        d = (await _get(crm, "leads", **RANGO)).json()

        assert d["cached"] is False

    async def test_sin_precalculo_calcula_y_guarda_para_la_siguiente(self, crm: Escenario) -> None:
        primera = (await _get(crm, "leads")).json()
        segunda = (await _get(crm, "leads")).json()
        assert (primera["cached"], segunda["cached"]) == (False, True)

    async def test_el_precalculo_no_se_cruza_entre_tenants(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        await _sembrar_leads(a)
        await lead_analytics.precalcular_tenant(a.client_id)

        de_a = (await _get(a, "leads")).json()
        de_b = (await _get(b, "leads")).json()

        assert (de_a["created"], de_a["cached"]) == (5, True)
        assert de_b["created"] == 0
        assert de_b["cached"] is False

    async def test_con_redis_caido_se_calcula_en_vivo(
        self, crm: Escenario, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _sembrar_leads(crm)

        def roto() -> Any:
            raise ConnectionError("redis caido")

        monkeypatch.setattr(lead_analytics, "get_redis", roto)
        r = await _get(crm, "leads")

        # Solo se rompe el cache de la analitica (el limite de peticiones usa su propio cliente).
        assert r.status_code == 200
        assert r.json()["created"] == 5
        assert r.json()["cached"] is False

    async def test_precalcular_un_tenant_vacio_funciona(self, crm: Escenario) -> None:
        assert await lead_analytics.precalcular_tenant(crm.client_id) == 4

    async def test_la_tarea_diaria(self, crm: Escenario, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.tasks import analytics_tasks

        async def solo_este() -> list[uuid.UUID]:
            return [crm.client_id]

        monkeypatch.setattr(analytics_tasks, "_load_active_client_ids", solo_este)

        resultado = await analytics_tasks.precalcular_todos()

        assert resultado == {"skipped": False, "tenants": 1, "computed": 4}
        assert (await _get(crm, "pipeline")).json()["cached"] is True

    async def test_la_tarea_no_toca_tenants_sin_el_modulo(
        self, crm: Escenario, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.tasks import analytics_tasks

        await activar_modulo(crm.client_id, False)

        async def solo_este() -> list[uuid.UUID]:
            return [crm.client_id]

        monkeypatch.setattr(analytics_tasks, "_load_active_client_ids", solo_este)

        assert (await analytics_tasks.precalcular_todos())["tenants"] == 0

    async def test_una_pasada_a_la_vez(self) -> None:
        from app.tasks import analytics_tasks

        redis = get_redis()
        await redis.set(analytics_tasks.CLAVE_PASADA, "1", ex=60)
        try:
            assert await analytics_tasks.precalcular_todos() == {"skipped": True}
        finally:
            await redis.delete(analytics_tasks.CLAVE_PASADA)

    async def test_registro_en_beat(self) -> None:
        from app.tasks import analytics_tasks  # noqa: F401
        from app.tasks.celery_app import celery_app

        entrada = celery_app.conf.beat_schedule["precompute-lead-analytics"]
        assert entrada["task"] == "app.tasks.bulk_precompute_lead_analytics"
        assert entrada["options"]["queue"] == "bulk"
        assert "app.tasks.bulk_precompute_lead_analytics" in celery_app.tasks
        ruta = celery_app.amqp.router.route({}, "app.tasks.bulk_precompute_lead_analytics")
        assert ruta["queue"].name == "bulk"


# ── Validacion, permisos y aislamiento ───────────────────────────────────────────────────────


class TestAcceso:
    @pytest.mark.parametrize("ruta", ["leads", "pipeline", "revenue", "channels"])
    async def test_un_agente_no_ve_las_cifras_del_negocio(self, crm: Escenario, ruta: str) -> None:
        assert (await _get(crm, ruta, rol="agent")).status_code == 403

    @pytest.mark.parametrize("rol", ["supervisor", "admin"])
    async def test_supervisor_y_admin_si(self, crm: Escenario, rol: str) -> None:
        assert (await _get(crm, "leads", rol=rol)).status_code == 200

    async def test_modulo_apagado(self, crm: Escenario) -> None:
        await activar_modulo(crm.client_id, False)
        assert (await _get(crm, "leads")).status_code == 403

    @pytest.mark.parametrize("ruta", ["leads", "pipeline", "revenue", "channels"])
    async def test_las_dos_fechas_juntas_o_ninguna(self, crm: Escenario, ruta: str) -> None:
        r = await _get(crm, ruta, date_from="2026-10-01")
        assert r.status_code == 400

    @pytest.mark.parametrize("ruta", ["leads", "pipeline", "revenue", "channels"])
    async def test_orden_y_tamano_del_rango(self, crm: Escenario, ruta: str) -> None:
        al_reves = await _get(crm, ruta, date_from="2026-10-10", date_to="2026-10-01")
        enorme = await _get(crm, ruta, date_from="2020-01-01", date_to="2026-10-01")
        assert (al_reves.status_code, enorme.status_code) == (400, 400)

    async def test_fechas_mal_formadas_y_stalled_days_fuera_de_rango(self, crm: Escenario) -> None:
        mala = await _get(crm, "leads", date_from="ayer", date_to="hoy")
        cero = await _get(crm, "pipeline", stalled_days=0)
        enorme = await _get(crm, "pipeline", stalled_days=366)
        assert (mala.status_code, cero.status_code, enorme.status_code) == (422, 422, 422)

    async def test_stalled_days_a_medida_tambien_valida_el_rango(self, crm: Escenario) -> None:
        r = await _get(
            crm, "pipeline", stalled_days=20, date_from="2026-10-10", date_to="2026-10-01"
        )
        assert r.status_code == 400

    async def test_limite_de_peticiones_por_usuario_y_minuto(
        self, crm: Escenario, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.api.v1 import lead_analytics as analytics

        monkeypatch.setattr(analytics, "PETICIONES_POR_MINUTO", 3)
        estados = [(await _get(crm, "leads")).status_code for _ in range(4)]
        assert estados == [200, 200, 200, 429]

    async def test_un_tenant_no_ve_las_cifras_de_otro(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        await _sembrar_leads(a)
        await _sembrar_deals(a)
        await _sembrar_canales(a)

        for ruta in ("leads", "pipeline", "revenue", "channels"):
            d = (await _get(b, ruta, **RANGO)).json()
            vacio = {
                "leads": d.get("created") == 0,
                "pipeline": d.get("open_by_stage") == [] and d.get("win_rates") == [],
                "revenue": d.get("totals") == [],
                "channels": d.get("channels") == [] and d.get("calls") == [],
            }[ruta]
            assert vacio, ruta
