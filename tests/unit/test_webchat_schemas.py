"""Tests de los frames del WebSocket de Webchat."""

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from app.schemas.webchat import (
    ButtonReplyFrame,
    HelloFrame,
    InboundFrame,
    MessageFrame,
    PingFrame,
    frame_de_error,
    limpiar_texto,
)

_ADAPTER: TypeAdapter[Any] = TypeAdapter(InboundFrame)


class TestHello:
    def test_minimo(self) -> None:
        hello = HelloFrame.model_validate({"type": "hello"})

        assert hello.session is None
        assert hello.last_message_id is None
        assert hello.name is None

    def test_con_todo(self) -> None:
        hello = HelloFrame.model_validate(
            {"type": "hello", "session": "tok", "last_message_id": "m1", "name": "  Ada  "}
        )

        assert (hello.session, hello.last_message_id, hello.name) == ("tok", "m1", "Ada")

    @pytest.mark.parametrize("nombre", ["", "   ", "\x00\x07"])
    def test_un_nombre_vacio_o_de_controles_pasa_a_none(self, nombre: str) -> None:
        assert HelloFrame.model_validate({"type": "hello", "name": nombre}).name is None

    def test_el_nombre_pierde_los_caracteres_de_control(self) -> None:
        hello = HelloFrame.model_validate({"type": "hello", "name": "Ada\x00\x1b[31m"})

        assert hello.name == "Ada[31m"

    @pytest.mark.parametrize(
        "frame",
        [
            {"type": "message", "message_id": "a", "text": "x"},
            {"type": "hello", "extra": 1},
            {"type": "hello", "session": "x" * 513},
            {"type": "hello", "name": "x" * 101},
            {},
        ],
    )
    def test_lo_que_no_es_un_hello_valido_se_rechaza(self, frame: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            HelloFrame.model_validate(frame)


class TestMessage:
    def test_valido(self) -> None:
        frame = _ADAPTER.validate_python(
            {"type": "message", "message_id": "abc-123", "text": " hola "}
        )

        assert isinstance(frame, MessageFrame)
        assert frame.text == "hola"

    @pytest.mark.parametrize("texto", ["", "   ", "\n\t ", "\x00\x01"])
    def test_un_texto_vacio_se_rechaza(self, texto: str) -> None:
        with pytest.raises(ValidationError):
            MessageFrame.model_validate({"type": "message", "message_id": "a", "text": texto})

    def test_conserva_saltos_de_linea_y_quita_otros_controles(self) -> None:
        frame = MessageFrame.model_validate(
            {"type": "message", "message_id": "a", "text": "a\nb\tc\x00\x1b"}
        )

        assert frame.text == "a\nb\tc"

    @pytest.mark.parametrize("message_id", ["", "a b", "a:b", "a/b", "x" * 65, "a\n", "ñ"])
    def test_un_message_id_inseguro_se_rechaza(self, message_id: str) -> None:
        """Va dentro de una clave de Redis y de un id externo."""
        with pytest.raises(ValidationError):
            MessageFrame.model_validate({"type": "message", "message_id": message_id, "text": "x"})

    def test_un_campo_desconocido_se_rechaza(self) -> None:
        with pytest.raises(ValidationError):
            MessageFrame.model_validate(
                {"type": "message", "message_id": "a", "text": "x", "media_url": "http://x"}
            )


class TestOtros:
    def test_ping(self) -> None:
        assert isinstance(_ADAPTER.validate_python({"type": "ping"}), PingFrame)

    def test_button_reply(self) -> None:
        frame = _ADAPTER.validate_python(
            {"type": "button_reply", "message_id": "b1", "id": "si", "title": "Si"}
        )

        assert isinstance(frame, ButtonReplyFrame)
        assert (frame.id, frame.title) == ("si", "Si")

    @pytest.mark.parametrize(
        "frame",
        [
            {"type": "typing"},
            {"type": "hello"},
            {"type": "button_reply", "message_id": "b", "id": "", "title": "x"},
            {"type": "button_reply", "message_id": "b", "id": "x", "title": ""},
            {"message_id": "a", "text": "x"},
        ],
    )
    def test_frames_no_admitidos(self, frame: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            _ADAPTER.validate_python(frame)


def test_limpiar_texto_conserva_lo_legitimo() -> None:
    assert limpiar_texto("Hola\nmundo\t¿qué tal? 😀") == "Hola\nmundo\t¿qué tal? 😀"


def test_frame_de_error() -> None:
    assert frame_de_error("c", "m") == {"type": "error", "code": "c", "message": "m"}
