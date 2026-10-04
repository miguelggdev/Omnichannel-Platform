# ruff: noqa: F811
"""Contestar desde el panel contra PostgreSQL real con RLS (Sprint 15, fase 2).

Requiere base de datos: `pytest tests/ --run-db`. Solo el proveedor de mensajeria esta
sustituido; el estado de la conversacion, la asignacion y el mensaje guardado son reales.
"""

from typing import Any

import pytest
from sqlalchemy import text

from app.agents.nodes import _delivery as delivery_module
from app.core.database import tenant_session
from tests.integration.test_crm_api import (
    CONVERSATIONS,
    Escenario,
    _cliente,
    dos_tenants,  # noqa: F401  (fixture)
    escenario,  # noqa: F401  (fixture)
)

# Los fixtures `escenario` y `dos_tenants` se reutilizan importandolos de test_crm_api: pytest
# los registra por nombre y ruff ve cada parametro como una redefinicion (F811).
pytestmark = [pytest.mark.db, pytest.mark.asyncio]


@pytest.fixture
def envios(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Sustituye solo el proveedor; el resto del envio es real."""
    enviados: list[dict[str, Any]] = []

    class FakeProvider:
        async def send_message(self, to: str, content: Any, channel_config: Any) -> str:
            enviados.append({"to": to, "text": content.text})
            return f"ext-{len(enviados)}"

    monkeypatch.setattr(
        delivery_module, "get_channel_config", lambda canal: ("ycloud", {"api_key": "k"})
    )
    monkeypatch.setattr(delivery_module, "get_messaging_provider", lambda n, c: FakeProvider())
    return enviados


async def _fila(esc: Escenario, sql: str) -> Any:
    async with tenant_session(esc.client_id) as session:
        return (
            (await session.execute(text(sql), {"id": str(esc.conversation_id)})).mappings().first()
        )


async def test_contestar_envia_guarda_y_toma_la_conversacion(
    escenario: Escenario,
    envios: list[dict[str, Any]],
) -> None:
    async with _cliente(escenario, role="agent") as client:
        r = await client.post(
            f"{CONVERSATIONS}/{escenario.conversation_id}/messages", json={"text": "  Hola Ada  "}
        )

    assert r.status_code == 201, r.text
    assert r.json()["sender_type"] == "agent"
    assert r.json()["sender_id"] == str(escenario.user_id)
    assert [e["text"] for e in envios] == ["Hola Ada"]

    conv = await _fila(
        escenario, "SELECT status, assigned_user_id FROM conversations WHERE id = :id"
    )
    assert (conv["status"], conv["assigned_user_id"]) == ("human_active", escenario.user_id)

    msg = await _fila(
        escenario,
        "SELECT direction, sender_type, sender_id, content, external_message_id "
        "FROM messages WHERE conversation_id = :id",
    )
    assert dict(msg) == {
        "direction": "outbound",
        "sender_type": "agent",
        "sender_id": escenario.user_id,
        "content": "Hola Ada",
        "external_message_id": "ext-1",
    }


async def test_si_el_proveedor_falla_no_queda_mensaje_pero_si_la_conversacion_tomada(
    escenario: Escenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Roto:
        async def send_message(self, to: str, content: Any, channel_config: Any) -> str:
            raise RuntimeError("boom con https://secreto.example")

    monkeypatch.setattr(delivery_module, "get_channel_config", lambda c: ("ycloud", {"a": 1}))
    monkeypatch.setattr(delivery_module, "get_messaging_provider", lambda n, c: Roto())

    async with _cliente(escenario, role="agent") as client:
        r = await client.post(
            f"{CONVERSATIONS}/{escenario.conversation_id}/messages", json={"text": "Hola"}
        )

    assert r.status_code == 502
    assert "secreto" not in r.text
    msgs = await _fila(escenario, "SELECT count(*) AS n FROM messages WHERE conversation_id = :id")
    assert msgs["n"] == 0
    conv = await _fila(escenario, "SELECT status FROM conversations WHERE id = :id")
    assert conv["status"] == "human_active"


async def test_una_conversacion_resuelta_no_admite_respuesta(
    escenario: Escenario,
    envios: list[dict[str, Any]],
) -> None:
    async with tenant_session(escenario.client_id) as session:
        await session.execute(
            text("UPDATE conversations SET status = 'resolved' WHERE id = :id"),
            {"id": str(escenario.conversation_id)},
        )

    async with _cliente(escenario, role="admin") as client:
        r = await client.post(
            f"{CONVERSATIONS}/{escenario.conversation_id}/messages", json={"text": "Hola"}
        )

    assert r.status_code == 409
    assert envios == []


async def test_un_tenant_no_puede_contestar_en_la_conversacion_de_otro(
    dos_tenants: tuple[Escenario, Escenario],
    envios: list[dict[str, Any]],
) -> None:
    a, b = dos_tenants

    async with _cliente(b, role="admin") as client:
        r = await client.post(
            f"{CONVERSATIONS}/{a.conversation_id}/messages", json={"text": "intruso"}
        )

    assert r.status_code == 404
    assert envios == []
    conv = await _fila(a, "SELECT status FROM conversations WHERE id = :id")
    assert conv["status"] == "bot_active"


async def test_el_contacto_sin_identificador_en_el_canal_es_409(
    escenario: Escenario,
    envios: list[dict[str, Any]],
) -> None:
    async with tenant_session(escenario.client_id) as session:
        await session.execute(
            text("UPDATE conversations SET channel = 'telegram' WHERE id = :id"),
            {"id": str(escenario.conversation_id)},
        )
    monkey = pytest.MonkeyPatch()
    monkey.setattr(delivery_module, "get_channel_config", lambda c: ("telegram", {"a": 1}))
    try:
        async with _cliente(escenario, role="admin") as client:
            r = await client.post(
                f"{CONVERSATIONS}/{escenario.conversation_id}/messages", json={"text": "Hola"}
            )
    finally:
        monkey.undo()

    assert r.status_code == 409
    assert envios == []
