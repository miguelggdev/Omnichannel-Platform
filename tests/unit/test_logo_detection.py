"""El tipo del logo se decide por los bytes, no por lo que declare el cliente."""

import pytest

from app.api.v1.business_profile import detectar_imagen

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 20


@pytest.mark.parametrize(
    ("contenido", "esperado"),
    [(PNG, ("png", "image/png")), (JPG, ("jpg", "image/jpeg")), (WEBP, ("webp", "image/webp"))],
)
def test_reconoce_los_formatos_admitidos(contenido: bytes, esperado: tuple[str, str]) -> None:
    assert detectar_imagen(contenido) == esperado


@pytest.mark.parametrize(
    "contenido",
    [
        b"",
        b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
        b"GIF89a" + b"\x00" * 10,
        b"<html><script>alert(1)</script></html>",
        b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 10,  # RIFF pero no WEBP
        b"MZ" + b"\x00" * 20,
    ],
)
def test_rechaza_lo_demas_incluido_svg_y_html(contenido: bytes) -> None:
    assert detectar_imagen(contenido) is None
