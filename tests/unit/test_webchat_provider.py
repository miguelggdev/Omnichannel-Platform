"""Tests del `WebchatProvider`: normalizacion, entrega por Redis y no-webhook."""

import json
import uuid
from typing import Any

import pytest

from app.agents.nodes._tenant import CHANNEL_PROVIDERS, get_channel_config
from app.core.config import get_settings
from app.services.messaging import webchat as wc
from app.services.messaging.base import (
    MessageContent,
    MessagingProvider,
    TemplateMessage,
    TemplateNotSupportedError,
)
from app.services.messaging.factory import get_messaging_provider
from app.services.messaging.webchat import WebchatProvider, canal_de_salida

TENANT = uuid.uuid4()
VISITANTE = "a" * 32


class FakeRedis:
    """Redis falso que registra lo publicado."""

    def __init__(self) -> None:
        self.publicados: list[tuple[str, str]] = []

    async def publish(self, canal: str, mensaje: str) -> int:
        self.publicados.append((canal, mensaje))
        return 0


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """Sustituye el cliente Redis del provider."""
    falso = FakeRedis()
    monkeypatch.setattr(wc, "get_redis", lambda: falso)
    return falso


@pytest.fixture
def provider() -> WebchatProvider:
    return WebchatProvider()


class TestParse:
    async def test_normaliza_un_mensaje(self, provider: WebchatProvider) -> None:
        normalizado = await provider.parse_webhook(
            {"visitor_id": VISITANTE, "message_id": "m1", "text": "hola", "name": "Ada"}
        )

        assert normalizado.channel.value == "webchat"
        assert normalizado.sender_identifier == VISITANTE
        assert normalizado.sender_name == "Ada"
        assert normalizado.text == "hola"

    async def test_el_id_externo_lleva_el_visitante(self, provider: WebchatProvider) -> None:
        """Dos visitantes con el mismo `message_id` no chocan en la deduplicacion."""
        uno = await provider.parse_webhook({"visitor_id": "1" * 32, "message_id": "m", "text": "x"})
        otro = await provider.parse_webhook(
            {"visitor_id": "2" * 32, "message_id": "m", "text": "x"}
        )

        assert uno.external_message_id != otro.external_message_id
        assert uno.external_message_id == f"{'1' * 32}:m"

    async def test_un_boton_llega_como_texto_y_respuesta_interactiva(
        self, provider: WebchatProvider
    ) -> None:
        normalizado = await provider.parse_webhook(
            {
                "visitor_id": VISITANTE,
                "message_id": "b1",
                "button": {"id": "confirmar", "title": "Si, confirmo"},
            }
        )

        assert normalizado.text == "Si, confirmo"
        assert normalizado.interactive_response == {
            "type": "button_reply",
            "id": "confirmar",
            "title": "Si, confirmo",
        }

    async def test_el_payload_guardado_es_compacto(self, provider: WebchatProvider) -> None:
        """Acaba en `messages.metadata` y en la cola: sin el texto duplicado."""
        normalizado = await provider.parse_webhook(
            {"visitor_id": VISITANTE, "message_id": "m1", "text": "secreto", "name": "Ada"}
        )

        assert normalizado.raw_payload == {"visitor_id": VISITANTE, "message_id": "m1"}

    @pytest.mark.parametrize(
        "payload",
        [
            {"message_id": "m", "text": "x"},
            {"visitor_id": VISITANTE, "text": "x"},
            {"visitor_id": "", "message_id": "m", "text": "x"},
            {},
        ],
    )
    async def test_sin_visitante_o_id_se_rechaza(
        self, provider: WebchatProvider, payload: dict[str, Any]
    ) -> None:
        with pytest.raises(ValueError, match="visitor_id o message_id"):
            await provider.parse_webhook(payload)


class TestNoEsUnWebhook:
    @pytest.mark.parametrize("firma", ["", "cualquiera", "sha256=abc"])
    @pytest.mark.parametrize("secreto", ["", "un-secreto"])
    async def test_ninguna_firma_es_valida(
        self, provider: WebchatProvider, firma: str, secreto: str
    ) -> None:
        """La spec devolvia `True`: cualquiera habria inyectado mensajes por HTTP."""
        assert not await provider.validate_signature(b"{}", firma, secreto)

    def test_esta_registrado_en_la_factory(self) -> None:
        assert isinstance(get_messaging_provider("webchat", {}), WebchatProvider)
        assert isinstance(get_messaging_provider("webchat"), MessagingProvider)

    def test_el_canal_apunta_a_su_provider(self) -> None:
        assert CHANNEL_PROVIDERS["webchat"] == "webchat"


class TestEnvio:
    async def test_publica_en_el_canal_del_visitante(
        self, provider: WebchatProvider, redis: FakeRedis
    ) -> None:
        message_id = await provider.send_message(
            VISITANTE, MessageContent(text="Hola"), {"client_id": str(TENANT)}
        )

        (canal, cuerpo) = redis.publicados[0]
        frame = json.loads(cuerpo)
        assert canal == canal_de_salida(TENANT, VISITANTE)
        assert frame["type"] == "message"
        assert frame["text"] == "Hola"
        assert frame["message_id"] == message_id
        assert "timestamp" in frame

    async def test_el_canal_incluye_el_tenant(self) -> None:
        """Dos tenants no comparten canal aunque un `visitor_id` coincidiera."""
        assert canal_de_salida(TENANT, VISITANTE) != canal_de_salida(uuid.uuid4(), VISITANTE)

    async def test_cada_mensaje_tiene_su_id(
        self, provider: WebchatProvider, redis: FakeRedis
    ) -> None:
        ids = {
            await provider.send_message(VISITANTE, MessageContent(text="x"), {"client_id": "t"})
            for _ in range(5)
        }

        assert len(ids) == 5

    async def test_sin_suscriptores_no_es_un_error(
        self, provider: WebchatProvider, redis: FakeRedis
    ) -> None:
        """Publicar a nadie devuelve 0: el mensaje igual se guarda y se recupera despues."""
        assert await provider.send_message(VISITANTE, MessageContent(text="x"), {"client_id": "t"})

    async def test_los_botones_van_en_el_frame(
        self, provider: WebchatProvider, redis: FakeRedis
    ) -> None:
        await provider.send_message(
            VISITANTE,
            MessageContent(text="¿Confirmas?", buttons=[{"id": "si", "title": "Si"}]),
            {"client_id": "t"},
        )

        assert json.loads(redis.publicados[0][1])["buttons"] == [{"id": "si", "title": "Si"}]

    async def test_no_manda_mas_botones_de_los_permitidos(
        self, provider: WebchatProvider, redis: FakeRedis
    ) -> None:
        botones = [{"id": str(i), "title": str(i)} for i in range(20)]

        await provider.send_message(
            VISITANTE, MessageContent(text="x", buttons=botones), {"client_id": "t"}
        )

        assert len(json.loads(redis.publicados[0][1])["buttons"]) == wc.MAX_BUTTONS

    async def test_sin_botones_no_hay_clave_buttons(
        self, provider: WebchatProvider, redis: FakeRedis
    ) -> None:
        await provider.send_message(VISITANTE, MessageContent(text="x"), {"client_id": "t"})

        assert "buttons" not in json.loads(redis.publicados[0][1])

    async def test_el_media_no_se_soporta(
        self, provider: WebchatProvider, redis: FakeRedis
    ) -> None:
        with pytest.raises(ValueError, match="media"):
            await provider.send_message(
                VISITANTE,
                MessageContent(text="x", media_url="https://x/y.png", media_type="image"),
                {"client_id": "t"},
            )

        assert redis.publicados == []

    async def test_sin_client_id_falla_en_vez_de_publicar_en_un_canal_sin_tenant(
        self, provider: WebchatProvider, redis: FakeRedis
    ) -> None:
        with pytest.raises(KeyError):
            await provider.send_message(VISITANTE, MessageContent(text="x"), {})

        assert redis.publicados == []


class TestContrato:
    async def test_no_hay_templates(self, provider: WebchatProvider) -> None:
        with pytest.raises(TemplateNotSupportedError):
            await provider.send_template("v", TemplateMessage("t", "es", []), {})

    def test_restricciones(self, provider: WebchatProvider) -> None:
        c = provider.get_channel_constraints()

        assert c.session_window_hours is None
        assert not c.requires_template_outside_window
        assert c.supported_media_types == []
        assert c.max_text_length == wc.MAX_TEXT_LENGTH

    def test_get_channel_config_lleva_el_tenant_y_no_credenciales(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "DEFAULT_CLIENT_ID", str(TENANT))

        nombre, config = get_channel_config("webchat")

        assert nombre == "webchat"
        assert config == {"client_id": str(TENANT)}

    def test_sin_tenant_configurado_el_canal_no_esta_configurado(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.agents.nodes._tenant import ChannelNotConfiguredError

        monkeypatch.setattr(get_settings(), "DEFAULT_CLIENT_ID", "")

        with pytest.raises(ChannelNotConfiguredError):
            get_channel_config("webchat")
