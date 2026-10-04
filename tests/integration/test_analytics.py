# ruff: noqa: F811
"""Analitica del dashboard contra PostgreSQL real con RLS (Sprint 15, fase 2).

Requiere base de datos: `pytest tests/ --run-db`. Los dobles no pueden probar nada de esto:
son consultas SQL con ventanas, FILTER y generate_series, y los resultados dependen de las
marcas de tiempo exactas. Los datos se siembran con tiempos relativos a `now()` para que el
test no dependa de la hora a la que corre (salvo los de "por dia", que fijan dias enteros).
"""

import re
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text

from app.core.database import tenant_session
from tests.integration.test_crm_api import (  # noqa: F401  (los fixtures se registran por nombre)
    Escenario,
    _cliente,
    dos_tenants,
    escenario,
)

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

DASHBOARD = "/api/v1/analytics/dashboard"
POR_CANAL = "/api/v1/analytics/conversations-by-channel"
POR_DIA = "/api/v1/analytics/messages-over-time"

_MSG = (
    "INSERT INTO messages (client_id, conversation_id, direction, message_type, content, "
    "sender_type, created_at) VALUES (:cid, :conv, :dir, 'text', 'x', :snd, "
    "now() + CAST(:delta AS interval))"
)


def _d(texto: str) -> timedelta:
    """'-2 hours + 10 seconds' -> timedelta. asyncpg exige un timedelta para el tipo interval."""
    total = timedelta()
    for parte in texto.split(" + "):
        cantidad, unidad = re.fullmatch(
            r"(-?\d+) (second|minute|hour|day)s?", parte.strip()
        ).groups()  # type: ignore[union-attr]
        total += timedelta(**{unidad + "s": int(cantidad)})
    return total


async def _sql(esc: Escenario, sql: str, **params: Any) -> None:
    async with tenant_session(esc.client_id) as s:
        await s.execute(text(sql), {"cid": str(esc.client_id), **params})


async def _mensajes(esc: Escenario, conv: uuid.UUID, filas: list[tuple[str, str]]) -> None:
    """Inserta `(direccion, desplazamiento)`; el desplazamiento es un intervalo ('-2 hours')."""
    for direccion, delta in filas:
        await _sql(
            esc,
            _MSG,
            conv=str(conv),
            dir=direccion,
            snd="contact" if direccion == "inbound" else "bot",
            delta=_d(delta),
        )


async def _limpiar_presupuesto_y_encuestas(esc: Escenario) -> None:
    """`_limpiar` del escenario no conoce estas tablas y sus FK impiden borrar el tenant."""
    await _sql(esc, "DELETE FROM satisfaction_surveys WHERE client_id = :cid")
    await _sql(esc, "DELETE FROM token_budgets WHERE client_id = :cid")


async def _otra_conversacion(
    esc: Escenario, canal: str, estado: str, hace: str = "0 seconds"
) -> uuid.UUID:
    conv = uuid.uuid4()
    await _sql(
        esc,
        "INSERT INTO conversations (id, client_id, contact_id, channel, status, created_at, "
        "last_message_at) VALUES (:id, :cid, :contact, :canal, :estado, "
        "now() - CAST(:hace AS interval), now() - CAST(:hace AS interval))",
        id=str(conv),
        contact=str(esc.contact_id),
        canal=canal,
        estado=estado,
        hace=_d(hace),
    )
    return conv


async def test_un_tenant_vacio_devuelve_ceros_y_nulos_no_errores(escenario: Escenario) -> None:
    async with _cliente(escenario, role="admin") as c:
        r = await c.get(DASHBOARD)

    assert r.status_code == 200, r.text
    d = r.json()
    # El escenario trae una conversacion bot_active y un contacto.
    assert d["active_conversations"] == 1
    assert d["total_contacts"] == 1
    assert d["messages_last_24h"] == 0
    for nulo in (
        "messages_trend",
        "conversations_trend",
        "token_usage_percentage",
        "avg_response_time_seconds",
        "median_response_time_seconds",
        "csat_score",
    ):
        assert d[nulo] is None, nulo
    assert d["csat_responses"] == 0


async def test_cifras_de_conversaciones_y_mensajes(escenario: Escenario) -> None:
    conv = escenario.conversation_id
    espera = await _otra_conversacion(escenario, "instagram", "waiting_human")
    await _otra_conversacion(escenario, "telegram", "resolved")  # no es "abierta"
    await _otra_conversacion(escenario, "whatsapp", "bot_active", hace="10 days")  # periodo previo
    # 24h actuales: 3 mensajes. 24h previas: 1 mensaje.
    await _mensajes(escenario, conv, [("inbound", "-3 hours"), ("outbound", "-2 hours")])
    await _mensajes(escenario, espera, [("inbound", "-1 hour"), ("inbound", "-30 hours")])

    async with _cliente(escenario, role="supervisor") as c:
        d = (await c.get(DASHBOARD)).json()

    assert d["active_conversations"] == 3  # bot_active, waiting_human y la de hace 10 dias
    assert d["waiting_human"] == 1
    assert d["conversations_last_7_days"] == 3  # la del escenario, instagram y la resuelta
    assert d["conversations_trend"] == 200.0  # 3 ahora vs 1 hace 14-7 dias
    assert d["messages_last_24h"] == 3
    assert d["messages_trend"] == 200.0  # 3 vs 1


async def test_el_tiempo_de_respuesta_mide_la_primera_respuesta_de_cada_tanda(
    escenario: Escenario,
) -> None:
    conv = escenario.conversation_id
    await _mensajes(
        escenario,
        conv,
        [
            # Tanda A: contesta a los 10 s.
            ("inbound", "-2 hours"),
            ("outbound", "-2 hours + 10 seconds"),
            # Tanda B: dos mensajes seguidos del contacto; la primera respuesta llega a los
            # 30 s del primero. El segundo (+5 s) no abre otra tanda.
            ("inbound", "-1 hour"),
            ("inbound", "-1 hour + 5 seconds"),
            ("outbound", "-1 hour + 30 seconds"),
            # Un entrante sin respuesta todavia: no cuenta.
            ("inbound", "-10 minutes"),
        ],
    )

    async with _cliente(escenario, role="admin") as c:
        d = (await c.get(DASHBOARD)).json()

    assert d["avg_response_time_seconds"] == 20.0
    assert d["median_response_time_seconds"] == 20.0


async def test_la_mediana_resiste_una_espera_larga_que_dispara_la_media(
    escenario: Escenario,
) -> None:
    conv = escenario.conversation_id
    filas: list[tuple[str, str]] = []
    for hora in (5, 4, 3):  # tres respuestas de 10 s
        filas += [("inbound", f"-{hora} hours"), ("outbound", f"-{hora} hours + 10 seconds")]
    filas += [("inbound", "-2 days"), ("outbound", "-2 days + 1 hour")]  # una de 3600 s
    await _mensajes(escenario, conv, filas)

    async with _cliente(escenario, role="admin") as c:
        d = (await c.get(DASHBOARD)).json()

    assert d["median_response_time_seconds"] == 10.0
    assert d["avg_response_time_seconds"] == 907.5  # (3*10 + 3600) / 4


async def test_presupuesto_de_tokens_y_csat(escenario: Escenario) -> None:
    await _sql(
        escenario,
        "INSERT INTO token_budgets (client_id, month, total_budget, used_tokens) "
        "VALUES (:cid, to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM'), 200000, 50000)",
    )
    for rating, hace in ((5, "1 day"), (4, "2 days"), (3, "40 days")):
        # Una encuesta por conversacion (indice unico).
        conv = await _otra_conversacion(escenario, "whatsapp", "resolved")
        await _sql(
            escenario,
            "INSERT INTO satisfaction_surveys (client_id, conversation_id, contact_id, channel, "
            "rating, sent_at, responded_at, status) VALUES (:cid, :conv, :contact, 'whatsapp', "
            ":rating, now() - CAST(:hace AS interval), now() - CAST(:hace AS interval), 'responded')",
            conv=str(conv),
            contact=str(escenario.contact_id),
            rating=rating,
            hace=_d(hace),
        )

    try:
        async with _cliente(escenario, role="admin") as c:
            d = (await c.get(DASHBOARD)).json()
    finally:
        await _limpiar_presupuesto_y_encuestas(escenario)

    assert d["token_usage_percentage"] == 25.0
    # La de hace 40 dias queda fuera de la ventana de 30.
    assert d["csat_score"] == 4.5
    assert d["csat_responses"] == 2


async def test_un_presupuesto_ilimitado_no_da_porcentaje(escenario: Escenario) -> None:
    await _sql(
        escenario,
        "INSERT INTO token_budgets (client_id, month, total_budget, used_tokens) "
        "VALUES (:cid, to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM'), 0, 999)",
    )

    try:
        async with _cliente(escenario, role="admin") as c:
            d = (await c.get(DASHBOARD)).json()
    finally:
        await _limpiar_presupuesto_y_encuestas(escenario)

    assert d["token_usage_percentage"] is None


async def test_conversaciones_por_canal_ordenadas_y_con_ventana(escenario: Escenario) -> None:
    await _otra_conversacion(escenario, "instagram", "bot_active")
    await _otra_conversacion(escenario, "instagram", "bot_active", hace="2 days")
    await _otra_conversacion(escenario, "telegram", "bot_active", hace="60 days")

    async with _cliente(escenario, role="admin") as c:
        en_30 = (await c.get(POR_CANAL)).json()
        en_90 = (await c.get(POR_CANAL, params={"days": 90})).json()

    assert en_30 == [
        {"channel": "instagram", "count": 2},
        {"channel": "whatsapp", "count": 1},
    ]
    assert {x["channel"]: x["count"] for x in en_90} == {
        "instagram": 2,
        "whatsapp": 1,
        "telegram": 1,
    }


async def test_mensajes_por_dia_rellena_los_dias_vacios_con_ceros(escenario: Escenario) -> None:
    conv = escenario.conversation_id
    for direccion, dias in (("inbound", 1), ("inbound", 1), ("outbound", 1), ("inbound", 3)):
        await _sql(
            escenario,
            "INSERT INTO messages (client_id, conversation_id, direction, message_type, content, "
            "sender_type, created_at) VALUES (:cid, :conv, :dir, 'text', 'x', 'contact', "
            "(date_trunc('day', now() AT TIME ZONE 'UTC') - CAST(:d AS int) * interval '1 day' "
            "+ interval '12 hours') AT TIME ZONE 'UTC')",
            conv=str(conv),
            dir=direccion,
            d=dias,
        )

    async with _cliente(escenario, role="admin") as c:
        r = await c.get(POR_DIA, params={"days": 4})

    filas = r.json()
    assert len(filas) == 4
    assert [(f["inbound"], f["outbound"]) for f in filas] == [
        (1, 0),  # hace 3 dias
        (0, 0),  # hace 2 dias: vacio, pero aparece
        (2, 1),  # ayer
        (0, 0),  # hoy
    ]
    fechas = [f["date"] for f in filas]
    assert fechas == sorted(fechas)


async def test_los_parametros_fuera_de_rango_son_422(escenario: Escenario) -> None:
    async with _cliente(escenario, role="admin") as c:
        assert (await c.get(POR_DIA, params={"days": 0})).status_code == 422
        assert (await c.get(POR_DIA, params={"days": 91})).status_code == 422
        assert (await c.get(POR_CANAL, params={"days": 366})).status_code == 422


async def test_un_agent_no_ve_la_analitica(escenario: Escenario) -> None:
    async with _cliente(escenario, role="agent") as c:
        for ruta in (DASHBOARD, POR_CANAL, POR_DIA):
            assert (await c.get(ruta)).status_code == 403, ruta


async def test_cada_tenant_ve_solo_lo_suyo(dos_tenants: tuple[Escenario, Escenario]) -> None:
    a, b = dos_tenants
    await _mensajes(a, a.conversation_id, [("inbound", "-1 hour")] * 5)

    async with _cliente(b, role="admin") as c:
        d = (await c.get(DASHBOARD)).json()
        por_dia = (await c.get(POR_DIA)).json()

    assert d["messages_last_24h"] == 0
    assert sum(f["inbound"] for f in por_dia) == 0
