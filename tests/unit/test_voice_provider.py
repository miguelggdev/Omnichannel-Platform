"""Tests de TwilioVoiceProvider (Sprint 13, Dev A)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlencode
from uuid import uuid4

import httpx
import pytest

from app.schemas.message import ChannelEnum
from app.services.messaging.base import (
    IgnoredWebhookError,
    MessageContent,
    MessagingProvider,
    TemplateMessage,
    TemplateNotSupportedError,
)
from app.services.messaging.factory import get_messaging_provider
from app.services.messaging.voice_provider import (
    TwilioAPIError,
    TwilioVoiceProvider,
    canal_de_salida,
    faltan_credenciales_salientes,
    numero_del_contacto,
    twilio_signature,
)

#: Vector de prueba de la libreria oficial `twilio-python`
#: (tests/unit/test_request_validator.py): mismo URL, parametros y token.
URL_TWILIO = "https://mycompany.com/myapp.php?foo=1&bar=2"
PARAMS_TWILIO = {
    "CallSid": "CA1234567890ABCDE",
    "Caller": "+14158675309",
    "Digits": "1234",
    "From": "+14158675309",
    "To": "+18005551212",
}
FIRMA_TWILIO = "RSOYDt4T1cUTdK1PDd93/VVr8B8="


def test_la_firma_coincide_con_el_vector_oficial_de_twilio() -> None:
    assert twilio_signature(URL_TWILIO, list(PARAMS_TWILIO.items()), "12345") == FIRMA_TWILIO


async def test_validate_signature_acepta_un_webhook_firmado() -> None:
    provider = TwilioVoiceProvider({"url": URL_TWILIO})
    cuerpo = urlencode(PARAMS_TWILIO).encode()
    assert await provider.validate_signature(cuerpo, FIRMA_TWILIO, "12345")


@pytest.mark.parametrize(
    ("url", "cuerpo", "firma", "secreto"),
    [
        # Un parametro alterado.
        (URL_TWILIO, {**PARAMS_TWILIO, "Digits": "9999"}, FIRMA_TWILIO, "12345"),
        # Otra URL: la firma cubre la URL, no solo el cuerpo.
        ("https://otro.com/myapp.php?foo=1&bar=2", PARAMS_TWILIO, FIRMA_TWILIO, "12345"),
        # Otro token.
        (URL_TWILIO, PARAMS_TWILIO, FIRMA_TWILIO, "otro"),
        # Sin firma o sin secreto configurado.
        (URL_TWILIO, PARAMS_TWILIO, "", "12345"),
        (URL_TWILIO, PARAMS_TWILIO, FIRMA_TWILIO, ""),
    ],
)
async def test_validate_signature_rechaza(
    url: str, cuerpo: dict[str, str], firma: str, secreto: str
) -> None:
    provider = TwilioVoiceProvider({"url": url})
    assert not await provider.validate_signature(urlencode(cuerpo).encode(), firma, secreto)


async def test_sin_url_nunca_valida() -> None:
    """Por el endpoint generico de webhooks no se puede inyectar una frase."""
    provider = get_messaging_provider("twilio", {"channel": "voice"})
    cuerpo = urlencode(PARAMS_TWILIO).encode()
    assert not await provider.validate_signature(cuerpo, FIRMA_TWILIO, "12345")


def test_la_factory_registra_twilio_y_cumple_el_abc() -> None:
    provider = get_messaging_provider("twilio", {"url": URL_TWILIO})
    assert isinstance(provider, TwilioVoiceProvider)
    assert isinstance(provider, MessagingProvider)
    assert provider.url == URL_TWILIO


def test_el_canal_voice_usa_twilio() -> None:
    from app.agents.nodes._tenant import CHANNEL_PROVIDERS

    assert CHANNEL_PROVIDERS["voice"] == "twilio"


# ─── parse_webhook ───────────────────────────────────────────────────────────


async def test_parse_una_frase_de_una_llamada_entrante() -> None:
    mensaje = await TwilioVoiceProvider().parse_webhook(
        {
            "CallSid": "CA1",
            "From": "+573001234567",
            "To": "+15005550006",
            "Direction": "inbound",
            "SpeechResult": "  quiero una cita  ",
            "Confidence": 0.9,
            "UtteranceIndex": 3,
        }
    )
    assert mensaje.channel == ChannelEnum.voice
    assert mensaje.sender_identifier == "+573001234567"
    assert mensaje.text == "quiero una cita"
    assert mensaje.external_message_id == "CA1:3"
    assert mensaje.verified_phone is None  # el caller ID se puede falsificar
    assert mensaje.raw_payload["call_sid"] == "CA1"


async def test_en_una_llamada_saliente_el_contacto_es_el_destino() -> None:
    mensaje = await TwilioVoiceProvider().parse_webhook(
        {
            "CallSid": "CA1",
            "From": "+15005550006",
            "To": "+573001234567",
            "Direction": "outbound-api",
            "SpeechResult": "si",
        }
    )
    assert mensaje.sender_identifier == "+573001234567"


async def test_cada_frase_tiene_su_propio_id_externo() -> None:
    """Con el CallSid solo, la deduplicacion descartaba la segunda frase."""
    provider = TwilioVoiceProvider()
    base = {"CallSid": "CA1", "From": "+573001234567", "SpeechResult": "hola"}
    uno = await provider.parse_webhook({**base, "UtteranceIndex": 1})
    dos = await provider.parse_webhook({**base, "UtteranceIndex": 2})
    assert uno.external_message_id != dos.external_message_id


async def test_los_digitos_cuentan_como_texto() -> None:
    mensaje = await TwilioVoiceProvider().parse_webhook(
        {"CallSid": "CA1", "From": "+573001234567", "Digits": "1"}
    )
    assert mensaje.text == "1"


async def test_un_evento_sin_voz_se_ignora() -> None:
    with pytest.raises(IgnoredWebhookError):
        await TwilioVoiceProvider().parse_webhook(
            {"CallSid": "CA1", "From": "+573001234567", "CallStatus": "ringing"}
        )


@pytest.mark.parametrize("faltante", ["CallSid", "From"])
async def test_sin_call_sid_o_numero_falla(faltante: str) -> None:
    payload = {"CallSid": "CA1", "From": "+573001234567", "SpeechResult": "hola"}
    payload.pop(faltante)
    with pytest.raises(ValueError, match="CallSid"):
        await TwilioVoiceProvider().parse_webhook(payload)


def test_numero_del_contacto_por_direccion() -> None:
    llamada = {"From": "+1", "To": "+2"}
    assert numero_del_contacto({**llamada, "Direction": "inbound"}) == "+1"
    assert numero_del_contacto({**llamada, "Direction": "outbound-dial"}) == "+2"
    assert numero_del_contacto(llamada) == "+1"


# ─── send_message ────────────────────────────────────────────────────────────


def test_el_canal_de_redis_no_lleva_el_telefono() -> None:
    tenant = uuid4()
    canal = canal_de_salida(tenant, "+573001234567")
    assert "3001234567" not in canal
    assert canal.startswith(f"voice:out:{tenant}:")
    assert canal == canal_de_salida(tenant, "+573001234567")
    assert canal != canal_de_salida(uuid4(), "+573001234567")


async def test_send_message_publica_para_la_llamada_activa() -> None:
    tenant = uuid4()
    redis = MagicMock()
    redis.publish = AsyncMock(return_value=1)
    with patch("app.services.messaging.voice_provider.get_redis", return_value=redis):
        message_id = await TwilioVoiceProvider().send_message(
            "+573001234567", MessageContent(text="Claro"), {"client_id": str(tenant)}
        )

    canal, frame = redis.publish.await_args.args
    assert canal == canal_de_salida(tenant, "+573001234567")
    assert '"type": "say"' in frame
    assert '"text": "Claro"' in frame
    assert message_id in frame


async def test_send_message_sin_llamada_activa_no_falla(caplog: Any) -> None:
    """Reintentar no la entregaria: el cliente colgo."""
    redis = MagicMock()
    redis.publish = AsyncMock(return_value=0)
    with patch("app.services.messaging.voice_provider.get_redis", return_value=redis):
        assert await TwilioVoiceProvider().send_message(
            "+573001234567", MessageContent(text="Claro"), {"client_id": str(uuid4())}
        )
    assert "sin llamada activa" in caplog.text


async def test_send_message_rechaza_media() -> None:
    with pytest.raises(ValueError, match="media"):
        await TwilioVoiceProvider().send_message(
            "+57300", MessageContent(media_url="https://x/y.png"), {"client_id": str(uuid4())}
        )


async def test_send_template_no_esta_soportado() -> None:
    with pytest.raises(TemplateNotSupportedError):
        await TwilioVoiceProvider().send_template(
            "+57300", TemplateMessage("t", "es", []), {"client_id": "x"}
        )


def test_restricciones_del_canal() -> None:
    restricciones = TwilioVoiceProvider().get_channel_constraints()
    assert restricciones.supported_media_types == []
    assert restricciones.max_buttons == 0
    assert restricciones.session_window_hours is None


# ─── Llamadas salientes ──────────────────────────────────────────────────────

CONFIG = {
    "account_sid": "AC123",
    "auth_token": "tok",
    "phone_number": "+15005550006",
    "api_base_url": "https://api.twilio.test",
}


def _cliente_http(respuesta: httpx.Response) -> MagicMock:
    cliente = MagicMock()
    cliente.post = AsyncMock(return_value=respuesta)
    cliente.__aenter__ = AsyncMock(return_value=cliente)
    cliente.__aexit__ = AsyncMock(return_value=None)
    return cliente


async def test_start_call_crea_la_llamada_y_devuelve_el_sid() -> None:
    cliente = _cliente_http(httpx.Response(201, json={"sid": "CA999"}))
    with patch("app.services.messaging.voice_provider.httpx.AsyncClient", return_value=cliente):
        sid = await TwilioVoiceProvider().start_call(
            "+573001234567", CONFIG, "https://api/ans", "https://api/status"
        )

    assert sid == "CA999"
    url = cliente.post.await_args.args[0]
    kwargs = cliente.post.await_args.kwargs
    assert url == "https://api.twilio.test/2010-04-01/Accounts/AC123/Calls.json"
    assert kwargs["auth"] == ("AC123", "tok")
    datos = parse_qs(kwargs["content"])
    assert datos["StatusCallbackEvent"] == ["initiated", "answered", "completed"]
    datos = {clave: valores[0] for clave, valores in datos.items()}
    assert datos["To"] == "+573001234567"
    assert datos["From"] == "+15005550006"
    assert datos["Url"] == "https://api/ans"


async def test_start_call_rechazada_da_el_codigo_de_twilio() -> None:
    cliente = _cliente_http(
        httpx.Response(400, json={"code": 21211, "message": "Invalid 'To' Phone Number"})
    )
    with (
        patch("app.services.messaging.voice_provider.httpx.AsyncClient", return_value=cliente),
        pytest.raises(TwilioAPIError, match="21211"),
    ):
        await TwilioVoiceProvider().start_call("+57", CONFIG, "https://a", "https://s")


async def test_start_call_sin_respuesta_de_twilio() -> None:
    cliente = MagicMock()
    cliente.post = AsyncMock(side_effect=httpx.ConnectTimeout("t"))
    cliente.__aenter__ = AsyncMock(return_value=cliente)
    cliente.__aexit__ = AsyncMock(return_value=None)
    with (
        patch("app.services.messaging.voice_provider.httpx.AsyncClient", return_value=cliente),
        pytest.raises(TwilioAPIError, match="no respondio"),
    ):
        await TwilioVoiceProvider().start_call("+57", CONFIG, "https://a", "https://s")


def test_get_channel_config_de_voz_solo_exige_client_id() -> None:
    """Responder en una llamada no necesita las credenciales de salida.

    En un despliegue de solo entrada `TWILIO_PHONE_NUMBER` va vacio porque
    nadie llama hacia fuera. Exigirlo aqui hacia que `deliver_message()`
    reventara en cada respuesta del agente y la llamada quedara muda, con el
    audio entrante y el grafo funcionando.
    """
    from app.agents.nodes._tenant import get_channel_config

    tenant = str(uuid4())
    with patch("app.agents.nodes._tenant.get_settings") as settings:
        settings.return_value = MagicMock(
            DEFAULT_CLIENT_ID=tenant,
            TWILIO_ACCOUNT_SID="",
            TWILIO_AUTH_TOKEN="tok",
            TWILIO_PHONE_NUMBER="",
            TWILIO_API_BASE_URL="https://api.twilio.com",
        )
        proveedor, config = get_channel_config("voice")

    assert proveedor == "twilio"
    assert config["client_id"] == tenant
    # Y lo que falta se detecta donde si importa: al llamar hacia fuera.
    assert faltan_credenciales_salientes(config) == ["account_sid", "phone_number"]


def test_get_channel_config_de_voz_exige_client_id() -> None:
    from app.agents.nodes._tenant import ChannelNotConfiguredError, get_channel_config

    with patch("app.agents.nodes._tenant.get_settings") as settings:
        settings.return_value = MagicMock(
            DEFAULT_CLIENT_ID="",
            TWILIO_ACCOUNT_SID="sid",
            TWILIO_AUTH_TOKEN="tok",
            TWILIO_PHONE_NUMBER="+1",
            TWILIO_API_BASE_URL="https://api.twilio.com",
        )
        with pytest.raises(ChannelNotConfiguredError, match="client_id"):
            get_channel_config("voice")


async def test_start_call_sin_credenciales_no_llama_a_twilio() -> None:
    """La red de seguridad de la clase: no se sale a Twilio sin credenciales."""
    config = {**CONFIG, "phone_number": ""}
    with (
        patch("app.services.messaging.voice_provider.httpx.AsyncClient") as cliente,
        pytest.raises(TwilioAPIError, match="phone_number"),
    ):
        await TwilioVoiceProvider().start_call("+57", config, "https://a", "https://s")
    cliente.assert_not_called()
