"""Gestion de usuarios contra PostgreSQL real con RLS (Sprint 15, fase 2).

Requiere base de datos: `pytest tests/ --run-db`. Lo que los dobles no prueban: que RLS
aisle los usuarios de cada tenant en el listado y en la edicion, que la regla del ultimo
administrador cuente filas reales y que un usuario desactivado no pueda renovar su sesion.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.database import engine, tenant_session
from app.core.security import create_access_token, create_refresh_token, decode_jwt, hash_password
from app.main import create_app

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

USERS = "/api/v1/admin/users"


class Tenant:
    def __init__(self, client_id: uuid.UUID, admin_id: uuid.UUID, agent_id: uuid.UUID) -> None:
        self.client_id = client_id
        self.admin_id = admin_id
        self.agent_id = agent_id
        self.admin_email = f"admin-{admin_id.hex[:8]}@example.com"
        self.agent_email = f"agent-{agent_id.hex[:8]}@example.com"


async def _sembrar(nombre: str) -> Tenant:
    client_id, admin_id, agent_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    t = Tenant(client_id, admin_id, agent_id)
    hash_ = hash_password("clave-de-prueba-1")
    async with tenant_session(client_id) as s:
        await s.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active) "
                "VALUES (:id, :n, :slug, 'free', true)"
            ),
            {"id": str(client_id), "n": nombre, "slug": f"{nombre}-{client_id.hex[:8]}"},
        )
        for uid, email, rol, first in (
            (admin_id, t.admin_email, "admin", "Ana"),
            (agent_id, t.agent_email, "agent", "Aldo"),
        ):
            await s.execute(
                text(
                    "INSERT INTO users (id, client_id, email, password_hash, first_name, "
                    "last_name, role, is_active) VALUES (:id, :cid, :email, :h, :f, 'Prueba', "
                    "CAST(:rol AS user_role), true)"
                ),
                {
                    "id": str(uid),
                    "cid": str(client_id),
                    "email": email,
                    "h": hash_,
                    "f": first,
                    "rol": rol,
                },
            )
    return t


async def _borrar(t: Tenant) -> None:
    async with tenant_session(t.client_id) as s:
        await s.execute(text("DELETE FROM users WHERE client_id = :c"), {"c": str(t.client_id)})
        await s.execute(text("DELETE FROM clients WHERE id = :c"), {"c": str(t.client_id)})


def _cliente(t: Tenant, user_id: uuid.UUID, rol: str, email: str) -> AsyncClient:
    token = create_access_token(
        {"user_id": str(user_id), "client_id": str(t.client_id), "email": email, "role": rol}
    )
    return AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest_asyncio.fixture
async def dos() -> AsyncGenerator[tuple[Tenant, Tenant], None]:
    await engine.dispose()
    a, b = await _sembrar("usr-a"), await _sembrar("usr-b")
    yield a, b
    await _borrar(a)
    await _borrar(b)


async def test_el_listado_solo_trae_los_usuarios_del_propio_tenant(
    dos: tuple[Tenant, Tenant],
) -> None:
    a, _ = dos
    async with _cliente(a, a.admin_id, "admin", a.admin_email) as c:
        r = await c.get(USERS)

    assert r.status_code == 200
    assert r.json()["total"] == 2
    assert {u["email"] for u in r.json()["items"]} == {a.admin_email, a.agent_email}


async def test_filtra_por_rol_y_busca_por_nombre(dos: tuple[Tenant, Tenant]) -> None:
    a, _ = dos
    async with _cliente(a, a.admin_id, "admin", a.admin_email) as c:
        por_rol = await c.get(USERS, params={"role": "agent"})
        por_texto = await c.get(USERS, params={"search": "ALDO"})
        nada = await c.get(USERS, params={"search": "zzz"})

    assert [u["email"] for u in por_rol.json()["items"]] == [a.agent_email]
    assert [u["email"] for u in por_texto.json()["items"]] == [a.agent_email]
    assert nada.json()["total"] == 0


async def test_un_admin_no_puede_editar_a_un_usuario_de_otro_tenant(
    dos: tuple[Tenant, Tenant],
) -> None:
    a, b = dos
    async with _cliente(a, a.admin_id, "admin", a.admin_email) as c:
        r = await c.put(f"{USERS}/{b.agent_id}", json={"is_active": False})

    assert r.status_code == 404
    async with tenant_session(b.client_id) as s:
        activo = (
            await s.execute(
                text("SELECT is_active FROM users WHERE id = :i"), {"i": str(b.agent_id)}
            )
        ).scalar_one()
    assert activo is True


async def test_editar_cambia_la_fila_de_verdad(dos: tuple[Tenant, Tenant]) -> None:
    a, _ = dos
    async with _cliente(a, a.admin_id, "admin", a.admin_email) as c:
        r = await c.put(
            f"{USERS}/{a.agent_id}", json={"first_name": "Aurelio", "role": "supervisor"}
        )

    assert r.status_code == 200, r.text
    async with tenant_session(a.client_id) as s:
        fila = (
            (
                await s.execute(
                    text("SELECT first_name, role::text AS role FROM users WHERE id = :i"),
                    {"i": str(a.agent_id)},
                )
            )
            .mappings()
            .one()
        )
    assert dict(fila) == {"first_name": "Aurelio", "role": "supervisor"}


async def test_el_tenant_no_se_queda_sin_administrador(dos: tuple[Tenant, Tenant]) -> None:
    """Con un unico admin, ni otro rol con permiso puede degradarlo o desactivarlo."""
    a, _ = dos
    async with _cliente(a, uuid.uuid4(), "super_admin", "op@example.com") as c:
        desactivar = await c.put(f"{USERS}/{a.admin_id}", json={"is_active": False})
        degradar = await c.put(f"{USERS}/{a.admin_id}", json={"role": "agent"})

    assert desactivar.status_code == 409
    assert degradar.status_code == 409


async def test_un_usuario_desactivado_no_puede_renovar_la_sesion(
    dos: tuple[Tenant, Tenant],
) -> None:
    a, _ = dos
    refresh = create_refresh_token(
        {
            "user_id": str(a.agent_id),
            "client_id": str(a.client_id),
            "email": a.agent_email,
            "role": "agent",
        }
    )
    async with _cliente(a, a.admin_id, "admin", a.admin_email) as admin:
        await admin.put(f"{USERS}/{a.agent_id}", json={"is_active": False})
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as anon:
        r = await anon.post("/api/v1/auth/refresh", json={"refresh_token": refresh})

    assert r.status_code == 401


async def test_tras_cambiar_el_rol_el_refresh_emite_el_rol_nuevo(
    dos: tuple[Tenant, Tenant],
) -> None:
    """El refresh viejo decia `agent`; tras ascender a supervisor el token nuevo lo refleja."""
    a, _ = dos
    refresh = create_refresh_token(
        {
            "user_id": str(a.agent_id),
            "client_id": str(a.client_id),
            "email": a.agent_email,
            "role": "agent",
        }
    )
    async with _cliente(a, a.admin_id, "admin", a.admin_email) as admin:
        await admin.put(f"{USERS}/{a.agent_id}", json={"role": "supervisor"})
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as anon:
        r = await anon.post("/api/v1/auth/refresh", json={"refresh_token": refresh})

    assert r.status_code == 200, r.text
    assert decode_jwt(r.json()["access_token"])["role"] == "supervisor"


async def test_restablecer_la_contrasena_permite_entrar_con_la_nueva(
    dos: tuple[Tenant, Tenant],
) -> None:
    a, _ = dos
    async with _cliente(a, a.admin_id, "admin", a.admin_email) as admin:
        await admin.put(f"{USERS}/{a.agent_id}", json={"password": "clave-nueva-123"})
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as anon:
        nueva = await anon.post(
            "/api/v1/auth/login", json={"email": a.agent_email, "password": "clave-nueva-123"}
        )
        vieja = await anon.post(
            "/api/v1/auth/login", json={"email": a.agent_email, "password": "clave-de-prueba-1"}
        )

    assert nueva.status_code == 200, nueva.text
    assert vieja.status_code == 401
