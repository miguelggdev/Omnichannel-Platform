"""Tests del tope del cuerpo de las peticiones (`app/middleware/body_limit.py`, ADR-088)."""

from typing import Any

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.middleware.body_limit import (
    PAYLOAD_TOO_LARGE,
    TOPE_POR_DEFECTO,
    BodySizeLimitMiddleware,
    tope_para,
)


class Cuerpo(BaseModel):
    texto: str


def _app() -> FastAPI:
    app = FastAPI()

    @app.post("/crudo")
    async def crudo(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    @app.post("/modelo")
    async def modelo(cuerpo: Cuerpo) -> dict[str, int]:
        return {"chars": len(cuerpo.texto)}

    app.add_middleware(BodySizeLimitMiddleware)
    return app


async def _troceado(total: int, trozo: int = 64 * 1024) -> Any:
    """Cuerpo sin `Content-Length` (transfer-encoding chunked)."""
    enviados = 0
    while enviados < total:
        n = min(trozo, total - enviados)
        enviados += n
        yield b"a" * n


@pytest.fixture
async def cliente() -> Any:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        yield c


class TestTopePorRuta:
    @pytest.mark.parametrize(
        ("ruta", "tope"),
        [
            ("/api/v1/webhooks/email/inbound", 32 * 1024 * 1024),
            ("/api/v1/webhooks/meta/instagram", 2 * 1024 * 1024),
            ("/api/v1/documents/upload", 51 * 1024 * 1024),
            ("/api/v1/auth/login", TOPE_POR_DEFECTO),
        ],
    )
    def test_gana_el_prefijo_mas_largo(self, ruta: str, tope: int) -> None:
        assert tope_para(ruta) == tope


class TestMiddleware:
    async def test_un_cuerpo_normal_pasa(self, cliente: AsyncClient) -> None:
        r = await cliente.post("/crudo", content=b"x" * 1000)
        assert r.status_code == 200
        assert r.json() == {"bytes": 1000}

    async def test_content_length_grande_se_rechaza_sin_leer(self, cliente: AsyncClient) -> None:
        r = await cliente.post("/crudo", content=b"x" * (TOPE_POR_DEFECTO + 1))
        assert r.status_code == 413
        assert r.json()["error_code"] == PAYLOAD_TOO_LARGE

    async def test_sin_content_length_se_cuenta_lo_que_llega(self, cliente: AsyncClient) -> None:
        """Con chunked no hay cabecera que mirar: antes se cargaba todo en memoria."""
        r = await cliente.post("/crudo", content=_troceado(TOPE_POR_DEFECTO * 3))
        assert r.status_code == 413
        assert r.json()["error_code"] == PAYLOAD_TOO_LARGE

    async def test_tambien_corta_el_parseo_de_un_modelo(self, cliente: AsyncClient) -> None:
        """FastAPI lee el cuerpo de un modelo Pydantic antes del handler."""
        r = await cliente.post(
            "/modelo",
            content=_troceado(TOPE_POR_DEFECTO * 2),
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 413
        assert r.json()["error_code"] == PAYLOAD_TOO_LARGE

    async def test_chunked_pequeno_pasa(self, cliente: AsyncClient) -> None:
        r = await cliente.post("/crudo", content=_troceado(200_000))
        assert r.status_code == 200
        assert r.json() == {"bytes": 200_000}
