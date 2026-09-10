"""Flujo completo de un webhook entrante contra PostgreSQL real.

webhook normalizado -> contacto -> conversacion -> mensaje -> webhook_dedup,
para los tres canales del MVP (WhatsApp via YCloud, Instagram DM y Facebook
Messenger via Meta).

Requiere base de datos: `pytest tests/ --run-db`. Sin la opcion, el marker `db` los
omite (ver `pytest_collection_modifyitems` en conftest.py).

Se ejercita `_process_message()` del worker, no el endpoint HTTP: el endpoint ya
esta cubierto en `tests/unit/test_webhooks.py` con dobles, y aqui lo que interesa es
lo que queda escrito en la base con el contexto de tenant aplicado (SET LOCAL).
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.database import AsyncSessionLocal, tenant_session
from app.tasks.webhook_processor import _process_message

pytestmark = pytest.mark.db


# ─── Datos de entrada ────────────────────────────────────────────────────────


def _normalized(
    channel: str,
    sender: str,
    external_id: str,
    text_body: str = "Hola",
    **extra: Any,
) -> dict[str, Any]:
    """Construye un NormalizedMessage serializado como lo envia el endpoint."""
    payload = {
        "channel": channel,
        "sender_identifier": sender,
        "text": text_body,
        "media_url": None,
        "media_type": None,
        "timestamp": "2026-09-09T20:00:00+00:00",
        "external_message_id": external_id,
        "raw_payload": {"origen": "test"},
    }
    payload.update(extra)
    return payload


CASOS = [
    pytest.param("ycloud", "whatsapp", "573001112233", "wamid.FLOW001", id="ycloud-whatsapp"),
    pytest.param("meta", "instagram", "6789000000000001", "ig.FLOW002", id="meta-instagram"),
    pytest.param("meta", "facebook", "5432000000000001", "fb.FLOW003", id="meta-facebook"),
]


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def webhook_tenant(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[uuid.UUID, None]:
    """Crea un tenant commiteado y lo deja como DEFAULT_CLIENT_ID del worker.

    El worker abre su propia conexion, asi que el cliente tiene que estar commiteado
    y no puede vivir dentro de una transaccion que luego se revierte.
    """
    from app.core.config import get_settings

    client_id = uuid.uuid4()

    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, max_agents, is_active) "
                "VALUES (:id, 'Tenant Webhook Flow', :slug, 'free', 1, true)"
            ),
            {"id": str(client_id), "slug": f"webhook-flow-{client_id.hex[:8]}"},
        )

    monkeypatch.setattr(get_settings(), "DEFAULT_CLIENT_ID", str(client_id))

    yield client_id

    # Limpieza en orden inverso al de las FKs.
    async with AsyncSessionLocal() as session, session.begin():
        for tabla in (
            "messages",
            "conversations",
            "contact_identifiers",
            "contacts",
            "webhook_dedup",
        ):
            await session.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :cid"),  # noqa: S608
                {"cid": str(client_id)},
            )
        await session.execute(
            text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)}
        )


async def _contar(client_id: uuid.UUID, tabla: str) -> int:
    """Cuenta filas del tenant en la tabla indicada, con SET LOCAL aplicado."""
    async with tenant_session(client_id) as session:
        result = await session.execute(
            text(f"SELECT count(*) FROM {tabla} WHERE client_id = :cid"),  # noqa: S608
            {"cid": str(client_id)},
        )
        return int(result.scalar_one())


# ─── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("provider", "channel", "sender", "external_id"), CASOS)
async def test_flujo_completo_por_canal(
    webhook_tenant: uuid.UUID,
    provider: str,
    channel: str,
    sender: str,
    external_id: str,
) -> None:
    """Un mensaje nuevo crea contacto, conversacion, mensaje y registro de dedup."""
    await _process_message(provider, channel, _normalized(channel, sender, external_id))

    assert await _contar(webhook_tenant, "contacts") == 1
    assert await _contar(webhook_tenant, "conversations") == 1
    assert await _contar(webhook_tenant, "messages") == 1
    assert await _contar(webhook_tenant, "webhook_dedup") == 1

    async with tenant_session(webhook_tenant) as session:
        fila = (
            await session.execute(
                text(
                    "SELECT m.direction, m.content, m.external_message_id, m.sender_type, "
                    "c.status, c.channel FROM messages m "
                    "JOIN conversations c ON c.id = m.conversation_id "
                    "WHERE m.client_id = :cid"
                ),
                {"cid": str(webhook_tenant)},
            )
        ).one()

    assert fila.direction == "inbound"
    assert fila.content == "Hola"
    assert fila.external_message_id == external_id
    assert fila.sender_type == "contact"
    assert fila.status == "bot_active"
    assert fila.channel == channel


async def test_mismo_mensaje_dos_veces_no_duplica(webhook_tenant: uuid.UUID) -> None:
    """La idempotencia de nivel 2 corta el reproceso aunque Redis no intervenga."""
    mensaje = _normalized("whatsapp", "573001112233", "wamid.IDEMPOTENTE")

    await _process_message("ycloud", "whatsapp", mensaje)
    await _process_message("ycloud", "whatsapp", mensaje)

    assert await _contar(webhook_tenant, "messages") == 1
    assert await _contar(webhook_tenant, "webhook_dedup") == 1


async def test_segundo_mensaje_reusa_contacto_y_conversacion(
    webhook_tenant: uuid.UUID,
) -> None:
    """Mensajes distintos del mismo remitente no abren contacto ni hilo nuevos."""
    await _process_message(
        "ycloud", "whatsapp", _normalized("whatsapp", "573001112233", "wamid.A", "Primero")
    )
    await _process_message(
        "ycloud", "whatsapp", _normalized("whatsapp", "573001112233", "wamid.B", "Segundo")
    )

    assert await _contar(webhook_tenant, "contacts") == 1
    assert await _contar(webhook_tenant, "conversations") == 1
    assert await _contar(webhook_tenant, "messages") == 2


async def test_remitentes_distintos_abren_contactos_distintos(
    webhook_tenant: uuid.UUID,
) -> None:
    """Cada identifier del canal es un contacto propio."""
    await _process_message(
        "ycloud", "whatsapp", _normalized("whatsapp", "573001112233", "wamid.C")
    )
    await _process_message(
        "ycloud", "whatsapp", _normalized("whatsapp", "573004445566", "wamid.D")
    )

    assert await _contar(webhook_tenant, "contacts") == 2
    assert await _contar(webhook_tenant, "conversations") == 2


async def test_mismo_remitente_en_canales_distintos_no_comparte_conversacion(
    webhook_tenant: uuid.UUID,
) -> None:
    """El hilo es por (contacto, canal): Instagram y Facebook no se mezclan."""
    await _process_message(
        "meta", "instagram", _normalized("instagram", "6789000000000001", "ig.X")
    )
    await _process_message(
        "meta", "facebook", _normalized("facebook", "6789000000000001", "fb.X")
    )

    assert await _contar(webhook_tenant, "conversations") == 2
    assert await _contar(webhook_tenant, "messages") == 2


async def test_conversacion_resuelta_no_se_reabre(webhook_tenant: uuid.UUID) -> None:
    """Si el hilo anterior quedo resolved, el mensaje nuevo abre uno fresco."""
    await _process_message(
        "ycloud", "whatsapp", _normalized("whatsapp", "573001112233", "wamid.E")
    )

    async with tenant_session(webhook_tenant) as session:
        await session.execute(
            text("UPDATE conversations SET status = 'resolved' WHERE client_id = :cid"),
            {"cid": str(webhook_tenant)},
        )

    await _process_message(
        "ycloud", "whatsapp", _normalized("whatsapp", "573001112233", "wamid.F")
    )

    assert await _contar(webhook_tenant, "contacts") == 1
    assert await _contar(webhook_tenant, "conversations") == 2
