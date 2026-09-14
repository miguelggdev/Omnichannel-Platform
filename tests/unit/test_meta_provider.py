"""Tests de MetaProvider — Instagram DM y Facebook Messenger.

`app/services/messaging/meta.py` es entrega de Dev A (matriz Sprint 4) y a la fecha
de este commit todavia no esta en `main`. El `importorskip` de abajo hace que este
modulo se omita hasta entonces y se active solo en cuanto Dev A lo mergee, sin tocar
una linea de estos tests.

El contrato verificado es el de `specs/sprint-04-webhooks.md` §4.
"""

import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest

from tests.fixtures.meta_payloads import (
    FACEBOOK_TEXT,
    INSTAGRAM_IMAGE,
    INSTAGRAM_QUICK_REPLY,
    INSTAGRAM_TEXT,
)

meta = pytest.importorskip(
    "app.services.messaging.meta",
    reason="MetaProvider lo entrega Dev A en Sprint 4; aun no esta en main",
)
base = pytest.importorskip(
    "app.services.messaging.base",
    reason="El ABC MessagingProvider lo entrega Dev A en Sprint 4",
)

APP_SECRET = "secreto-de-prueba"


def _provider(channel: str):
    """Instancia un MetaProvider para el sub-canal indicado."""
    return meta.MetaProvider(
        {
            "page_access_token": "token-de-prueba",
            "app_secret": APP_SECRET,
            "channel": channel,
        }
    )


def _firma(payload: bytes, secret: str = APP_SECRET) -> str:
    """Calcula el header x-hub-signature-256 tal como lo envia Meta."""
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class TestParseoInstagram:
    """Instagram Direct."""

    async def test_texto(self) -> None:
        """Un DM de texto se normaliza con canal, remitente, texto e id externo."""
        normalized = await _provider("instagram").parse_webhook(INSTAGRAM_TEXT)

        assert normalized.channel.value == "instagram"
        assert normalized.sender_identifier == "6789000000000001"
        assert normalized.text == "Hola, quiero informacion sobre los horarios"
        assert normalized.external_message_id == "aWc6bXNnOjAwMDAwMDAx"
        assert normalized.media_url is None

    async def test_imagen(self) -> None:
        """Un attachment de imagen llena media_url y media_type."""
        normalized = await _provider("instagram").parse_webhook(INSTAGRAM_IMAGE)

        assert normalized.media_url == "https://scontent.cdninstagram.com/v/ejemplo.jpg"
        assert normalized.media_type.value == "image"
        assert normalized.text is None

    async def test_quick_reply(self) -> None:
        """El payload del quick reply queda en interactive_response."""
        normalized = await _provider("instagram").parse_webhook(INSTAGRAM_QUICK_REPLY)

        assert normalized.interactive_response is not None
        assert normalized.interactive_response["payload"] == "AGENDAR_CITA"
        assert normalized.text == "Si, agendar"

    async def test_timestamp_en_utc(self) -> None:
        """Meta envia epoch en milisegundos; debe quedar como datetime con tz."""
        epoch_ms = INSTAGRAM_TEXT["entry"][0]["messaging"][0]["timestamp"]
        esperado = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)

        normalized = await _provider("instagram").parse_webhook(INSTAGRAM_TEXT)

        assert normalized.timestamp.tzinfo is not None
        assert normalized.timestamp == esperado


class TestParseoFacebook:
    """Facebook Messenger."""

    async def test_texto(self) -> None:
        """El mismo parser distingue el canal por la config del provider."""
        normalized = await _provider("facebook").parse_webhook(FACEBOOK_TEXT)

        assert normalized.channel.value == "facebook"
        assert normalized.sender_identifier == "5432000000000001"
        assert normalized.text == "Buenas tardes, siguen abiertos?"
        assert normalized.external_message_id == "bWlkLjAwMDAwMDA0"


class TestFirmaHmac:
    """Validacion de x-hub-signature-256."""

    async def test_firma_valida(self) -> None:
        """Una firma bien calculada valida."""
        body = json.dumps(INSTAGRAM_TEXT).encode()

        assert await _provider("instagram").validate_signature(body, _firma(body), APP_SECRET)

    async def test_firma_de_otro_secreto_no_valida(self) -> None:
        """Una firma calculada con otro app_secret se rechaza."""
        body = json.dumps(INSTAGRAM_TEXT).encode()
        firma_ajena = _firma(body, "otro-secreto")

        assert not await _provider("instagram").validate_signature(body, firma_ajena, APP_SECRET)

    async def test_cuerpo_alterado_no_valida(self) -> None:
        """Si el cuerpo cambia despues de firmar, la firma deja de valer."""
        body = json.dumps(INSTAGRAM_TEXT).encode()
        firma = _firma(body)

        assert not await _provider("instagram").validate_signature(b'{"otro":1}', firma, APP_SECRET)

    async def test_sin_prefijo_sha256_no_valida(self) -> None:
        """Meta siempre prefija 'sha256='; un hex pelado se rechaza."""
        body = json.dumps(INSTAGRAM_TEXT).encode()
        hex_pelado = _firma(body).removeprefix("sha256=")

        assert not await _provider("instagram").validate_signature(body, hex_pelado, APP_SECRET)

    async def test_firma_vacia_no_valida(self) -> None:
        """Sin header de firma no se procesa nada."""
        body = json.dumps(INSTAGRAM_TEXT).encode()

        assert not await _provider("instagram").validate_signature(body, "", APP_SECRET)


class TestConstraintsPorCanal:
    """Cada sub-canal declara sus propios limites."""

    def test_instagram(self) -> None:
        """Instagram: 1000 chars, ventana de 24h, sin listas."""
        constraints = _provider("instagram").get_channel_constraints()

        assert constraints.max_text_length == 1000
        assert constraints.session_window_hours == 24
        assert constraints.max_list_items == 0
        assert "document" not in constraints.supported_media_types

    def test_facebook(self) -> None:
        """Facebook Messenger: 2000 chars y soporta documentos."""
        constraints = _provider("facebook").get_channel_constraints()

        assert constraints.max_text_length == 2000
        assert constraints.session_window_hours == 24
        assert "document" in constraints.supported_media_types


class TestTemplates:
    """Instagram no soporta templates."""

    async def test_send_template_en_instagram_lanza_not_implemented(self) -> None:
        """Debe fallar explicito, no enviar un mensaje distinto al pedido."""
        template = base.TemplateMessage(
            template_name="recordatorio_cita",
            language="es",
            components=[],
        )

        with pytest.raises(NotImplementedError):
            await _provider("instagram").send_template(
                "6789000000000001", template, {"page_access_token": "token-de-prueba"}
            )
