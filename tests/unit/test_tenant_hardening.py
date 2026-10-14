"""Piezas de aislamiento entre tenants de ADR-088 que no necesitan base de datos."""

import uuid

import pytest

from app.core.like import ESCAPE, escapar_like, patron_contiene
from app.services.branding import ruta_de_logo_propia, sin_logo_subido


class TestLike:
    @pytest.mark.parametrize(
        ("texto", "esperado"),
        [("ana", "ana"), ("100%", "100\\%"), ("a_b", "a\\_b"), ("c:\\x", "c:\\\\x")],
    )
    def test_escapa_los_comodines(self, texto: str, esperado: str) -> None:
        assert escapar_like(texto) == esperado

    def test_patron_contiene(self) -> None:
        assert patron_contiene("  %  ") == "%\\%%"
        assert ESCAPE == "\\"


class TestLogo:
    def test_solo_rutas_del_propio_tenant(self) -> None:
        mio, otro = uuid.uuid4(), uuid.uuid4()
        assert ruta_de_logo_propia(f"{mio}/branding/logo-1.png", mio) == (
            f"{mio}/branding/logo-1.png"
        )
        assert ruta_de_logo_propia(f"{otro}/branding/logo-1.png", mio) is None
        assert ruta_de_logo_propia(f"{mio}/branding/../../{otro}/x.png", mio) is None
        assert ruta_de_logo_propia(f"{mio}/documentos/x.pdf", mio) is None
        assert ruta_de_logo_propia(None, mio) is None

    def test_clonar_no_lleva_el_archivo_pero_si_la_url(self) -> None:
        tema = {
            "primary_color": "#000",
            "logo_url": "https://cdn.ejemplo.co/l.png",
            "logo_path": "x/branding/l.png",
            "logo_content_type": "image/png",
        }
        assert sin_logo_subido(tema) == {
            "primary_color": "#000",
            "logo_url": "https://cdn.ejemplo.co/l.png",
        }
        assert sin_logo_subido(None) == {}
