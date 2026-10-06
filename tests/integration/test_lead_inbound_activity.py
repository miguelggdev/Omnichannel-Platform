"""Los mensajes de un contacto enlazado a un lead cuentan como actividad del lead (Sprint 16).

Requiere base de datos: `pytest tests/ --run-db`. Se ejercita `_process_message()` del worker, como
`test_webhook_flow.py`. Lo que se prueba: que el lead suba `last_activity_at` (y nunca retroceda),
que el historial no se llene con un mensaje por fila, que un contacto sin lead no cueste nada y,
sobre todo, que **un fallo al anotar la actividad jamas cueste el mensaje del cliente**.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.database import tenant_session
from app.tasks.webhook_processor import _process_message
from tests.integration.lead_helpers import crear_lead, limpiar_leads

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("ia_encolada")]

BASE = datetime(2026, 9, 9, 20, 0, tzinfo=timezone.utc)
TELEFONO = "573001112233"


def _msg(n: int, minutos: float = 0, sender: str = TELEFONO) -> dict[str, Any]:
    return {
        "channel": "whatsapp",
        "sender_identifier": sender,
        "text": f"Hola {n}",
        "media_url": None,
        "media_type": None,
        "timestamp": (BASE + timedelta(minutes=minutos)).isoformat(),
        "external_message_id": f"wamid.LEAD{uuid.uuid4().hex[:10]}",
        "raw_payload": {"origen": "test"},
    }


@pytest_asyncio.fixture
async def contacto_lead(
    webhook_tenant: uuid.UUID,
) -> AsyncGenerator[tuple[uuid.UUID, uuid.UUID], None]:
    """Un contacto de WhatsApp (creado por un primer mensaje) enlazado a un lead."""
    await _process_message("ycloud", "whatsapp", _msg(0, minutos=-600))
    async with tenant_session(webhook_tenant) as s:
        contacto = (await s.execute(text("SELECT id FROM contacts"))).scalar_one()
    lead = await crear_lead(
        webhook_tenant, first_name="Ana", email="ana@example.com", contact_id=contacto,
        last_activity_at=BASE - timedelta(days=1),
    )  # fmt: skip
    async with tenant_session(webhook_tenant) as s:
        await s.execute(
            text("UPDATE contacts SET lead_id = :l, is_lead = true WHERE id = :c"),
            {"l": str(lead), "c": str(contacto)},
        )
    yield contacto, lead
    await limpiar_leads(webhook_tenant)


async def _lead(tenant: uuid.UUID, lead: uuid.UUID) -> Any:
    async with tenant_session(tenant) as s:
        return (
            await s.execute(
                text("SELECT last_activity_at FROM leads WHERE id = :i"), {"i": str(lead)}
            )
        ).one()


async def _actividades(tenant: uuid.UUID, lead: uuid.UUID) -> list[Any]:
    async with tenant_session(tenant) as s:
        return list(
            (
                await s.execute(
                    text(
                        "SELECT activity_type, user_id, metadata FROM lead_activities "
                        "WHERE lead_id = :l ORDER BY created_at"
                    ),
                    {"l": str(lead)},
                )
            ).all()
        )


async def _mensajes(tenant: uuid.UUID) -> int:
    async with tenant_session(tenant) as s:
        return int((await s.execute(text("SELECT count(*) FROM messages"))).scalar_one())


async def test_un_mensaje_del_contacto_sube_la_actividad_del_lead_y_deja_rastro(
    webhook_tenant: uuid.UUID, contacto_lead: tuple[uuid.UUID, uuid.UUID]
) -> None:
    _, lead = contacto_lead
    await _process_message("ycloud", "whatsapp", _msg(1))
    assert (await _lead(webhook_tenant, lead)).last_activity_at == BASE
    (act,) = await _actividades(webhook_tenant, lead)
    assert act.activity_type == "inbound_message"
    assert act.user_id is None
    assert act.metadata["channel"] == "whatsapp"
    assert "Hola" not in str(act.metadata)  # nunca el texto del mensaje


async def test_varios_mensajes_seguidos_son_una_sola_fila_y_pasada_media_hora_otra(
    webhook_tenant: uuid.UUID, contacto_lead: tuple[uuid.UUID, uuid.UUID]
) -> None:
    _, lead = contacto_lead
    for n, minutos in enumerate((0, 1, 5, 20)):
        await _process_message("ycloud", "whatsapp", _msg(n + 1, minutos))
    assert len(await _actividades(webhook_tenant, lead)) == 1
    # Pasa media hora (se envejece la fila): el siguiente mensaje anota una nueva.
    async with tenant_session(webhook_tenant) as s:
        await s.execute(
            text("UPDATE lead_activities SET created_at = created_at - interval '31 minutes'")
        )
    await _process_message("ycloud", "whatsapp", _msg(9, minutos=61))
    assert len(await _actividades(webhook_tenant, lead)) == 2
    assert (await _lead(webhook_tenant, lead)).last_activity_at == BASE + timedelta(minutes=61)


async def test_un_mensaje_reprocesado_con_fecha_vieja_no_atrasa_al_lead(
    webhook_tenant: uuid.UUID, contacto_lead: tuple[uuid.UUID, uuid.UUID]
) -> None:
    _, lead = contacto_lead
    await _process_message("ycloud", "whatsapp", _msg(1, minutos=30))
    await _process_message("ycloud", "whatsapp", _msg(2, minutos=-120))
    assert (await _lead(webhook_tenant, lead)).last_activity_at == BASE + timedelta(minutes=30)


async def test_un_contacto_que_no_es_lead_no_genera_nada(webhook_tenant: uuid.UUID) -> None:
    await _process_message("ycloud", "whatsapp", _msg(1, sender="573009998877"))
    async with tenant_session(webhook_tenant) as s:
        assert (await s.execute(text("SELECT count(*) FROM lead_activities"))).scalar_one() == 0
    assert await _mensajes(webhook_tenant) == 1


async def test_un_lead_borrado_no_se_toca_y_el_mensaje_se_guarda(
    webhook_tenant: uuid.UUID, contacto_lead: tuple[uuid.UUID, uuid.UUID]
) -> None:
    _, lead = contacto_lead
    antes = (await _lead(webhook_tenant, lead)).last_activity_at
    async with tenant_session(webhook_tenant) as s:
        await s.execute(text("UPDATE leads SET deleted_at = now() WHERE id = :i"), {"i": str(lead)})
    await _process_message("ycloud", "whatsapp", _msg(1))
    assert (await _lead(webhook_tenant, lead)).last_activity_at == antes
    assert await _actividades(webhook_tenant, lead) == []
    assert await _mensajes(webhook_tenant) == 2


async def test_si_anotar_la_actividad_falla_el_mensaje_del_cliente_se_guarda_igual(
    webhook_tenant: uuid.UUID,
    contacto_lead: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, lead = contacto_lead
    antes = await _mensajes(webhook_tenant)

    def roto(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("fallo inesperado al anotar")

    from app.services import lead_activity

    original = lead_activity.registrar_actividad
    monkeypatch.setattr("app.services.lead_activity.registrar_actividad", roto)
    await _process_message("ycloud", "whatsapp", _msg(1))  # no lanza
    assert await _mensajes(webhook_tenant) == antes + 1
    assert await _actividades(webhook_tenant, lead) == []
    # Y el siguiente mensaje, ya sin el fallo, funciona con normalidad.
    monkeypatch.setattr("app.services.lead_activity.registrar_actividad", original)
    await _process_message("ycloud", "whatsapp", _msg(2, minutos=1))
    assert len(await _actividades(webhook_tenant, lead)) == 1
