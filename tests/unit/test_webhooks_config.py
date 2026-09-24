"""Tests del CRUD de configuracion de webhooks salientes (Sprint 11, Dev B).

Mismo patron que `test_quick_replies.py`: la base se sustituye por `CrmSession`,
corren sin `--run-db`, y el envio real (`WebhookDispatcher.send_webhook`) se
sustituye por un doble en los tests del endpoint de prueba.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.api.v1 import webhooks_config as webhooks_config_module
from tests.unit.agent_doubles import fake_tenant_session
from tests.unit.crm_doubles import AHORA, CrmSession

URL = "/api/v1/outgoing-webhooks"


class FakeTenantWebhook:
    """Sustituto de TenantWebhook con lo que leen los endpoints y schemas."""

    def __init__(self, **kwargs: Any) -> None:
        """Construye el webhook con valores por defecto razonables."""
        self.id = kwargs.get("id") or uuid.uuid4()
        self.client_id = kwargs.get("client_id") or uuid.uuid4()
        self.url = kwargs.get("url", "https://ejemplo.com/hook")
        self.secret = kwargs.get("secret", "secreto-de-prueba-1234567890")
        self.events = kwargs.get("events", ["message.received"])
        self.is_active = kwargs.get("is_active", True)
        self.description = kwargs.get("description")
        self.headers = kwargs.get("headers", {})
        self.consecutive_failures = kwargs.get("consecutive_failures", 0)
        self.last_triggered_at = kwargs.get("last_triggered_at")
        self.last_success_at = kwargs.get("last_success_at")
        self.last_failure_at = kwargs.get("last_failure_at")
        self.disabled_reason = kwargs.get("disabled_reason")
        self.created_at = kwargs.get("created_at", AHORA)
        self.updated_at = kwargs.get("updated_at", AHORA)


class FakeWebhookLog:
    """Sustituto de OutgoingWebhookLog con lo que lee `WebhookLogResponse`."""

    def __init__(self, **kwargs: Any) -> None:
        """Construye el log con valores por defecto razonables."""
        self.id = kwargs.get("id") or uuid.uuid4()
        self.event = kwargs.get("event", "message.received")
        self.status = kwargs.get("status", "success")
        self.response_code = kwargs.get("response_code", 200)
        self.duration_ms = kwargs.get("duration_ms", 120)
        self.attempt = kwargs.get("attempt", 1)
        self.error = kwargs.get("error")
        self.created_at = kwargs.get("created_at", AHORA)


def _usa_sesion(monkeypatch: pytest.MonkeyPatch, session: CrmSession) -> CrmSession:
    """Hace que los endpoints de webhooks salientes usen la sesion falsa."""
    monkeypatch.setattr(webhooks_config_module, "tenant_session", fake_tenant_session(session))
    return session


# ─── GET /webhooks/outgoing ──────────────────────────────────────────────────


class TestListado:
    async def test_devuelve_los_del_tenant_sin_el_secreto(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[[FakeTenantWebhook()]]))

        response = await authenticated_client.get(URL)

        assert response.status_code == 200
        cuerpo = response.json()[0]
        assert "secret" not in cuerpo
        assert cuerpo["url"] == "https://ejemplo.com/hook"
        assert "order by tenant_webhooks.created_at desc" in str(session.executed[0]).lower()

    async def test_el_client_id_va_explicito_en_el_where(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[[]]))

        await authenticated_client.get(URL)

        assert "tenant_webhooks.client_id =" in str(session.executed[0])


# ─── POST /webhooks/outgoing ─────────────────────────────────────────────────


class TestAlta:
    async def test_crea_y_devuelve_el_secreto_en_claro(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _usa_sesion(monkeypatch, CrmSession())

        response = await authenticated_client.post(
            URL,
            json={
                "url": "https://ejemplo.com/hook",
                "events": ["message.received", "conversation.resolved"],
            },
        )

        assert response.status_code == 201
        assert response.json()["secret"]
        assert session.added[0].events == ["message.received", "conversation.resolved"]

    async def test_sin_secreto_se_genera_uno(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _usa_sesion(monkeypatch, CrmSession())

        response = await authenticated_client.post(
            URL, json={"url": "https://ejemplo.com/hook", "events": ["message.received"]}
        )

        assert len(response.json()["secret"]) >= 32
        assert session.added[0].secret == response.json()["secret"]

    async def test_evento_no_soportado_es_400(self, authenticated_client: Any) -> None:
        response = await authenticated_client.post(
            URL, json={"url": "https://ejemplo.com/hook", "events": ["evento.inventado"]}
        )

        assert response.status_code == 400
        assert "evento.inventado" in response.json()["message"]

    async def test_lista_de_eventos_vacia_es_422(self, authenticated_client: Any) -> None:
        response = await authenticated_client.post(
            URL, json={"url": "https://ejemplo.com/hook", "events": []}
        )

        assert response.status_code == 422


# ─── PUT y DELETE ────────────────────────────────────────────────────────────


class TestEdicionYBorrado:
    async def test_actualiza_solo_lo_enviado(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        webhook = FakeTenantWebhook(url="https://viejo.com", description="Original")
        _usa_sesion(monkeypatch, CrmSession(resultados=[webhook]))

        response = await authenticated_client.put(
            f"{URL}/{webhook.id}", json={"url": "https://nuevo.com"}
        )

        assert response.status_code == 200
        assert webhook.url == "https://nuevo.com"
        assert webhook.description == "Original"

    async def test_reactivar_resetea_los_fallos(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        webhook = FakeTenantWebhook(
            is_active=False, consecutive_failures=10, disabled_reason="auto_disabled_failures"
        )
        _usa_sesion(monkeypatch, CrmSession(resultados=[webhook]))

        response = await authenticated_client.put(f"{URL}/{webhook.id}", json={"is_active": True})

        assert response.status_code == 200
        assert webhook.consecutive_failures == 0
        assert webhook.disabled_reason is None

    @pytest.mark.parametrize("campo", ["url", "events", "headers"])
    async def test_null_explicito_en_columna_not_null_es_400(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, campo: str
    ) -> None:
        """Un `null` explicito no debe llegar como `IntegrityError` (500)."""
        webhook = FakeTenantWebhook()
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[webhook]))

        response = await authenticated_client.put(f"{URL}/{webhook.id}", json={campo: None})

        assert response.status_code == 400
        assert campo in response.json()["message"]
        # No debe haber tocado la base: el chequeo va antes de cargar el webhook.
        assert session.executed == []

    async def test_evento_no_soportado_en_update_es_400(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        webhook = FakeTenantWebhook()
        _usa_sesion(monkeypatch, CrmSession(resultados=[webhook]))

        response = await authenticated_client.put(
            f"{URL}/{webhook.id}", json={"events": ["evento.inventado"]}
        )

        assert response.status_code == 400

    async def test_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _usa_sesion(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.put(
            f"{URL}/{uuid.uuid4()}", json={"description": "x"}
        )

        assert response.status_code == 404

    async def test_borra(self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        webhook = FakeTenantWebhook()
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[webhook]))

        response = await authenticated_client.delete(f"{URL}/{webhook.id}")

        assert response.status_code == 200
        assert session.deleted == [webhook]

    async def test_borrar_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _usa_sesion(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.delete(f"{URL}/{uuid.uuid4()}")

        assert response.status_code == 404


# ─── GET /{id}/logs ───────────────────────────────────────────────────────────


class TestLogs:
    async def test_devuelve_el_historial_mas_reciente_primero(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        webhook = FakeTenantWebhook()
        session = _usa_sesion(
            monkeypatch, CrmSession(resultados=[webhook, [FakeWebhookLog(status="failed")]])
        )

        response = await authenticated_client.get(f"{URL}/{webhook.id}/logs")

        assert response.status_code == 200
        assert response.json()[0]["status"] == "failed"
        assert "order by outgoing_webhook_logs.created_at desc" in str(session.executed[1]).lower()

    async def test_filtra_por_status(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        webhook = FakeTenantWebhook()
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[webhook, []]))

        await authenticated_client.get(f"{URL}/{webhook.id}/logs", params={"status": "failed"})

        assert "outgoing_webhook_logs.status =" in str(session.executed[1])

    async def test_webhook_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _usa_sesion(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.get(f"{URL}/{uuid.uuid4()}/logs")

        assert response.status_code == 404


# ─── POST /{id}/test ──────────────────────────────────────────────────────────


class TestEnvioDePrueba:
    async def test_envio_exitoso(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        webhook = FakeTenantWebhook()
        _usa_sesion(monkeypatch, CrmSession(resultados=[webhook]))

        with patch.object(
            webhooks_config_module.WebhookDispatcher,
            "send_webhook",
            AsyncMock(return_value={"success": True, "status_code": 200, "duration_ms": 50}),
        ):
            response = await authenticated_client.post(f"{URL}/{webhook.id}/test")

        assert response.status_code == 200
        assert response.json() == {
            "success": True,
            "status_code": 200,
            "duration_ms": 50,
            "error": None,
        }

    async def test_envio_fallido_no_es_error_http(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un destino que no responde es un resultado de la prueba, no un 5xx."""
        webhook = FakeTenantWebhook()
        _usa_sesion(monkeypatch, CrmSession(resultados=[webhook]))

        with patch.object(
            webhooks_config_module.WebhookDispatcher,
            "send_webhook",
            AsyncMock(return_value={"success": False, "duration_ms": 10000, "error": "Timeout"}),
        ):
            response = await authenticated_client.post(f"{URL}/{webhook.id}/test")

        assert response.status_code == 200
        assert response.json()["success"] is False
        assert response.json()["error"] == "Timeout"

    async def test_webhook_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _usa_sesion(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.post(f"{URL}/{uuid.uuid4()}/test")

        assert response.status_code == 404
