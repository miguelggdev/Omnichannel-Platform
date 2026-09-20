"""Tests de la tarea que envia respuestas fijas del sistema por un canal."""

import uuid
from typing import Any

import pytest

from app.agents.nodes import _delivery
from app.tasks import channel_replies as tarea

IDS = {
    "client_id": str(uuid.uuid4()),
    "conversation_id": str(uuid.uuid4()),
    "contact_id": str(uuid.uuid4()),
}


class FakeTaskSelf:
    """Sustituto del `self` de una tarea Celery con bind=True."""

    def __init__(self, retries: int = 0, max_retries: int = 3) -> None:
        self.request = type("Request", (), {"retries": retries})()
        self.max_retries = max_retries
        self.countdowns: list[float] = []

    def retry(self, exc: Exception | None = None, countdown: float | None = None) -> Exception:
        self.countdowns.append(countdown or 0)
        return RuntimeError("retry solicitado")


def _ejecutar(yo: FakeTaskSelf | None = None, metadata: dict[str, Any] | None = None) -> Any:
    return tarea.send_channel_reply.run.__func__(  # type: ignore[attr-defined]
        yo or FakeTaskSelf(), channel="telegram", text="hola", metadata=metadata, **IDS
    )


@pytest.fixture
def entregas(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Captura las llamadas a `deliver_message`."""
    llamadas: list[dict[str, Any]] = []

    async def _deliver(**kwargs: Any) -> str:
        llamadas.append(kwargs)
        return "ext-1"

    monkeypatch.setattr(_delivery, "deliver_message", _deliver)
    return llamadas


class TestEnvio:
    def test_entrega_por_el_canal_con_los_ids_y_la_metadata(
        self, entregas: list[dict[str, Any]]
    ) -> None:
        resultado = _ejecutar(metadata={"remove_keyboard": True})

        assert resultado == {"status": "sent"}
        (llamada,) = entregas
        assert str(llamada["client_id"]) == IDS["client_id"]
        assert str(llamada["conversation_id"]) == IDS["conversation_id"]
        assert str(llamada["contact_id"]) == IDS["contact_id"]
        assert llamada["channel"] == "telegram"
        assert llamada["text"] == "hola"
        assert llamada["metadata"] == {"remove_keyboard": True}

    def test_un_fallo_reintenta_con_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _falla(**kwargs: Any) -> str:
            raise ConnectionError("Telegram caido")

        monkeypatch.setattr(_delivery, "deliver_message", _falla)
        yo = FakeTaskSelf(retries=1)

        with pytest.raises(RuntimeError, match="retry solicitado"):
            _ejecutar(yo)

        assert yo.countdowns == [tarea.RETRY_BACKOFF_SECONDS[1]]

    def test_agotados_los_reintentos_no_propaga(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No hay una persona esperando una respuesta: se registra y se cierra."""

        async def _falla(**kwargs: Any) -> str:
            raise ConnectionError("Telegram caido")

        monkeypatch.setattr(_delivery, "deliver_message", _falla)
        yo = FakeTaskSelf(retries=3)

        assert _ejecutar(yo) == {"status": "failed"}
        assert yo.countdowns == []

    def test_el_log_de_un_fallo_no_incluye_el_mensaje_de_la_excepcion(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """El mensaje de una excepcion de red puede traer la URL con el token del bot."""

        async def _falla(**kwargs: Any) -> str:
            raise ConnectionError("https://api.telegram.org/botSECRETO/sendMessage")

        monkeypatch.setattr(_delivery, "deliver_message", _falla)

        with caplog.at_level("WARNING"), pytest.raises(RuntimeError):
            _ejecutar(FakeTaskSelf(retries=0))

        assert "SECRETO" not in caplog.text
