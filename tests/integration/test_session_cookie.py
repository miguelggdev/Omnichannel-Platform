"""Sesion del panel contra PostgreSQL y Redis reales (ADR-089).

El navegador de los tests es un `AsyncClient` con `https://`: guarda y reenvia la cookie
`Secure` como lo haria uno de verdad, con su `Path=/api/v1/auth`.
"""

import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.database import engine, tenant_session
from app.core.security import create_access_token, decode_jwt, hash_password
from app.main import create_app
from app.services.dedup import get_redis

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

PASSWORD = "clave-de-prueba-1"
LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"


class Usuario:
    def __init__(self) -> None:
        self.client_id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.email = f"sesion-{self.user_id.hex[:10]}@example.com"


@pytest_asyncio.fixture
async def usuario() -> AsyncGenerator[Usuario, None]:
    await engine.dispose()
    u = Usuario()
    async with tenant_session(u.client_id) as s:
        await s.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active) "
                "VALUES (:id, 'Sesion', :slug, 'free', true)"
            ),
            {"id": str(u.client_id), "slug": f"sesion-{u.client_id.hex[:8]}"},
        )
        await s.execute(
            text(
                "INSERT INTO users (id, client_id, email, password_hash, first_name, last_name, "
                "role, is_active) VALUES (:id, :c, :e, :h, 'Ana', 'Prueba', 'admin', true)"
            ),
            {
                "id": str(u.user_id),
                "c": str(u.client_id),
                "e": u.email,
                "h": hash_password(PASSWORD),
            },
        )
    yield u
    async with tenant_session(u.client_id) as s:
        await s.execute(text("DELETE FROM users WHERE client_id = :c"), {"c": str(u.client_id)})
        await s.execute(text("DELETE FROM clients WHERE id = :c"), {"c": str(u.client_id)})


def _navegador() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="https://test")


async def _entrar(nav: AsyncClient, u: Usuario) -> Any:
    r = await nav.post(LOGIN, json={"email": u.email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r


async def _version(u: Usuario) -> int:
    async with tenant_session(u.client_id) as s:
        return int(
            (
                await s.execute(
                    text("SELECT token_version FROM users WHERE id = :u"), {"u": str(u.user_id)}
                )
            ).scalar_one()
        )


async def test_login_deja_la_sesion_en_la_cookie_y_refresh_la_rota(usuario: Usuario) -> None:
    async with _navegador() as nav:
        login = await _entrar(nav, usuario)
        primero = nav.cookies.get("refresh_token")
        refrescado = await nav.post(REFRESH)
        segundo = nav.cookies.get("refresh_token")

    assert "refresh_token" not in login.json()
    assert primero
    assert refrescado.status_code == 200, refrescado.text
    assert segundo
    assert segundo != primero
    assert decode_jwt(refrescado.json()["access_token"])["user_id"] == str(usuario.user_id)


async def test_un_refresh_robado_y_reusado_cierra_todas_las_sesiones(usuario: Usuario) -> None:
    async with _navegador() as nav:
        await _entrar(nav, usuario)
        robado = nav.cookies.get("refresh_token") or ""
        assert (await nav.post(REFRESH)).status_code == 200
        # El primer uso queda fuera del margen de dos pestanas.
        jti = decode_jwt(robado)["jti"]
        await get_redis().set(f"refresh-usado:{jti}", int(time.time()) - 60, ex=600)

    async with _navegador() as atacante:
        atacante.cookies.set("refresh_token", robado)
        reuso = await atacante.post(REFRESH)

    assert reuso.status_code == 401
    assert await _version(usuario) == 1


async def test_logout_all_invalida_el_refresh_de_otros_dispositivos(usuario: Usuario) -> None:
    async with _navegador() as movil, _navegador() as portatil:
        await _entrar(movil, usuario)
        login = await _entrar(portatil, usuario)
        access = login.json()["access_token"]
        salir = await portatil.post(
            "/api/v1/auth/logout-all", headers={"Authorization": f"Bearer {access}"}
        )
        movil_despues = await movil.post(REFRESH)

    assert salir.status_code == 204
    assert movil_despues.status_code == 401
    assert await _version(usuario) == 1


async def test_cambiar_la_contrasena_cierra_las_sesiones(usuario: Usuario) -> None:
    admin = create_access_token(
        {
            "user_id": str(usuario.user_id),
            "client_id": str(usuario.client_id),
            "email": usuario.email,
            "role": "admin",
        }
    )
    async with _navegador() as nav:
        await _entrar(nav, usuario)
        cambio = await nav.put(
            f"/api/v1/admin/users/{usuario.user_id}",
            json={"password": "otra-clave-segura-2"},
            headers={"Authorization": f"Bearer {admin}"},
        )
        despues = await nav.post(REFRESH)

    assert cambio.status_code == 200, cambio.text
    assert despues.status_code == 401


async def test_logout_borra_la_cookie_y_el_refresh_deja_de_valer(usuario: Usuario) -> None:
    async with _navegador() as nav:
        await _entrar(nav, usuario)
        token = nav.cookies.get("refresh_token") or ""
        salir = await nav.post("/api/v1/auth/logout")
        sin_cookie = nav.cookies.get("refresh_token")
        jti = decode_jwt(token)["jti"]
        await get_redis().set(f"refresh-usado:{jti}", int(time.time()) - 60, ex=600)
        nav.cookies.set("refresh_token", token)
        reusado = await nav.post(REFRESH)

    assert salir.status_code == 204
    assert sin_cookie is None
    assert reusado.status_code == 401
