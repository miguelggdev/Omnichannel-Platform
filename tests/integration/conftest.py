"""Fixtures compartidas por los tests de integracion del flujo de webhooks.

Viven aqui (y no en `test_webhook_flow.py`) para que `test_multichannel.py` las
use sin importarlas de otro modulo de tests, que ruff marca como redefinicion.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

# Sin imports de `app` a nivel de modulo: pytest importa este conftest como
# "inicial" cuando se le pasa una ruta de `tests/integration/` en la linea de
# comandos, ANTES de `pytest_configure` de `tests/conftest.py`, que es donde se
# fijan las variables que `Settings` exige. Importar `app.core.database` aqui
# (crea el engine y llama a `get_settings()`) haria fallar la coleccion.


@pytest_asyncio.fixture
async def webhook_tenant(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[uuid.UUID, None]:
    """Crea un tenant commiteado y lo deja como DEFAULT_CLIENT_ID del worker.

    El worker abre su propia conexion, asi que el cliente tiene que estar commiteado
    y no puede vivir dentro de una transaccion que luego se revierte.
    """
    from app.core.config import get_settings
    from app.core.database import engine, tenant_session

    # app.core.database.engine es un singleton de modulo (una app real vive en
    # un solo event loop). pytest-asyncio abre un loop nuevo por test por
    # defecto, asi que cualquier conexion que el pool del engine haya abierto
    # en el loop de un test anterior (ya cerrado) revienta con
    # "attached to a different loop" al reusarse aqui. dispose() vacia el pool
    # sin usar ninguna conexion existente; el proximo checkout crea una
    # conexion nueva en el loop de ESTE test.
    await engine.dispose()

    client_id = uuid.uuid4()

    # Con contexto de tenant: la politica RLS de `clients` exige
    # `id = current_setting('app.current_client_id')::uuid` tambien en el WITH CHECK.
    async with tenant_session(client_id) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active) "
                "VALUES (:id, 'Tenant Webhook Flow', :slug, 'free', true)"
            ),
            {"id": str(client_id), "slug": f"webhook-flow-{client_id.hex[:8]}"},
        )

    monkeypatch.setattr(get_settings(), "DEFAULT_CLIENT_ID", str(client_id))

    yield client_id

    # Limpieza en orden inverso al de las FKs, tambien con contexto de tenant.
    async with tenant_session(client_id) as session:
        for tabla in (
            "messages",
            "conversations",
            "contact_identifiers",
            "contacts",
            "webhook_dedup",
        ):
            await session.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :cid"),  # noqa: S608
                {"cid": str(client_id)},
            )
        await session.execute(text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)})


@pytest.fixture
def ia_encolada(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Sustituye el `.delay()` del worker de IA: aqui se prueba la base, no el broker.

    Desde Sprint 6 `_enqueue_ai_processing()` encola de verdad
    (`app.tasks.ai_processor`). El job de integracion no levanta el broker de
    Celery, asi que sin este doble cada test se colgaba reintentando conectarse
    al result backend hasta agotar el limite. Lo que interesa verificar aqui es
    que el mensaje llega a la cola con los ids serializados, no que Celery sepa
    hablar con Redis.
    """
    from app.tasks.ai_processor import process_ai_response

    encoladas: list[dict[str, Any]] = []
    monkeypatch.setattr(process_ai_response, "delay", lambda **kw: encoladas.append(kw))
    return encoladas
