"""Tests del receptor de webhooks (`app/api/v1/webhooks.py`) y del servicio de dedup.

No se toca Redis, ni Celery, ni la base de datos: el provider, la deduplicacion y el
encolado se sustituyen para poder afirmar sobre el contrato del endpoint.

`app.services.messaging.factory` es entrega de Dev A y todavia no existe, por eso los
tests del POST reemplazan `_resolve_provider` en lugar de instanciar un provider real.
"""

import uuid
from typing import Any

import pytest

from app.api.v1 import webhooks as webhooks_module
from app.services import dedup as dedup_module

WEBHOOK_URL = "/api/v1/webhooks/meta/instagram"


# ─── Dobles de prueba ────────────────────────────────────────────────────────


class FakeNormalized:
    """Sustituto de NormalizedMessage (schema pendiente de Dev A)."""

    def __init__(self, external_message_id: str = "mid.0001", channel: str = "instagram") -> None:
        self.external_message_id = external_message_id
        self.channel = channel

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        """Serializa como lo haria el schema real de Pydantic."""
        return {
            "channel": self.channel,
            "sender_identifier": "6789000000000001",
            "text": "hola",
            "external_message_id": self.external_message_id,
            "timestamp": "2026-09-09T20:00:00+00:00",
            "raw_payload": {},
        }


class FakeProvider:
    """MessagingProvider minimo: firma configurable y parseo controlado."""

    def __init__(
        self,
        *,
        valid_signature: bool = True,
        normalized: FakeNormalized | None = None,
        parse_raises: bool = False,
    ) -> None:
        self.valid_signature = valid_signature
        self.normalized = normalized or FakeNormalized()
        self.parse_raises = parse_raises

    async def validate_signature(self, payload: bytes, signature: str, secret: str) -> bool:
        """Devuelve el veredicto fijado en el constructor."""
        return self.valid_signature

    async def parse_webhook(self, raw_payload: dict[str, Any]) -> FakeNormalized:
        """Devuelve el mensaje normalizado, o revienta si asi se configuro."""
        if self.parse_raises:
            raise ValueError("payload no reconocido")
        return self.normalized


class FakeTask:
    """Sustituto de la tarea Celery: registra las llamadas a delay()."""

    def __init__(self, raises: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raises = raises

    def delay(self, **kwargs: Any) -> None:
        """Registra el encolado, o falla para simular un broker caido."""
        if self.raises:
            raise ConnectionError("broker no disponible")
        self.calls.append(kwargs)


class FakeRedis:
    """Redis en memoria con la semantica de SET NX EX que usa la deduplicacion."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(
        self, key: str, value: str, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        """Implementa SET NX: solo escribe si la clave no existe."""
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, key: str) -> int:
        """Borra la clave si existe."""
        return 1 if self.store.pop(key, None) is not None else 0


@pytest.fixture
def fake_redis() -> FakeRedis:
    """Redis en memoria para los tests de deduplicacion."""
    return FakeRedis()


@pytest.fixture
def patched_webhook(monkeypatch: pytest.MonkeyPatch, fake_redis: FakeRedis) -> FakeTask:
    """Sustituye dedup y Celery en el endpoint; devuelve la tarea falsa.

    El provider NO se sustituye aqui: cada test decide cual quiere.
    """
    import app.tasks.webhook_processor as processor_module

    fake_task = FakeTask()
    monkeypatch.setattr(processor_module, "process_incoming_message", fake_task)
    monkeypatch.setattr(dedup_module, "_redis_client", fake_redis, raising=False)
    monkeypatch.setattr(dedup_module, "get_redis", lambda: fake_redis)
    return fake_task


def _use_provider(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    """Hace que el endpoint resuelva siempre el provider indicado."""
    monkeypatch.setattr(
        webhooks_module,
        "_resolve_provider",
        lambda name, config=None: provider,
    )


# ─── POST: deduplicacion ─────────────────────────────────────────────────────


class TestWebhookDeduplication:
    """El mismo external_message_id solo puede encolarse una vez."""

    async def test_mensaje_nuevo_se_encola(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch, patched_webhook: FakeTask
    ) -> None:
        """Un mensaje no visto responde queued y llega a Celery."""
        _use_provider(monkeypatch, FakeProvider())

        response = await api_client.post(WEBHOOK_URL, json={"object": "instagram"})

        assert response.status_code == 200
        assert response.json() == {"status": "queued"}
        assert len(patched_webhook.calls) == 1

    async def test_mensaje_repetido_se_descarta(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch, patched_webhook: FakeTask
    ) -> None:
        """Dos entregas del mismo id: la segunda responde duplicate y no se encola."""
        _use_provider(monkeypatch, FakeProvider(normalized=FakeNormalized("mid.repetido")))
        payload = {"object": "instagram"}

        first = await api_client.post(WEBHOOK_URL, json=payload)
        second = await api_client.post(WEBHOOK_URL, json=payload)

        assert first.json() == {"status": "queued"}
        assert second.status_code == 200
        assert second.json() == {"status": "duplicate"}
        assert len(patched_webhook.calls) == 1, "el duplicado no debe encolarse"

    async def test_ids_distintos_se_encolan_por_separado(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch, patched_webhook: FakeTask
    ) -> None:
        """Mensajes distintos del mismo canal no se pisan entre si."""
        _use_provider(monkeypatch, FakeProvider(normalized=FakeNormalized("mid.aaa")))
        await api_client.post(WEBHOOK_URL, json={})

        _use_provider(monkeypatch, FakeProvider(normalized=FakeNormalized("mid.bbb")))
        await api_client.post(WEBHOOK_URL, json={})

        assert len(patched_webhook.calls) == 2


# ─── POST: autenticacion y errores ───────────────────────────────────────────


class TestWebhookSignature:
    """La autenticacion del endpoint es la firma HMAC, no el JWT."""

    async def test_firma_invalida_devuelve_401(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch, patched_webhook: FakeTask
    ) -> None:
        """Firma que no valida: 401 y nada encolado."""
        _use_provider(monkeypatch, FakeProvider(valid_signature=False))

        response = await api_client.post(WEBHOOK_URL, json={"object": "instagram"})

        assert response.status_code == 401
        assert response.json()["error_code"] == webhooks_module.INVALID_SIGNATURE
        assert patched_webhook.calls == []

    async def test_no_expone_traceback(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch, patched_webhook: FakeTask
    ) -> None:
        """El cuerpo del error nunca contiene traza interna."""
        _use_provider(monkeypatch, FakeProvider(valid_signature=False))

        response = await api_client.post(WEBHOOK_URL, json={})

        body = response.text.lower()
        assert "traceback" not in body
        assert "file " not in body


class TestWebhookErrores:
    """Contratos de error del POST."""

    async def test_provider_desconocido_devuelve_400(
        self, api_client: Any, patched_webhook: FakeTask
    ) -> None:
        """Un provider no registrado responde 400 (no 404 ni 500)."""
        response = await api_client.post("/api/v1/webhooks/telegram/telegram", json={})

        assert response.status_code == 400
        assert response.json()["error_code"] == webhooks_module.UNSUPPORTED_PROVIDER

    async def test_payload_no_parseable_devuelve_200(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch, patched_webhook: FakeTask
    ) -> None:
        """Un payload irreconocible responde 200 para que el proveedor no reintente."""
        _use_provider(monkeypatch, FakeProvider(parse_raises=True))

        response = await api_client.post(WEBHOOK_URL, json={"raro": True})

        assert response.status_code == 200
        assert response.json() == {"status": "parse_error"}
        assert patched_webhook.calls == []

    async def test_fallo_de_encolado_libera_la_marca_de_dedup(
        self,
        api_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        patched_webhook: FakeTask,
        fake_redis: FakeRedis,
    ) -> None:
        """Si Celery falla: 503 y la clave se libera para que el reintento pase."""
        import app.tasks.webhook_processor as processor_module

        monkeypatch.setattr(processor_module, "process_incoming_message", FakeTask(raises=True))
        _use_provider(monkeypatch, FakeProvider(normalized=FakeNormalized("mid.sin.broker")))

        response = await api_client.post(WEBHOOK_URL, json={})

        assert response.status_code == 503
        assert response.json()["error_code"] == webhooks_module.QUEUE_UNAVAILABLE
        assert fake_redis.store == {}, "la marca debe liberarse o el mensaje se pierde 24h"


# ─── GET: verificacion de la URL del webhook ─────────────────────────────────


class TestMetaVerification:
    """Handshake `hub.mode=subscribe` de Meta."""

    async def test_token_correcto_devuelve_challenge(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con el verify_token correcto se devuelve el challenge en texto plano."""
        from app.core.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "META_WEBHOOK_VERIFY_TOKEN", "token-de-prueba")

        response = await api_client.get(
            WEBHOOK_URL,
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "token-de-prueba",
                "hub.challenge": "1158201444",
            },
        )

        assert response.status_code == 200
        assert response.text == "1158201444"
        assert response.headers["content-type"].startswith("text/plain")

    async def test_token_incorrecto_devuelve_403(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un verify_token que no coincide no revela el challenge."""
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "META_WEBHOOK_VERIFY_TOKEN", "token-de-prueba")

        response = await api_client.get(
            WEBHOOK_URL,
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "token-equivocado",
                "hub.challenge": "1158201444",
            },
        )

        assert response.status_code == 403
        assert "1158201444" not in response.text

    async def test_token_sin_configurar_no_valida(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin token configurado el GET no puede validar por accidente."""
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "META_WEBHOOK_VERIFY_TOKEN", "")

        response = await api_client.get(
            WEBHOOK_URL,
            params={"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "x"},
        )

        assert response.status_code == 403


class TestYCloudVerification:
    """YCloud usa un challenge simple en query param."""

    async def test_challenge_se_devuelve_tal_cual(self, api_client: Any) -> None:
        """El challenge vuelve como texto plano."""
        response = await api_client.get(
            "/api/v1/webhooks/ycloud/whatsapp", params={"challenge": "abc123"}
        )

        assert response.status_code == 200
        assert response.text == "abc123"

    async def test_sin_challenge_responde_ok(self, api_client: Any) -> None:
        """Sin challenge el endpoint sigue respondiendo 200."""
        response = await api_client.get("/api/v1/webhooks/ycloud/whatsapp")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


# ─── Servicio de deduplicacion ───────────────────────────────────────────────


class TestDedupService:
    """Contrato de `app/services/dedup.py`."""

    def test_patron_de_clave(self) -> None:
        """La clave sigue el patron webhook_dedup:{channel}:{external_message_id}."""
        assert (
            dedup_module.build_dedup_key("whatsapp", "wamid.X")
            == "webhook_dedup:whatsapp:wamid.X"
        )

    async def test_primera_marca_es_nueva_y_la_segunda_no(self, fake_redis: FakeRedis) -> None:
        """SET NX: solo la primera llamada gana."""
        assert await dedup_module.mark_if_new("whatsapp", "id-1", redis_client=fake_redis) is True
        assert await dedup_module.mark_if_new("whatsapp", "id-1", redis_client=fake_redis) is False

    async def test_release_permite_reprocesar(self, fake_redis: FakeRedis) -> None:
        """Liberar la marca deja pasar el reintento del proveedor."""
        await dedup_module.mark_if_new("whatsapp", "id-2", redis_client=fake_redis)
        await dedup_module.release_mark("whatsapp", "id-2", redis_client=fake_redis)

        assert await dedup_module.mark_if_new("whatsapp", "id-2", redis_client=fake_redis) is True

    async def test_redis_caido_no_bloquea_el_mensaje(self) -> None:
        """Fail-open: si Redis falla se procesa igual y decide PostgreSQL."""

        class BrokenRedis:
            async def set(self, *args: Any, **kwargs: Any) -> bool:
                raise ConnectionError("redis caido")

        resultado = await dedup_module.mark_if_new(
            "whatsapp", "id-3", redis_client=BrokenRedis()
        )

        assert resultado is True


# ─── Resolucion de tenant en el worker ───────────────────────────────────────


class TestResolucionDeTenant:
    """`_resolve_client_id` es el unico punto que decide el tenant del webhook."""

    def test_devuelve_el_uuid_configurado(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Con DEFAULT_CLIENT_ID valido se obtiene el UUID."""
        from app.core.config import get_settings
        from app.tasks.webhook_processor import _resolve_client_id

        esperado = uuid.uuid4()
        monkeypatch.setattr(get_settings(), "DEFAULT_CLIENT_ID", str(esperado))

        assert _resolve_client_id("meta", "instagram") == esperado

    def test_sin_configurar_falla_explicito(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin DEFAULT_CLIENT_ID el mensaje debe fallar, nunca caer en otro tenant."""
        from app.core.config import get_settings
        from app.tasks.webhook_processor import ClientResolutionError, _resolve_client_id

        monkeypatch.setattr(get_settings(), "DEFAULT_CLIENT_ID", "")

        with pytest.raises(ClientResolutionError):
            _resolve_client_id("meta", "instagram")

    def test_uuid_invalido_falla_explicito(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un DEFAULT_CLIENT_ID mal escrito no debe pasar silenciosamente."""
        from app.core.config import get_settings
        from app.tasks.webhook_processor import ClientResolutionError, _resolve_client_id

        monkeypatch.setattr(get_settings(), "DEFAULT_CLIENT_ID", "no-es-un-uuid")

        with pytest.raises(ClientResolutionError):
            _resolve_client_id("meta", "instagram")
