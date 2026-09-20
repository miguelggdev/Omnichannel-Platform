"""Tests de `WebchatProvider` y de la firma de sesiones (Sprint 9, Dev B).

Redis se sustituye por un doble que registra lo publicado: no hay servidor.
"""

import json
from typing import Any
from uuid import uuid4

import pytest

from app.schemas.message import ChannelEnum, MessageTypeEnum
from app.services.messaging import webchat as webchat_module
from app.services.messaging.base import (
    IgnoredWebhookError,
    MessageContent,
    TemplateMessage,
    TemplateNotSupportedError,
)
from app.services.messaging.webchat import (
    EXTERNAL_ID_PREFIX,
    MAX_TEXT_LENGTH,
    WebchatProvider,
    canal_de_sesion,
    firmar_sesion,
    nueva_sesion,
    nuevo_id_externo,
    sesion_de_token,
)

CLIENT_ID = "11111111-1111-1111-1111-111111111111"


class RedisDoble:
    """Cliente Redis minimo: guarda lo publicado y dice cuantos escuchaban."""

    def __init__(self, suscriptores: int = 1) -> None:
        """Inicializa el doble.

        Args:
            suscriptores: Cuantos suscriptores simula que hay en el canal.
        """
        self.suscriptores = suscriptores
        self.publicado: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, canal: str, datos: str) -> int:
        """Registra la publicacion.

        Args:
            canal: Canal de Redis.
            datos: Cuerpo serializado.

        Returns:
            Numero de suscriptores simulados.
        """
        self.publicado.append((canal, json.loads(datos)))
        return self.suscriptores


@pytest.fixture
def provider() -> WebchatProvider:
    """Provider sin config: la factory lo construye sin argumentos."""
    return WebchatProvider()


@pytest.fixture
def redis_doble(monkeypatch: pytest.MonkeyPatch) -> RedisDoble:
    """Sustituye el cliente Redis del servicio por el doble."""
    doble = RedisDoble()
    monkeypatch.setattr("app.services.dedup.get_redis", lambda: doble)
    return doble


def _frame(**extra: Any) -> dict[str, Any]:
    """Arma el payload que el endpoint WebSocket le pasa al provider.

    Args:
        **extra: Campos que sustituyen o suman a los de un mensaje de texto.

    Returns:
        El payload completo.
    """
    payload: dict[str, Any] = {
        "type": "message",
        "session_id": "a" * 32,
        "external_message_id": "wc-abc123",
        "text": "Hola, quiero una cita",
    }
    payload.update(extra)
    return payload


# ─── Firma de la sesion ──────────────────────────────────────────────────────


class TestFirmaDeSesion:
    """La firma es lo unico que impide reclamar la sesion de otro visitante."""

    def test_el_token_emitido_se_verifica(self) -> None:
        session_id, token = nueva_sesion()
        assert sesion_de_token(token) == session_id

    def test_dos_sesiones_nuevas_no_coinciden(self) -> None:
        primera, _ = nueva_sesion()
        segunda, _ = nueva_sesion()
        assert primera != segunda

    def test_un_session_id_sin_firma_se_rechaza(self) -> None:
        session_id, _ = nueva_sesion()
        assert sesion_de_token(session_id) is None

    def test_una_firma_ajena_se_rechaza(self) -> None:
        """El caso real: reconectar con el id de otro y la firma propia."""
        victima, _ = nueva_sesion()
        _, token_propio = nueva_sesion()
        firma_propia = token_propio.split(".", 1)[1]
        assert sesion_de_token(f"{victima}.{firma_propia}") is None

    def test_una_firma_manipulada_se_rechaza(self) -> None:
        _session_id, token = nueva_sesion()
        manipulado = token[:-1] + ("A" if token[-1] != "A" else "B")
        assert sesion_de_token(manipulado) is None

    @pytest.mark.parametrize("valor", [None, "", ".", "sinpunto", ".solofirma"])
    def test_tokens_degenerados_se_rechazan(self, valor: str | None) -> None:
        assert sesion_de_token(valor) is None

    def test_la_firma_no_sirve_como_token_de_acceso(self) -> None:
        """La etiqueta de dominio separa esta firma de cualquier otra del mismo secreto."""
        session_id = uuid4().hex
        import hmac
        from hashlib import sha256

        from app.core.config import get_settings

        sin_dominio = hmac.new(
            get_settings().JWT_SECRET.encode("utf-8"), session_id.encode("utf-8"), sha256
        ).hexdigest()
        assert sesion_de_token(f"{session_id}.{sin_dominio}") is None
        assert firmar_sesion(session_id) != f"{session_id}.{sin_dominio}"


class TestCanalDeSesion:
    """El canal de Redis lleva el tenant, no solo la sesion."""

    def test_incluye_tenant_y_sesion(self) -> None:
        assert canal_de_sesion(CLIENT_ID, "sesion") == f"webchat:out:{CLIENT_ID}:sesion"

    def test_dos_tenants_no_comparten_canal(self) -> None:
        otro = "22222222-2222-2222-2222-222222222222"
        assert canal_de_sesion(CLIENT_ID, "s") != canal_de_sesion(otro, "s")


class TestIdExterno:
    """Los ids los genera el servidor, nunca el cliente."""

    def test_lleva_prefijo_y_es_unico(self) -> None:
        primero, segundo = nuevo_id_externo(), nuevo_id_externo()
        assert primero.startswith(EXTERNAL_ID_PREFIX)
        assert primero != segundo


# ─── Recepcion ───────────────────────────────────────────────────────────────


class TestParseWebhook:
    """Normalizacion de un frame del widget."""

    @pytest.mark.asyncio
    async def test_mensaje_de_texto(self, provider: WebchatProvider) -> None:
        normalizado = await provider.parse_webhook(_frame())

        assert normalizado.channel == ChannelEnum.webchat
        assert normalizado.sender_identifier == "a" * 32
        assert normalizado.text == "Hola, quiero una cita"
        assert normalizado.external_message_id == "wc-abc123"
        assert normalizado.media_type == MessageTypeEnum.text
        assert normalizado.timestamp.tzinfo is not None

    @pytest.mark.asyncio
    async def test_recorta_los_espacios_del_texto(self, provider: WebchatProvider) -> None:
        normalizado = await provider.parse_webhook(_frame(text="  hola  "))
        assert normalizado.text == "hola"

    @pytest.mark.asyncio
    async def test_sin_id_externo_genera_uno(self, provider: WebchatProvider) -> None:
        payload = _frame()
        del payload["external_message_id"]
        normalizado = await provider.parse_webhook(payload)
        assert normalizado.external_message_id.startswith(EXTERNAL_ID_PREFIX)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tipo", ["typing", "read_receipt", "ping"])
    async def test_los_frames_que_no_son_mensaje_se_ignoran(
        self, provider: WebchatProvider, tipo: str
    ) -> None:
        with pytest.raises(IgnoredWebhookError):
            await provider.parse_webhook(_frame(type=tipo))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("texto", ["", "   ", None])
    async def test_un_frame_sin_texto_se_ignora(
        self, provider: WebchatProvider, texto: str | None
    ) -> None:
        with pytest.raises(IgnoredWebhookError):
            await provider.parse_webhook(_frame(text=texto))

    @pytest.mark.asyncio
    async def test_sin_session_id_de_la_conexion_es_un_error(
        self, provider: WebchatProvider
    ) -> None:
        """No es un descarte legitimo: el endpoint siempre lo agrega."""
        payload = _frame()
        del payload["session_id"]
        with pytest.raises(ValueError, match="session_id"):
            await provider.parse_webhook(payload)

    @pytest.mark.asyncio
    async def test_el_adjunto_del_visitante_se_descarta(self, provider: WebchatProvider) -> None:
        """Sin endpoint de subida, un media_url del cliente es una URL arbitraria."""
        normalizado = await provider.parse_webhook(
            _frame(media_url="http://169.254.169.254/latest/meta-data/", media_type="image")
        )
        assert normalizado.media_url is None
        assert normalizado.media_type == MessageTypeEnum.text


class TestValidateSignature:
    """Webchat no entra por el endpoint HTTP de webhooks."""

    @pytest.mark.asyncio
    async def test_rechaza_siempre(self, provider: WebchatProvider) -> None:
        assert await provider.validate_signature(b"{}", "", "") is False

    @pytest.mark.asyncio
    async def test_rechaza_aunque_la_firma_parezca_valida(self, provider: WebchatProvider) -> None:
        assert await provider.validate_signature(b"{}", "sha256=loquesea", "secreto") is False


# ─── Envio ───────────────────────────────────────────────────────────────────


class TestSendMessage:
    """Lo saliente se publica en el canal Redis de la sesion."""

    @pytest.mark.asyncio
    async def test_publica_el_texto_en_el_canal_de_la_sesion(
        self, provider: WebchatProvider, redis_doble: RedisDoble
    ) -> None:
        external_id = await provider.send_message(
            to="sesion-1",
            content=MessageContent(text="Claro que si"),
            channel_config={"client_id": CLIENT_ID},
        )

        canal, frame = redis_doble.publicado[0]
        assert canal == canal_de_sesion(CLIENT_ID, "sesion-1")
        assert frame["type"] == "message"
        assert frame["text"] == "Claro que si"
        assert frame["message_id"] == external_id
        assert external_id.startswith(EXTERNAL_ID_PREFIX)

    @pytest.mark.asyncio
    async def test_incluye_media_y_botones(
        self, provider: WebchatProvider, redis_doble: RedisDoble
    ) -> None:
        await provider.send_message(
            to="sesion-1",
            content=MessageContent(
                text="Mira esto",
                media_url="https://cdn.example.com/a.png",
                media_type="image",
                caption="pie",
                buttons=[{"id": "si", "title": "Si"}],
            ),
            channel_config={"client_id": CLIENT_ID},
        )

        _canal, frame = redis_doble.publicado[0]
        assert frame["media_url"] == "https://cdn.example.com/a.png"
        assert frame["media_type"] == "image"
        assert frame["caption"] == "pie"
        assert frame["buttons"] == [{"id": "si", "title": "Si"}]

    @pytest.mark.asyncio
    async def test_sin_suscriptores_no_es_un_error(
        self, provider: WebchatProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El visitante cerro la pestana: el mensaje se le repone al reconectar."""
        doble = RedisDoble(suscriptores=0)
        monkeypatch.setattr("app.services.dedup.get_redis", lambda: doble)

        external_id = await provider.send_message(
            to="sesion-1",
            content=MessageContent(text="sigue ahi?"),
            channel_config={"client_id": CLIENT_ID},
        )
        assert external_id.startswith(EXTERNAL_ID_PREFIX)
        assert doble.publicado

    @pytest.mark.asyncio
    async def test_dos_envios_no_comparten_id(
        self, provider: WebchatProvider, redis_doble: RedisDoble
    ) -> None:
        config = {"client_id": CLIENT_ID}
        primero = await provider.send_message("s", MessageContent(text="a"), config)
        segundo = await provider.send_message("s", MessageContent(text="b"), config)
        assert primero != segundo


class TestSendTemplate:
    """Webchat no tiene templates preaprobados."""

    @pytest.mark.asyncio
    async def test_lanza_template_not_supported(self, provider: WebchatProvider) -> None:
        with pytest.raises(TemplateNotSupportedError):
            await provider.send_template(
                "s", TemplateMessage(template_name="t", language="es", components=[]), {}
            )


class TestConstraints:
    """El canal no tiene ventana: el agente puede escribir cuando quiera."""

    def test_sin_ventana_y_sin_templates(self, provider: WebchatProvider) -> None:
        limites = provider.get_channel_constraints()
        assert limites.session_window_hours is None
        assert limites.requires_template_outside_window is False
        assert limites.max_text_length == MAX_TEXT_LENGTH


class TestRegistroEnElProyecto:
    """El canal tiene que estar cableado de punta a punta, no solo existir."""

    def test_la_factory_lo_construye_sin_argumentos(self) -> None:
        from app.services.messaging.factory import get_messaging_provider

        assert isinstance(get_messaging_provider("webchat"), WebchatProvider)

    def test_get_channel_config_exige_el_tenant(self) -> None:
        from app.agents.nodes._tenant import ChannelNotConfiguredError, get_channel_config

        with pytest.raises(ChannelNotConfiguredError):
            get_channel_config("webchat")

    def test_get_channel_config_devuelve_el_tenant(self) -> None:
        from uuid import UUID

        from app.agents.nodes._tenant import get_channel_config

        provider_name, config = get_channel_config("webchat", UUID(CLIENT_ID))
        assert provider_name == "webchat"
        assert config == {"client_id": CLIENT_ID}

    def test_el_modulo_no_importa_redis_al_cargarse(self) -> None:
        """El import de `get_redis` es perezoso: el modulo no debe atarse al loop."""
        assert not hasattr(webchat_module, "get_redis")
