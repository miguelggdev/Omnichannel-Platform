"""`media_url` solo admite esquemas conocidos (OWASP: XSS por URL de adjunto)."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.schemas.message import (
    ChannelEnum,
    MessageCreate,
    NormalizedMessage,
    sanear_media_url,
)


@pytest.mark.parametrize(
    "valor",
    [
        "https://cdn.example.com/a.png",
        "http://x.test/a",
        "telegram-file:AgAD123",
        " HTTPS://X.test/a ",
    ],
)
def test_acepta_esquemas_conocidos(valor: str) -> None:
    assert sanear_media_url(valor) == valor.strip()


@pytest.mark.parametrize(
    "valor",
    [
        None,
        "",
        "   ",
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html,<script>1</script>",
        "vbscript:x",
        "file:///etc/passwd",
        "//evil.test/a",
        "sin esquema",
        "https://x.test/a\nb",
        "https://x.test/" + "a" * 1000,
    ],
)
def test_descarta_lo_demas(valor: str | None) -> None:
    assert sanear_media_url(valor) is None


def test_normalized_message_descarta_el_adjunto_pero_conserva_el_texto() -> None:
    msg = NormalizedMessage(
        channel=ChannelEnum.whatsapp,
        sender_identifier="573001112233",
        text="hola",
        media_url="javascript:alert(1)",
        timestamp=datetime.now(timezone.utc),
        external_message_id="m1",
        raw_payload={},
    )
    assert msg.media_url is None
    assert msg.text == "hola"


def test_message_create_descarta_esquema_peligroso() -> None:
    msg = MessageCreate(
        conversation_id=uuid4(),
        direction="inbound",
        sender_type="contact",
        media_url="data:text/html,x",
    )
    assert msg.media_url is None
