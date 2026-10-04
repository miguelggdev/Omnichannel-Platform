"""Gestion de clientes desde la plataforma contra PostgreSQL real con RLS (Sprint 15, fase 2).

Requiere base de datos: `pytest tests/ --run-db`. Es el unico codigo que mira entre tenants,
asi que aqui se prueba lo que ningun doble puede: que `admin_list_clients()` (SECURITY DEFINER)
funcione con un rol sujeto a RLS, que no filtre contenido, que el detalle y el cambio de estado
solo toquen el tenant elegido y que suspender corte el login y el refresh de verdad.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.database import engine, tenant_session
from app.core.security import create_access_token, create_refresh_token, hash_password
from app.main import create_app

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

CLIENTS = "/api/v1/platform/clients"
PASSWORD = "clave-de-prueba-1"


class Tenant:
    def __init__(self, nombre: str) -> None:
        self.client_id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.slug = f"{nombre}-{self.client_id.hex[:8]}"
        self.email = f"u-{self.user_id.hex[:8]}@example.com"


async def _sembrar(
    nombre: str, *, mensajes: int = 0, activo: bool = True, sandbox: bool = False
) -> Tenant:
    t = Tenant(nombre)
    hash_ = hash_password(PASSWORD)
    async with tenant_session(t.client_id) as s:
        await s.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active, is_sandbox) "
                "VALUES (:id, :n, :slug, 'free', :act, :sb)"
            ),
            {
                "id": str(t.client_id),
                "n": f"Cliente {nombre}",
                "slug": t.slug,
                "act": activo,
                "sb": sandbox,
            },
        )
        await s.execute(
            text(
                "INSERT INTO users (id, client_id, email, password_hash, first_name, last_name, "
                "role, is_active) VALUES (:id, :cid, :e, :h, 'U', 'Prueba', 'admin', true)"
            ),
            {"id": str(t.user_id), "cid": str(t.client_id), "e": t.email, "h": hash_},
        )
        if mensajes:
            contact, conv = uuid.uuid4(), uuid.uuid4()
            await s.execute(
                text("INSERT INTO contacts (id, client_id, first_name) VALUES (:i, :c, 'X')"),
                {"i": str(contact), "c": str(t.client_id)},
            )
            await s.execute(
                text(
                    "INSERT INTO conversations (id, client_id, contact_id, channel, status) "
                    "VALUES (:i, :c, :ct, 'whatsapp', 'bot_active')"
                ),
                {"i": str(conv), "c": str(t.client_id), "ct": str(contact)},
            )
            for _ in range(mensajes):
                await s.execute(
                    text(
                        "INSERT INTO messages (client_id, conversation_id, direction, message_type, "
                        "content, sender_type) VALUES (:c, :v, 'inbound', 'text', 'texto secreto', 'contact')"
                    ),
                    {"c": str(t.client_id), "v": str(conv)},
                )
    return t


async def _borrar(t: Tenant) -> None:
    async with tenant_session(t.client_id) as s:
        for tabla in ("messages", "conversations", "contacts", "token_budgets", "users"):
            borrar = text(f"DELETE FROM {tabla} WHERE client_id = :c")  # noqa: S608
            await s.execute(borrar, {"c": str(t.client_id)})
        await s.execute(text("DELETE FROM clients WHERE id = :c"), {"c": str(t.client_id)})


def _cliente(t: Tenant, rol: str = "super_admin") -> AsyncClient:
    token = create_access_token(
        {"user_id": str(t.user_id), "client_id": str(t.client_id), "email": t.email, "role": rol}
    )
    return AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


def _anonimo() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


@pytest_asyncio.fixture
async def plataforma() -> AsyncGenerator[tuple[Tenant, Tenant, Tenant], None]:
    """El tenant del operador, uno con actividad y uno suspendido."""
    await engine.dispose()
    operador = await _sembrar("operador")
    activo = await _sembrar("activo", mensajes=3)
    suspendido = await _sembrar("suspendido", activo=False)
    yield operador, activo, suspendido
    for t in (operador, activo, suspendido):
        await _borrar(t)


async def test_el_listado_ve_a_los_demas_tenants_con_un_rol_sujeto_a_rls(
    plataforma: tuple[Tenant, Tenant, Tenant],
) -> None:
    """Sin la funcion SECURITY DEFINER, la RLS de `clients` solo dejaria ver el propio tenant."""
    operador, activo, suspendido = plataforma

    async with _cliente(operador) as c:
        r = await c.get(CLIENTS, params={"search": "-", "page_size": 100})

    assert r.status_code == 200, r.text
    slugs = {x["slug"] for x in r.json()["items"]}
    assert {operador.slug, activo.slug, suspendido.slug} <= slugs


async def test_trae_cifras_de_uso_y_nada_de_contenido(
    plataforma: tuple[Tenant, Tenant, Tenant],
) -> None:
    operador, activo, _ = plataforma

    async with _cliente(operador) as c:
        r = await c.get(CLIENTS, params={"search": activo.slug})

    (fila,) = r.json()["items"]
    assert fila["users_count"] == 1
    assert fila["conversations_30d"] == 1
    assert fila["messages_30d"] == 3
    assert fila["last_message_at"] is not None
    assert "texto secreto" not in r.text
    assert set(fila) == {
        "id", "name", "slug", "plan", "is_active", "created_at", "suspended_at",
        "users_count", "conversations_30d", "messages_30d", "last_message_at",
    }  # fmt: skip


async def test_filtra_por_estado_y_por_texto(plataforma: tuple[Tenant, Tenant, Tenant]) -> None:
    operador, activo, suspendido = plataforma
    async with _cliente(operador) as c:
        solo_inactivos = (await c.get(CLIENTS, params={"status": "inactive", "search": "-"})).json()
        solo_activos = (
            await c.get(CLIENTS, params={"status": "active", "search": "-", "page_size": 100})
        ).json()
        por_nombre = (await c.get(CLIENTS, params={"search": "CLIENTE SUSPENDIDO"})).json()

    assert suspendido.slug in {x["slug"] for x in solo_inactivos["items"]}
    assert all(not x["is_active"] for x in solo_inactivos["items"])
    assert suspendido.slug not in {x["slug"] for x in solo_activos["items"]}
    assert activo.slug in {x["slug"] for x in solo_activos["items"]}
    assert [x["slug"] for x in por_nombre["items"]] == [suspendido.slug]


@pytest.mark.parametrize("patron", ["%", "_", "\\", "%activo%", "a_t"])
async def test_el_texto_buscado_nunca_actua_como_patron(
    plataforma: tuple[Tenant, Tenant, Tenant], patron: str
) -> None:
    operador, _, _ = plataforma
    async with _cliente(operador) as c:
        r = await c.get(CLIENTS, params={"search": patron})

    assert r.status_code == 200
    assert r.json()["total"] == 0, patron


async def test_los_sandbox_no_son_clientes(plataforma: tuple[Tenant, Tenant, Tenant]) -> None:
    operador, _, _ = plataforma
    sandbox = await _sembrar("sandbox", sandbox=True)
    try:
        async with _cliente(operador) as c:
            lista = await c.get(CLIENTS, params={"search": sandbox.slug})
            detalle = await c.get(f"{CLIENTS}/{sandbox.client_id}")
            estado = await c.put(f"{CLIENTS}/{sandbox.client_id}/status", json={"is_active": False})
    finally:
        await _borrar(sandbox)

    assert lista.json()["total"] == 0
    assert detalle.status_code == 404
    assert estado.status_code == 404


async def test_paginacion_y_total(plataforma: tuple[Tenant, Tenant, Tenant]) -> None:
    operador, _, _ = plataforma
    async with _cliente(operador) as c:
        p1 = (await c.get(CLIENTS, params={"search": "-", "page_size": 1, "page": 1})).json()
        p2 = (await c.get(CLIENTS, params={"search": "-", "page_size": 1, "page": 2})).json()

    assert p1["total"] >= 3
    assert p1["total"] == p2["total"]
    assert p1["items"][0]["id"] != p2["items"][0]["id"]


@pytest.mark.parametrize("rol", ["admin", "supervisor", "agent", "medical"])
async def test_solo_el_super_admin_entra(
    plataforma: tuple[Tenant, Tenant, Tenant], rol: str
) -> None:
    operador, activo, _ = plataforma
    async with _cliente(operador, rol=rol) as c:
        for peticion in (
            c.get(CLIENTS),
            c.get(f"{CLIENTS}/{activo.client_id}"),
            c.put(f"{CLIENTS}/{activo.client_id}/status", json={"is_active": False}),
        ):
            assert (await peticion).status_code == 403


async def test_el_detalle_trae_el_uso_de_ese_tenant_y_no_el_de_otro(
    plataforma: tuple[Tenant, Tenant, Tenant],
) -> None:
    operador, activo, _ = plataforma
    async with tenant_session(activo.client_id) as s:
        await s.execute(
            text(
                "INSERT INTO token_budgets (client_id, month, total_budget, used_tokens) "
                "VALUES (:c, to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM'), 1000, 250)"
            ),
            {"c": str(activo.client_id)},
        )

    async with _cliente(operador) as c:
        d_activo = (await c.get(f"{CLIENTS}/{activo.client_id}")).json()
        d_operador = (await c.get(f"{CLIENTS}/{operador.client_id}")).json()
        inexistente = await c.get(f"{CLIENTS}/{uuid.uuid4()}")

    assert (
        d_activo["messages_30d"],
        d_activo["contacts_total"],
        d_activo["conversations_open"],
    ) == (3, 1, 1)
    assert (d_activo["token_used"], d_activo["token_budget"]) == (250, 1000)
    assert d_activo["has_agent"] is False
    # El operador no tiene mensajes: nada se mezcla entre tenants.
    assert (d_operador["messages_30d"], d_operador["token_used"]) == (0, None)
    assert inexistente.status_code == 404


async def test_suspender_corta_el_login_y_el_refresh_y_reactivar_los_devuelve(
    plataforma: tuple[Tenant, Tenant, Tenant],
) -> None:
    operador, activo, _ = plataforma
    refresh = create_refresh_token(
        {
            "user_id": str(activo.user_id),
            "client_id": str(activo.client_id),
            "email": activo.email,
            "role": "admin",
        }
    )

    async with _cliente(operador) as c:
        r = await c.put(
            f"{CLIENTS}/{activo.client_id}/status",
            json={"is_active": False, "reason": "Pago pendiente"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is False
    assert r.json()["suspended_at"] is not None
    assert r.json()["alert_message"] == "Pago pendiente"

    async with _anonimo() as anon:
        login = await anon.post(
            "/api/v1/auth/login", json={"email": activo.email, "password": PASSWORD}
        )
        renovar = await anon.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert login.status_code == 401
    assert renovar.status_code == 401

    async with _cliente(operador) as c:
        back = await c.put(f"{CLIENTS}/{activo.client_id}/status", json={"is_active": True})
    assert back.json()["is_active"] is True
    assert back.json()["suspended_at"] is None
    assert back.json()["alert_message"] is None  # el motivo se borra al reactivar

    async with _anonimo() as anon:
        login = await anon.post(
            "/api/v1/auth/login", json={"email": activo.email, "password": PASSWORD}
        )
        renovar = await anon.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert login.status_code == 200, login.text
    assert renovar.status_code == 200


async def test_no_se_puede_suspender_el_propio_tenant(
    plataforma: tuple[Tenant, Tenant, Tenant],
) -> None:
    operador, _, _ = plataforma
    async with _cliente(operador) as c:
        r = await c.put(f"{CLIENTS}/{operador.client_id}/status", json={"is_active": False})
        detalle = await c.get(f"{CLIENTS}/{operador.client_id}")

    assert r.status_code == 400
    assert detalle.json()["is_active"] is True


async def test_suspender_a_uno_solo_no_toca_a_los_demas(
    plataforma: tuple[Tenant, Tenant, Tenant],
) -> None:
    operador, activo, suspendido = plataforma
    async with _cliente(operador) as c:
        await c.put(f"{CLIENTS}/{activo.client_id}/status", json={"is_active": False})
        otro = (await c.get(f"{CLIENTS}/{operador.client_id}")).json()
        sigue = (await c.get(f"{CLIENTS}/{suspendido.client_id}")).json()

    assert otro["is_active"] is True
    assert sigue["is_active"] is False  # ya lo estaba; no se reactivo por accidente


async def test_la_funcion_no_sirve_para_ver_contenido(
    plataforma: tuple[Tenant, Tenant, Tenant],
) -> None:
    """Un rol sujeto a RLS sin contexto de tenant no ve mensajes; la funcion solo da conteos."""
    operador, activo, _ = plataforma
    async with tenant_session(operador.client_id) as s:
        directos = (
            await s.execute(
                text("SELECT count(*) FROM messages WHERE client_id = :c"),
                {"c": str(activo.client_id)},
            )
        ).scalar_one()
        via_funcion: Any = (
            await s.execute(
                text("SELECT messages_30d FROM public.admin_list_clients(:s, NULL, 10, 0)"),
                {"s": activo.slug},
            )
        ).scalar_one()

    assert (
        directos == 0
    )  # RLS: desde el contexto del operador no se ven los mensajes de otro tenant
    assert via_funcion == 3  # pero el conteo agregado si sale de la funcion
