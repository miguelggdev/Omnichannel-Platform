"""Vision general de seguridad contra PostgreSQL real con RLS (Sprint 15, cierre).

Requiere base de datos: `pytest tests/ --run-db`. Aqui se prueba lo que un doble no puede: que
el catalogo de PostgreSQL diga que toda tabla con `client_id` tiene RLS habilitada y forzada
(la regla absoluta de CLAUDE.md, ahora vigilada por el propio panel) y que el rol con el que
corren los tests (`app_user`, `NOBYPASSRLS`) se vea como sujeto a RLS.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import create_access_token
from app.main import create_app

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

URL = "/api/v1/platform/security"


def _cliente(role: str) -> AsyncClient:
    token = create_access_token(
        {
            "user_id": str(uuid.uuid4()),
            "client_id": str(uuid.uuid4()),
            "email": "op@example.com",
            "role": role,
        }
    )
    return AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_toda_tabla_con_client_id_tiene_rls_habilitada_y_forzada() -> None:
    async with _cliente("super_admin") as api:
        r = await api.get(URL)
    assert r.status_code == 200, r.text
    datos = r.json()
    nombres = {t["name"] for t in datos["rls_tables"]}
    # Tablas de las que depende el aislamiento: si el catalogo no las ve, la consulta falla.
    assert {"clients", "users", "contacts", "conversations", "messages", "documents"} <= nombres
    assert datos["rls_unprotected"] == []
    assert all(t["rls_enabled"] and t["rls_forced"] for t in datos["rls_tables"])
    checks = {c["id"]: c for c in datos["checks"]}
    assert checks["rls_tables"]["status"] == "ok"
    assert checks["rls_tables"]["detail"] == f"{len(nombres)}/{len(nombres)}"


async def test_el_rol_de_la_aplicacion_esta_sujeto_a_rls() -> None:
    async with _cliente("super_admin") as api:
        datos = (await api.get(URL)).json()
    rol = {c["id"]: c for c in datos["checks"]}["db_role_rls"]
    assert rol["status"] == "ok", rol


@pytest.mark.parametrize("role", ["admin", "supervisor", "agent", "medical"])
async def test_solo_el_super_admin_la_ve(role: str) -> None:
    async with _cliente(role) as api:
        assert (await api.get(URL)).status_code == 403
