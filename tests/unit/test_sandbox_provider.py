"""`SandboxProvider`: el canal de las conversaciones de prueba (Sprint 14c, ADR-078)."""

from typing import Any

import pytest

from app.agents.nodes._tenant import CHANNEL_PROVIDERS, get_channel_config
from app.services.channel_identity import identidad_verificada
from app.services.messaging.base import MessageContent, TemplateNotSupportedError
from app.services.messaging.factory import get_messaging_provider
from app.services.messaging.sandbox import SandboxProvider


def test_la_factory_lo_resuelve_por_nombre() -> None:
    assert isinstance(get_messaging_provider("sandbox"), SandboxProvider)
    assert CHANNEL_PROVIDERS["sandbox"] == "sandbox"


def test_el_canal_no_necesita_credenciales() -> None:
    """Sin `DEFAULT_CLIENT_ID` ni nada: el webchat si los exigiria."""
    assert get_channel_config("sandbox") == ("sandbox", {})


async def test_enviar_no_toca_redis_ni_la_red(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_debe_llamarse() -> None:
        raise AssertionError("el sandbox no debe usar Redis")

    monkeypatch.setattr("app.services.dedup.get_redis", _no_debe_llamarse)

    id_externo = await SandboxProvider().send_message("tester", MessageContent(text="hola"), {})

    assert len(id_externo) == 32


async def test_cada_envio_tiene_su_propio_id() -> None:
    proveedor = SandboxProvider()

    ids = {await proveedor.send_message("t", MessageContent(text="x"), {}) for _ in range(5)}

    assert len(ids) == 5


async def test_no_acepta_webhooks_de_nadie() -> None:
    proveedor = SandboxProvider()

    assert await proveedor.validate_signature(b"{}", "firma", "secreto") is False
    with pytest.raises(NotImplementedError):
        await proveedor.parse_webhook({})


async def test_no_tiene_templates() -> None:
    with pytest.raises(TemplateNotSupportedError):
        await SandboxProvider().send_template("t", None, {})  # type: ignore[arg-type]


def test_solo_texto_y_sin_ventana_de_sesion() -> None:
    restricciones = SandboxProvider().get_channel_constraints()

    assert restricciones.supported_media_types == []
    assert restricciones.session_window_hours is None
    assert restricciones.max_buttons == 0


def test_el_canal_no_autoriza_a_los_agentes_clinico_ni_de_marketing() -> None:
    """Su identidad la elige quien llama, no una plataforma: ADR-072 no la admite."""
    assert identidad_verificada("sandbox") is False


async def test_el_endpoint_generico_de_webhooks_no_acepta_nada(api_client: Any) -> None:
    respuesta = await api_client.post("/api/v1/webhooks/sandbox/sandbox", json={"text": "hola"})

    assert respuesta.status_code in (400, 401, 403)
