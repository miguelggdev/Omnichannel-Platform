"""`deliver_message` admite metadata de canal para un envio concreto."""

import uuid
from typing import Any

import pytest

from app.agents.nodes import _delivery

CLIENT_ID = uuid.uuid4()


class _Resultado:
    def scalar_one_or_none(self) -> Any:
        return None


class _Sesion:
    def __init__(self) -> None:
        self.agregados: list[Any] = []

    def add(self, obj: Any) -> None:
        self.agregados.append(obj)

    async def execute(self, *args: Any, **kwargs: Any) -> _Resultado:
        return _Resultado()


@pytest.fixture
def envio(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Sustituye todo lo que `deliver_message` toca fuera de si mismo.

    Args:
        monkeypatch: Fixture de pytest.

    Returns:
        Registro con lo que recibio el provider (`content`).
    """
    registro: dict[str, Any] = {}

    async def _identificador(client_id: Any, contact_id: Any, channel: str) -> str:
        return "789"

    class _Provider:
        async def send_message(self, to: str, content: Any, channel_config: dict[str, Any]) -> str:
            registro["to"] = to
            registro["content"] = content
            return "ext-1"

    class _Ctx:
        async def __aenter__(self) -> _Sesion:
            return _Sesion()

        async def __aexit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr(_delivery, "get_contact_identifier", _identificador)
    monkeypatch.setattr(_delivery, "get_channel_config", lambda canal: ("telegram", {}))
    monkeypatch.setattr(_delivery, "get_messaging_provider", lambda nombre, cfg: _Provider())
    monkeypatch.setattr(_delivery, "tenant_session", lambda cid: _Ctx())
    monkeypatch.setattr(_delivery, "record_message", lambda *a, **k: None)
    return registro


async def _enviar(**extra: Any) -> None:
    await _delivery.deliver_message(
        client_id=CLIENT_ID,
        conversation_id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        channel="telegram",
        text="hola",
        **extra,
    )


async def test_la_metadata_llega_al_provider(envio: dict[str, Any]) -> None:
    await _enviar(metadata={"request_contact": "Compartir"})

    assert envio["content"].metadata == {"request_contact": "Compartir"}


async def test_sin_metadata_el_contenido_no_cambia(envio: dict[str, Any]) -> None:
    await _enviar()

    assert envio["content"].metadata is None


async def test_se_suma_al_contexto_del_hilo_y_el_llamador_manda(
    envio: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _contexto(client_id: Any, conversation_id: Any, channel: str) -> dict[str, Any]:
        return {"subject": "Re: x", "in_reply_to": "<a@b>"}

    monkeypatch.setattr(_delivery, "_contexto_de_respuesta", _contexto)

    await _enviar(metadata={"subject": "Otro", "extra": 1})

    assert envio["content"].metadata == {"subject": "Otro", "in_reply_to": "<a@b>", "extra": 1}
