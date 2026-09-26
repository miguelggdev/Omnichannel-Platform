"""Listado de tenants de las tareas periodicas bajo RLS real (BUG-045).

Antes `_load_active_client_ids()` hacia `SELECT id FROM clients`, que la RLS
bloquea para el rol de la aplicacion, y caia siempre a `DEFAULT_CLIENT_ID`:
las campanas programadas y el auto-cierre de cualquier otro tenant no corrian.
Esta suite corre como `app_user` (NOBYPASSRLS), igual que CI.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def tres_tenants() -> AsyncGenerator[list[uuid.UUID], None]:
    """Dos tenants activos y uno inactivo, commiteados."""
    from app.core.database import engine, tenant_session

    await engine.dispose()
    ids = [uuid.uuid4() for _ in range(3)]
    for client_id, activo in zip(ids, (True, True, False), strict=True):
        async with tenant_session(client_id) as session:
            await session.execute(
                text(
                    "INSERT INTO clients (id, name, slug, plan, is_active) "
                    "VALUES (:id, 'Tenant', :slug, 'free', :activo)"
                ),
                {"id": str(client_id), "slug": f"t-{client_id.hex[:8]}", "activo": activo},
            )

    yield ids

    for client_id in ids:
        async with tenant_session(client_id) as session:
            await session.execute(
                text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)}
            )


async def test_lista_todos_los_activos_y_ninguno_inactivo(
    tres_tenants: list[uuid.UUID],
) -> None:
    from app.tasks.auto_close import _load_active_client_ids

    activos = set(await _load_active_client_ids())

    assert tres_tenants[0] in activos
    assert tres_tenants[1] in activos
    assert tres_tenants[2] not in activos


async def test_la_rls_de_clients_sigue_intacta(tres_tenants: list[uuid.UUID]) -> None:
    """La funcion es la excepcion acotada: un SELECT directo sigue bloqueado."""
    from app.core.database import tenant_session

    async with tenant_session(tres_tenants[0]) as session:
        visibles = (await session.execute(text("SELECT id FROM clients"))).scalars().all()

    assert visibles == [tres_tenants[0]]
