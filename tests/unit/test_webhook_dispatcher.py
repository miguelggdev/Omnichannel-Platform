"""Tests del dispatcher de webhooks salientes (Sprint 11).

`httpx.AsyncClient` se sustituye entero por un cliente falso que guarda lo que
se le pidio enviar: lo que se prueba aca es que firmamos y armamos bien el
request, no que httpx sepa hacer un POST.
"""

import hashlib
import hmac
import json
import os
from types import SimpleNamespace
from typing import ClassVar
from uuid import uuid4

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-testing-only-minimum-32-chars")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-minimum-32-characters-long")

from app.services import webhook_dispatcher as wd
from app.services.webhook_dispatcher import (
    WebhookDispatcher,
    WebhookTargetError,
    validar_destino,
)


def _webhook(**over):
    base = {
        "id": uuid4(),
        "url": "https://ejemplo.com/hook",
        "secret": "un-secreto",
        "headers": {},
    }
    base.update(over)
    return SimpleNamespace(**base)


def _payload(**over):
    base = {
        "event": "message.received",
        "timestamp": "2026-09-23T10:00:00+00:00",
        "webhook_delivery_id": str(uuid4()),
        "data": {"message_id": str(uuid4())},
    }
    base.update(over)
    return base


class _ClienteFalso:
    """Sustituto de `httpx.AsyncClient` que registra el POST y devuelve lo pactado."""

    ultima_llamada: ClassVar[dict] = {}

    def __init__(self, respuesta=None, error: Exception | None = None, **kwargs) -> None:
        self._respuesta = respuesta
        self._error = error
        self.kwargs = kwargs

    async def __aenter__(self) -> "_ClienteFalso":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return

    async def post(self, url: str, content: str, headers: dict):
        type(self).ultima_llamada = {
            "url": url,
            "content": content,
            "headers": headers,
            "kwargs": self.kwargs,
        }
        if self._error is not None:
            raise self._error
        return self._respuesta


def _cliente(monkeypatch, respuesta=None, error: Exception | None = None):
    """Instala `_ClienteFalso` en lugar de `httpx.AsyncClient`."""
    # Se limpia al instalar, no al construir el cliente: hay un test que
    # comprueba justamente que no se construyo ninguno.
    _ClienteFalso.ultima_llamada = {}

    def _fabrica(**kwargs):
        return _ClienteFalso(respuesta=respuesta, error=error, **kwargs)

    monkeypatch.setattr(wd.httpx, "AsyncClient", _fabrica)


def _sin_ssrf(monkeypatch) -> None:
    """Desactiva la comprobacion de destino (se prueba aparte)."""
    monkeypatch.setattr(wd, "validar_destino", lambda url: None)


class TestFirma:
    def test_es_hmac_sha256_de_timestamp_punto_body(self) -> None:
        """La firma es exactamente HMAC-SHA256 sobre "{timestamp}.{body}"."""
        dispatcher = WebhookDispatcher()
        body = '{"event":"test"}'

        firma = dispatcher.compute_signature(body, "secreto", "1700000000")

        esperada = hmac.new(b"secreto", f"1700000000.{body}".encode(), hashlib.sha256).hexdigest()
        assert firma == esperada
        assert len(firma) == 64

    def test_cambia_con_el_secreto_y_con_el_timestamp(self) -> None:
        """Sin esto, un replay con otro timestamp seguiria verificando."""
        dispatcher = WebhookDispatcher()
        body = '{"event":"test"}'

        firma = dispatcher.compute_signature(body, "secreto", "1700000000")

        assert firma != dispatcher.compute_signature(body, "otro", "1700000000")
        assert firma != dispatcher.compute_signature(body, "secreto", "1700000001")

    def test_el_tenant_no_puede_pisar_las_cabeceras_propias(self) -> None:
        """Un `headers` con X-Webhook-Signature no debe invalidar la firma real."""
        dispatcher = WebhookDispatcher()
        webhook = _webhook(headers={"X-Webhook-Signature": "sha256=falsa", "X-Propia": "1"})
        payload = _payload()
        body = json.dumps(payload, default=str, sort_keys=True)

        headers, timestamp = dispatcher.build_headers(webhook, payload, body)

        assert headers["X-Webhook-Signature"] == (
            f"sha256={dispatcher.compute_signature(body, webhook.secret, timestamp)}"
        )
        assert headers["X-Propia"] == "1"


class TestValidarDestino:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/hook",
            "http://localhost/hook",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/hook",
            "http://192.168.1.10/hook",
            "http://[::1]/hook",
        ],
    )
    def test_rechaza_la_red_interna(self, url: str) -> None:
        """El destino lo elige el tenant y quien hace el POST esta dentro de la red."""
        with pytest.raises(WebhookTargetError):
            validar_destino(url)

    @pytest.mark.parametrize("url", ["ftp://ejemplo.com/hook", "file:///etc/passwd", "no-es-url"])
    def test_rechaza_esquemas_que_no_son_http(self, url: str) -> None:
        with pytest.raises(WebhookTargetError):
            validar_destino(url)

    def test_acepta_una_ip_publica(self, monkeypatch) -> None:
        monkeypatch.setattr(
            wd,
            "_direcciones_del_host",
            lambda host: [__import__("ipaddress").ip_address("93.184.216.34")],
        )

        validar_destino("https://ejemplo.com/hook")

    def test_el_flag_de_desarrollo_permite_localhost(self, monkeypatch) -> None:
        """`OUTGOING_WEBHOOK_ALLOW_PRIVATE_HOSTS` existe para el receptor local."""
        monkeypatch.setattr(
            wd, "get_settings", lambda: SimpleNamespace(OUTGOING_WEBHOOK_ALLOW_PRIVATE_HOSTS=True)
        )

        validar_destino("http://127.0.0.1:8001/hook")


class TestSendWebhook:
    @pytest.mark.asyncio
    async def test_respuesta_2xx_es_exito(self, monkeypatch) -> None:
        _sin_ssrf(monkeypatch)
        _cliente(monkeypatch, respuesta=SimpleNamespace(status_code=200, text="OK"))

        resultado = await WebhookDispatcher().send_webhook(_webhook(), _payload())

        assert resultado["success"] is True
        assert resultado["status_code"] == 200
        assert resultado["response_body"] == "OK"
        assert resultado["duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_respuesta_5xx_es_fallo_con_motivo(self, monkeypatch) -> None:
        _sin_ssrf(monkeypatch)
        _cliente(monkeypatch, respuesta=SimpleNamespace(status_code=500, text="boom"))

        resultado = await WebhookDispatcher().send_webhook(_webhook(), _payload())

        assert resultado["success"] is False
        assert resultado["status_code"] == 500
        assert "HTTP 500" in resultado["error"]

    @pytest.mark.asyncio
    async def test_timeout_no_propaga(self, monkeypatch) -> None:
        _sin_ssrf(monkeypatch)
        _cliente(monkeypatch, error=httpx.TimeoutException("agotado"))

        resultado = await WebhookDispatcher().send_webhook(_webhook(), _payload())

        assert resultado["success"] is False
        assert "Timeout" in resultado["error"]

    @pytest.mark.asyncio
    async def test_error_de_red_no_propaga(self, monkeypatch) -> None:
        _sin_ssrf(monkeypatch)
        _cliente(monkeypatch, error=httpx.ConnectError("sin ruta"))

        resultado = await WebhookDispatcher().send_webhook(_webhook(), _payload())

        assert resultado["success"] is False
        assert "Error de red" in resultado["error"]

    @pytest.mark.asyncio
    async def test_un_destino_interno_no_llega_a_enviarse(self, monkeypatch) -> None:
        """La comprobacion de SSRF corta antes del POST, no despues."""
        _cliente(monkeypatch, respuesta=SimpleNamespace(status_code=200, text="OK"))

        resultado = await WebhookDispatcher().send_webhook(
            _webhook(url="http://169.254.169.254/latest/meta-data/"), _payload()
        )

        assert resultado["success"] is False
        assert _ClienteFalso.ultima_llamada == {}

    @pytest.mark.asyncio
    async def test_el_cuerpo_enviado_es_el_firmado(self, monkeypatch) -> None:
        """Serializar dos veces romperia la verificacion del receptor."""
        _sin_ssrf(monkeypatch)
        _cliente(monkeypatch, respuesta=SimpleNamespace(status_code=204, text=""))
        webhook = _webhook()
        payload = _payload()

        await WebhookDispatcher().send_webhook(webhook, payload)

        llamada = _ClienteFalso.ultima_llamada
        firma = llamada["headers"]["X-Webhook-Signature"].removeprefix("sha256=")
        esperada = WebhookDispatcher().compute_signature(
            llamada["content"], webhook.secret, llamada["headers"]["X-Webhook-Timestamp"]
        )
        assert firma == esperada
        assert json.loads(llamada["content"]) == payload

    @pytest.mark.asyncio
    async def test_no_sigue_redirecciones(self, monkeypatch) -> None:
        """Un 302 a 127.0.0.1 esquivaria la comprobacion de destino."""
        _sin_ssrf(monkeypatch)
        _cliente(monkeypatch, respuesta=SimpleNamespace(status_code=200, text="OK"))

        await WebhookDispatcher().send_webhook(_webhook(), _payload())

        assert _ClienteFalso.ultima_llamada["kwargs"]["follow_redirects"] is False

    @pytest.mark.asyncio
    async def test_las_cabeceras_estandar_viajan(self, monkeypatch) -> None:
        _sin_ssrf(monkeypatch)
        _cliente(monkeypatch, respuesta=SimpleNamespace(status_code=200, text="OK"))
        payload = _payload()

        await WebhookDispatcher().send_webhook(_webhook(), payload)

        headers = _ClienteFalso.ultima_llamada["headers"]
        assert headers["X-Webhook-Event"] == "message.received"
        assert headers["X-Webhook-ID"] == payload["webhook_delivery_id"]
        assert headers["Content-Type"] == "application/json"
        assert headers["User-Agent"] == wd.USER_AGENT

    @pytest.mark.asyncio
    async def test_la_respuesta_se_recorta(self, monkeypatch) -> None:
        _sin_ssrf(monkeypatch)
        _cliente(monkeypatch, respuesta=SimpleNamespace(status_code=200, text="x" * 5000))

        resultado = await WebhookDispatcher().send_webhook(_webhook(), _payload())

        assert len(resultado["response_body"]) == wd.RESPONSE_BODY_MAX_CHARS
