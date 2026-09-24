"""Tests del cliente de la DIAN (Sprint 12).

El digito de verificacion se comprueba contra NITs publicos reales: es la parte
que el spec traia mal (aplicaba los pesos de izquierda a derecha) y un digito
equivocado hace que la DIAN rechace la factura.
"""

import os
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-testing-only-minimum-32-chars")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-minimum-32-characters-long")

from app.services import dian
from app.services.dian import (
    DianError,
    DianNoConfiguradaError,
    NitInvalidoError,
    calcular_digito_verificacion,
    consultar_contribuyente,
    enviar_factura,
    normalizar_nit,
)

#: NITs publicos conocidos con su digito de verificacion oficial.
NITS_CONOCIDOS = {
    "800197268": 4,
    "830053812": 2,
    "860002964": 4,
    "890903938": 8,
    "899999068": 1,
    "890100577": 6,
}


def _configurar(monkeypatch, url: str = "https://dian.example", token: str = "t") -> None:
    """Hace creer al cliente que hay credenciales de la DIAN."""
    monkeypatch.setattr(
        dian,
        "get_settings",
        lambda: SimpleNamespace(DIAN_API_URL=url, DIAN_API_TOKEN=token, DIAN_TIMEOUT_SECONDS=5.0),
    )


def _sin_configurar(monkeypatch) -> None:
    """Deja el cliente sin credenciales."""
    monkeypatch.setattr(
        dian,
        "get_settings",
        lambda: SimpleNamespace(DIAN_API_URL="", DIAN_API_TOKEN="", DIAN_TIMEOUT_SECONDS=5.0),
    )


def _respuesta(monkeypatch, status_code: int, payload: dict | None = None) -> None:
    """Sustituye httpx.AsyncClient por uno que devuelve lo indicado."""

    class _Cliente:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get(self, url: str):
            return SimpleNamespace(status_code=status_code, json=lambda: payload or {})

        async def post(self, url: str, json=None):
            return SimpleNamespace(status_code=status_code, json=lambda: payload or {})

    monkeypatch.setattr(dian.httpx, "AsyncClient", lambda **kwargs: _Cliente(**kwargs))


class TestDigitoDeVerificacion:
    @pytest.mark.parametrize(("nit", "esperado"), sorted(NITS_CONOCIDOS.items()))
    def test_coincide_con_los_nits_publicos(self, nit: str, esperado: int) -> None:
        assert calcular_digito_verificacion(nit) == esperado

    def test_la_formula_del_spec_habria_fallado(self) -> None:
        """El spec aplica los pesos de izquierda a derecha sobre el NIT rellenado.

        Falla en 5 de los 6 NITs de referencia. El que acierta (899999068) lo
        hace por casualidad: su digito coincide con el valor que la formula
        equivocada devuelve para casi cualquier entrada.
        """
        pesos = (3, 7, 13, 17, 19, 23, 29, 37, 41, 43, 47, 53, 59, 67, 71)

        def como_el_spec(nit: str) -> int:
            total = sum(int(d) * w for d, w in zip(nit.zfill(15), pesos, strict=False))
            resto = total % 11
            return resto if resto <= 1 else 11 - resto

        fallos = [nit for nit, dv in NITS_CONOCIDOS.items() if como_el_spec(nit) != dv]
        assert len(fallos) == len(NITS_CONOCIDOS) - 1, f"solo deberia acertar uno: {fallos}"


class TestNormalizarNit:
    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            ("900.373.115", "900373115"),
            ("900373115-0", "900373115"),
            (" 900 373 115 ", "900373115"),
        ],
    )
    def test_quita_formato_y_digito_de_verificacion(self, entrada: str, esperado: str) -> None:
        assert normalizar_nit(entrada) == esperado

    @pytest.mark.parametrize("entrada", ["abc", "12345", "9" * 16, ""])
    def test_rechaza_lo_que_no_es_un_nit(self, entrada: str) -> None:
        with pytest.raises(NitInvalidoError):
            normalizar_nit(entrada)


class TestConsultarContribuyente:
    @pytest.mark.asyncio
    async def test_sin_credenciales_devuelve_none(self, monkeypatch) -> None:
        """Sin integracion no se inventa nada: solo se valida el formato."""
        _sin_configurar(monkeypatch)

        assert await consultar_contribuyente("900373115") is None

    @pytest.mark.asyncio
    async def test_devuelve_los_datos_del_contribuyente(self, monkeypatch) -> None:
        _configurar(monkeypatch)
        _respuesta(monkeypatch, 200, {"razon_social": "ACME SAS", "regimen": "comun"})

        datos = await consultar_contribuyente("900373115")

        assert datos == {"razon_social": "ACME SAS", "regimen": "comun"}

    @pytest.mark.asyncio
    async def test_un_404_es_un_nit_que_no_existe(self, monkeypatch) -> None:
        """Distinto de "no se pudo consultar": el dict vacio dice que no esta."""
        _configurar(monkeypatch)
        _respuesta(monkeypatch, 404)

        assert await consultar_contribuyente("900373115") == {}

    @pytest.mark.asyncio
    async def test_un_fallo_de_red_no_propaga(self, monkeypatch) -> None:
        _configurar(monkeypatch)

        class _ClienteRoto:
            def __init__(self, **kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def get(self, url: str):
                raise httpx.ConnectError("sin ruta")

        monkeypatch.setattr(dian.httpx, "AsyncClient", lambda **kwargs: _ClienteRoto())

        assert await consultar_contribuyente("900373115") is None


class TestEnviarFactura:
    @pytest.mark.asyncio
    async def test_sin_credenciales_lanza_para_dejarla_pendiente(self, monkeypatch) -> None:
        """Quien llama debe dejar la factura en pending_dian, no inventar un CUFE."""
        _sin_configurar(monkeypatch)

        with pytest.raises(DianNoConfiguradaError):
            await enviar_factura({"total_cents": 100})

    @pytest.mark.asyncio
    async def test_devuelve_la_respuesta_de_la_dian(self, monkeypatch) -> None:
        _configurar(monkeypatch)
        _respuesta(monkeypatch, 200, {"cufe": "abc123"})

        assert await enviar_factura({"total_cents": 100}) == {"cufe": "abc123"}

    @pytest.mark.asyncio
    async def test_un_error_http_es_dian_error(self, monkeypatch) -> None:
        _configurar(monkeypatch)
        _respuesta(monkeypatch, 500)

        with pytest.raises(DianError):
            await enviar_factura({"total_cents": 100})
