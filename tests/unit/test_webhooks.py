"""Tests del receptor de webhooks (`app/api/v1/webhooks.py`) y del servicio de dedup.

No se toca Redis, ni Celery, ni la base de datos: el provider, la deduplicacion y el
encolado se sustituyen para poder afirmar sobre el contrato del endpoint.

`app.services.messaging.factory` es entrega de Dev A y todavia no existe, por eso los
tests del POST reemplazan `_resolve_provider` en lugar de instanciar un provider real.
"""

import uuid
from typing import Any, ClassVar

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
        response = await api_client.post("/api/v1/webhooks/linkedin/linkedin", json={})

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


# ─── POST: Telegram, con el provider real ────────────────────────────────────


class TestWebhookTelegram:
    """El recorrido completo del endpoint con `TelegramProvider` (sin sustituirlo)."""

    URL = "/api/v1/webhooks/telegram/telegram"
    SECRETO = "secreto-de-prueba_123"
    UPDATE: ClassVar[dict[str, Any]] = {
        "update_id": 4242,
        "message": {
            "message_id": 7,
            "from": {"id": 789, "is_bot": False, "first_name": "Ada"},
            "chat": {"id": 789, "type": "private"},
            "date": 1_700_000_000,
            "text": "Hola",
        },
    }

    @pytest.fixture(autouse=True)
    def _secreto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Configura `TELEGRAM_WEBHOOK_SECRET` sin tocar el `.env`."""
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "TELEGRAM_WEBHOOK_SECRET", self.SECRETO)

    async def test_update_valido_se_encola(
        self, api_client: Any, patched_webhook: FakeTask
    ) -> None:
        """Con el secret_token correcto, el mensaje llega a Celery ya normalizado."""
        response = await api_client.post(
            self.URL,
            json=self.UPDATE,
            headers={"X-Telegram-Bot-Api-Secret-Token": self.SECRETO},
        )

        assert response.status_code == 200
        assert response.json() == {"status": "queued"}
        (llamada,) = patched_webhook.calls
        assert llamada["provider"] == "telegram"
        mensaje = llamada["normalized_message"]
        assert mensaje["channel"] == "telegram"
        assert mensaje["sender_identifier"] == "789"
        assert mensaje["sender_name"] == "Ada"
        assert mensaje["external_message_id"] == "4242"
        assert mensaje["text"] == "Hola"

    @pytest.mark.parametrize("cabecera", [None, "", "otro-secreto"])
    async def test_sin_secret_token_correcto_es_401(
        self, api_client: Any, patched_webhook: FakeTask, cabecera: str | None
    ) -> None:
        """Sin el secret_token cualquiera podria inyectar mensajes al bot."""
        headers = {} if cabecera is None else {"X-Telegram-Bot-Api-Secret-Token": cabecera}

        response = await api_client.post(self.URL, json=self.UPDATE, headers=headers)

        assert response.status_code == 401
        assert response.json()["error_code"] == webhooks_module.INVALID_SIGNATURE
        assert patched_webhook.calls == []

    async def test_secreto_sin_configurar_rechaza_todo(
        self, api_client: Any, patched_webhook: FakeTask, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con `TELEGRAM_WEBHOOK_SECRET` vacio, el endpoint no queda abierto."""
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "TELEGRAM_WEBHOOK_SECRET", "")

        response = await api_client.post(
            self.URL, json=self.UPDATE, headers={"X-Telegram-Bot-Api-Secret-Token": ""}
        )

        assert response.status_code == 401
        assert patched_webhook.calls == []

    async def test_update_de_grupo_responde_200_y_no_se_encola(
        self, api_client: Any, patched_webhook: FakeTask
    ) -> None:
        """Telegram reintenta lo que no recibe con 200: un grupo no debe provocar reintentos."""
        update = {
            **self.UPDATE,
            "message": {**self.UPDATE["message"], "chat": {"id": -1, "type": "group"}},
        }

        response = await api_client.post(
            self.URL, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": self.SECRETO}
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ignored"}
        assert patched_webhook.calls == []

    async def test_el_mismo_update_dos_veces_se_encola_una_sola(
        self, api_client: Any, patched_webhook: FakeTask
    ) -> None:
        """Telegram reenvia un update si tarda en recibir el 200."""
        headers = {"X-Telegram-Bot-Api-Secret-Token": self.SECRETO}

        await api_client.post(self.URL, json=self.UPDATE, headers=headers)
        segunda = await api_client.post(self.URL, json=self.UPDATE, headers=headers)

        assert segunda.json() == {"status": "duplicate"}
        assert len(patched_webhook.calls) == 1


# ─── POST: Email (Inbound Parse), con el provider real ───────────────────────


def _basic(secreto: str) -> dict[str, str]:
    """Cabecera `Authorization: Basic` para `usuario:password`."""
    import base64

    return {"Authorization": "Basic " + base64.b64encode(secreto.encode()).decode()}


class TestWebhookEmail:
    """El Inbound Parse llega como formulario, no como JSON, y no va firmado."""

    URL = "/api/v1/webhooks/email/email"
    SECRETO = "hook:una-password-larga-123"
    CAMPOS: ClassVar[dict[str, str]] = {
        "from": "Juan Perez <juan@example.com>",
        "to": "soporte@empresa.com",
        "subject": "Consulta de precios",
        "text": "Hola, quiero saber el precio del plan Pro.",
        "headers": "Message-ID: <abc123@mail.example.com>\nDate: Mon, 14 Nov 2023 22:13:20 +0000\n",
    }

    @pytest.fixture(autouse=True)
    def _ajustes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Configura secreto y direccion propia sin tocar el `.env`."""
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "EMAIL_INBOUND_WEBHOOK_SECRET", self.SECRETO)
        monkeypatch.setattr(get_settings(), "EMAIL_FROM_ADDRESS", "soporte@empresa.com")

    async def test_formulario_urlencoded_se_encola(
        self, api_client: Any, patched_webhook: FakeTask
    ) -> None:
        """Con las credenciales de la URL, el email llega a Celery ya normalizado."""
        response = await api_client.post(self.URL, data=self.CAMPOS, headers=_basic(self.SECRETO))

        assert response.status_code == 200
        assert response.json() == {"status": "queued"}
        (llamada,) = patched_webhook.calls
        assert llamada["provider"] == "email"
        mensaje = llamada["normalized_message"]
        assert mensaje["channel"] == "email"
        assert mensaje["sender_identifier"] == "juan@example.com"
        assert mensaje["sender_name"] == "Juan Perez"
        assert mensaje["external_message_id"] == "<abc123@mail.example.com>"
        assert mensaje["text"].startswith("Asunto: Consulta de precios")

    async def test_multipart_con_adjunto_se_serializa_con_su_metadata(
        self, api_client: Any, patched_webhook: FakeTask
    ) -> None:
        """SendGrid manda `multipart/form-data` y los adjuntos son archivos.

        Un `UploadFile` en el payload no se serializa a Celery tal cual — pero su
        nombre, tipo y tamano si llegan como texto (ADR-062).
        """
        import json

        campos = {clave: (None, valor) for clave, valor in self.CAMPOS.items()}
        campos["attachment1"] = ("factura.pdf", b"%PDF-1.4 contenido", "application/pdf")

        response = await api_client.post(self.URL, files=campos, headers=_basic(self.SECRETO))

        assert response.status_code == 200
        assert response.json() == {"status": "queued"}
        (llamada,) = patched_webhook.calls
        json.dumps(llamada["normalized_message"])  # revienta si quedo un UploadFile
        mensaje = llamada["normalized_message"]
        assert mensaje["raw_payload"]["attachments"] == [
            {"filename": "factura.pdf", "content_type": "application/pdf", "size": 18}
        ]
        assert "[Adjunto(s): factura.pdf (18 B)]" in mensaje["text"]

    async def test_el_provider_recibe_metadata_del_adjunto_no_el_archivo(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch, patched_webhook: FakeTask
    ) -> None:
        """El `UploadFile` no llega a `parse_webhook`; su metadata si, aparte.

        Cualquier provider que guarde `raw_payload` tal cual lo mandaria a
        Celery, que no sabe serializar un `UploadFile`. Lo que SI llega es
        `_attachments`, ya reducido a texto (`app/api/v1/webhooks.py::_leer_adjuntos`).
        """
        recibido: list[dict[str, Any]] = []

        class EspiaProvider(FakeProvider):
            async def parse_webhook(self, raw_payload: dict[str, Any]) -> FakeNormalized:
                recibido.append(raw_payload)
                return self.normalized

        _use_provider(monkeypatch, EspiaProvider())
        campos = {clave: (None, valor) for clave, valor in self.CAMPOS.items()}
        campos["attachment1"] = ("factura.pdf", b"%PDF-1.4 contenido", "application/pdf")

        response = await api_client.post(self.URL, files=campos, headers=_basic(self.SECRETO))

        assert response.status_code == 200
        assert set(recibido[0]) == set(self.CAMPOS) | {"_attachments"}
        assert all(isinstance(recibido[0][clave], str) for clave in self.CAMPOS)
        assert recibido[0]["_attachments"] == [
            {"filename": "factura.pdf", "content_type": "application/pdf", "size": 18}
        ]

    async def test_sin_adjuntos_no_agrega_la_clave(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch, patched_webhook: FakeTask
    ) -> None:
        """Un multipart normal (sin archivos) no gana un `_attachments` vacio."""
        recibido: list[dict[str, Any]] = []

        class EspiaProvider(FakeProvider):
            async def parse_webhook(self, raw_payload: dict[str, Any]) -> FakeNormalized:
                recibido.append(raw_payload)
                return self.normalized

        _use_provider(monkeypatch, EspiaProvider())

        response = await api_client.post(self.URL, data=self.CAMPOS, headers=_basic(self.SECRETO))

        assert response.status_code == 200
        assert "_attachments" not in recibido[0]

    async def test_no_extrae_adjuntos_de_otros_proveedores(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch, patched_webhook: FakeTask
    ) -> None:
        """Solo email manda multipart; los demas nunca deberian ganar `_attachments`."""
        recibido: list[dict[str, Any]] = []

        class EspiaProvider(FakeProvider):
            async def parse_webhook(self, raw_payload: dict[str, Any]) -> FakeNormalized:
                recibido.append(raw_payload)
                return self.normalized

        _use_provider(monkeypatch, EspiaProvider())
        campos = {clave: (None, valor) for clave, valor in self.CAMPOS.items()}
        campos["attachment1"] = ("factura.pdf", b"%PDF-1.4 contenido", "application/pdf")

        await api_client.post(
            "/api/v1/webhooks/linkedin/linkedin", files=campos, headers=_basic(self.SECRETO)
        )

        assert "_attachments" not in recibido[0]

    async def test_mas_adjuntos_que_el_limite_se_recortan(
        self,
        api_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        patched_webhook: FakeTask,
    ) -> None:
        """Un email con mas adjuntos de la cuenta no debe crecer sin control."""
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "EMAIL_MAX_ATTACHMENTS", 2)
        recibido: list[dict[str, Any]] = []

        class EspiaProvider(FakeProvider):
            async def parse_webhook(self, raw_payload: dict[str, Any]) -> FakeNormalized:
                recibido.append(raw_payload)
                return self.normalized

        _use_provider(monkeypatch, EspiaProvider())
        campos = {clave: (None, valor) for clave, valor in self.CAMPOS.items()}
        campos["attachment1"] = ("uno.pdf", b"111", "application/pdf")
        campos["attachment2"] = ("dos.pdf", b"222", "application/pdf")
        campos["attachment3"] = ("tres.pdf", b"333", "application/pdf")

        await api_client.post(self.URL, files=campos, headers=_basic(self.SECRETO))

        assert len(recibido[0]["_attachments"]) == 2

    async def test_un_nombre_con_ruta_se_sanea(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch, patched_webhook: FakeTask
    ) -> None:
        """El nombre del adjunto lo elige el remitente: no debe colarse un path."""
        recibido: list[dict[str, Any]] = []

        class EspiaProvider(FakeProvider):
            async def parse_webhook(self, raw_payload: dict[str, Any]) -> FakeNormalized:
                recibido.append(raw_payload)
                return self.normalized

        _use_provider(monkeypatch, EspiaProvider())
        campos = {clave: (None, valor) for clave, valor in self.CAMPOS.items()}
        campos["attachment1"] = ("../../etc/passwd", b"x", "text/plain")

        await api_client.post(self.URL, files=campos, headers=_basic(self.SECRETO))

        nombre = recibido[0]["_attachments"][0]["filename"]
        assert "/" not in nombre
        assert ".." not in nombre

    async def test_un_adjunto_que_supera_el_limite_no_se_lee_completo(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch, patched_webhook: FakeTask
    ) -> None:
        """El conteo se corta apenas se supera el limite, no sigue leyendo el resto.

        El adjunto tiene que ser mayor a un trozo de lectura (64 KB): con uno mas
        chico, un solo `.read()` ya trae todo el archivo y no hay forma de
        distinguir "se corto" de "no habia mas que leer".
        """
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "EMAIL_MAX_ATTACHMENT_BYTES", 100)
        recibido: list[dict[str, Any]] = []

        class EspiaProvider(FakeProvider):
            async def parse_webhook(self, raw_payload: dict[str, Any]) -> FakeNormalized:
                recibido.append(raw_payload)
                return self.normalized

        _use_provider(monkeypatch, EspiaProvider())
        campos = {clave: (None, valor) for clave, valor in self.CAMPOS.items()}
        campos["attachment1"] = ("grande.pdf", b"x" * 200_000, "application/pdf")

        await api_client.post(self.URL, files=campos, headers=_basic(self.SECRETO))

        assert recibido[0]["_attachments"][0]["size"] < 150_000

    @pytest.mark.parametrize("cabeceras", [{}, _basic("hook:otra-password"), _basic(":")])
    async def test_sin_credenciales_correctas_es_401(
        self, api_client: Any, patched_webhook: FakeTask, cabeceras: dict[str, str]
    ) -> None:
        """El Inbound Parse no va firmado: sin Basic auth cualquiera inyectaria emails."""
        response = await api_client.post(self.URL, data=self.CAMPOS, headers=cabeceras)

        assert response.status_code == 401
        assert response.json()["error_code"] == webhooks_module.INVALID_SIGNATURE
        assert patched_webhook.calls == []

    async def test_secreto_sin_configurar_rechaza_todo(
        self, api_client: Any, patched_webhook: FakeTask, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con `EMAIL_INBOUND_WEBHOOK_SECRET` vacio, el endpoint no queda abierto."""
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "EMAIL_INBOUND_WEBHOOK_SECRET", "")

        response = await api_client.post(self.URL, data=self.CAMPOS, headers=_basic(":"))

        assert response.status_code == 401
        assert patched_webhook.calls == []

    async def test_una_autorespuesta_responde_200_y_no_se_encola(
        self, api_client: Any, patched_webhook: FakeTask
    ) -> None:
        """Contestar a un autorespondedor es un bucle infinito."""
        campos = {
            **self.CAMPOS,
            "headers": self.CAMPOS["headers"] + "Auto-Submitted: auto-replied\n",
        }

        response = await api_client.post(self.URL, data=campos, headers=_basic(self.SECRETO))

        assert response.status_code == 200
        assert response.json() == {"status": "ignored"}
        assert patched_webhook.calls == []

    async def test_el_mismo_email_dos_veces_se_encola_una_sola(
        self, api_client: Any, patched_webhook: FakeTask
    ) -> None:
        """SendGrid y Mailgun reentregan si tardan en recibir el 200."""
        cabeceras = _basic(self.SECRETO)

        await api_client.post(self.URL, data=self.CAMPOS, headers=cabeceras)
        segunda = await api_client.post(self.URL, data=self.CAMPOS, headers=cabeceras)

        assert segunda.json() == {"status": "duplicate"}
        assert len(patched_webhook.calls) == 1

    async def test_un_cuerpo_que_declara_mas_del_tope_es_413_sin_leerlo(
        self, api_client: Any, patched_webhook: FakeTask
    ) -> None:
        """El endpoint lee el cuerpo entero *antes* de autenticar.

        Sin tope, cualquiera podria hacerlo cargar cientos de MB en memoria.
        """
        cabeceras = {
            **_basic(self.SECRETO),
            "content-length": str(webhooks_module.MAX_WEBHOOK_BODY_BYTES + 1),
        }

        response = await api_client.post(self.URL, data=self.CAMPOS, headers=cabeceras)

        assert response.status_code == 413
        assert response.json()["error_code"] == webhooks_module.PAYLOAD_TOO_LARGE
        assert patched_webhook.calls == []

    async def test_json_sigue_funcionando_para_los_demas_proveedores(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch, patched_webhook: FakeTask
    ) -> None:
        """La lectura por tipo de contenido no cambia lo que ya funcionaba."""
        _use_provider(monkeypatch, FakeProvider())

        response = await api_client.post(WEBHOOK_URL, json={"object": "instagram"})

        assert response.json() == {"status": "queued"}

    async def test_un_json_que_no_es_un_objeto_es_parse_error(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch, patched_webhook: FakeTask
    ) -> None:
        """Una lista o un numero no son un payload: 200 sin reintento, no un 500."""
        _use_provider(monkeypatch, FakeProvider())

        response = await api_client.post(WEBHOOK_URL, json=[1, 2, 3])

        assert response.status_code == 200
        assert response.json() == {"status": "parse_error"}


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
            dedup_module.build_dedup_key("whatsapp", "wamid.X") == "webhook_dedup:whatsapp:wamid.X"
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

    async def test_cliente_redis_es_unico_por_proceso(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El worker de Celery no tiene app.state: el cliente se cachea en el modulo."""
        monkeypatch.setattr(dedup_module, "_redis_client", None, raising=False)

        primero = dedup_module.get_redis()
        segundo = dedup_module.get_redis()

        assert primero is segundo
        await dedup_module.close_redis()
        assert dedup_module._redis_client is None

    async def test_close_redis_sin_cliente_no_falla(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cerrar dos veces (o sin haber abierto) es inofensivo."""
        monkeypatch.setattr(dedup_module, "_redis_client", None, raising=False)

        await dedup_module.close_redis()

    async def test_redis_caido_no_bloquea_el_mensaje(self) -> None:
        """Fail-open: si Redis falla se procesa igual y decide PostgreSQL."""

        class BrokenRedis:
            async def set(self, *args: Any, **kwargs: Any) -> bool:
                raise ConnectionError("redis caido")

        resultado = await dedup_module.mark_if_new("whatsapp", "id-3", redis_client=BrokenRedis())

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


class TestWebchatNoEsUnWebhook:
    """El Webchat entra por su WebSocket; el endpoint HTTP generico no puede aceptarlo.

    La factory registra `webchat`, asi que `POST /webhooks/webchat/webchat` resuelve el
    provider. Si su `validate_signature` devolviera `True` (como proponia la spec),
    cualquiera podria inyectar mensajes como cualquier visitante por HTTP.
    """

    @pytest.mark.parametrize(
        "cabeceras",
        [{}, {"X-Ycloud-Signature": "cualquiera"}, {"Authorization": "Basic Zm9vOmJhcg=="}],
    )
    async def test_ningun_post_es_aceptado(
        self, api_client: Any, patched_webhook: FakeTask, cabeceras: dict[str, str]
    ) -> None:
        cuerpo = {"visitor_id": "a" * 32, "message_id": "m1", "text": "hola"}

        response = await api_client.post(
            "/api/v1/webhooks/webchat/webchat", json=cuerpo, headers=cabeceras
        )

        assert response.status_code == 401
        assert response.json()["error_code"] == webhooks_module.INVALID_SIGNATURE
        assert patched_webhook.calls == []
