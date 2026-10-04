"""Operacion de la plataforma contra Redis, PostgreSQL y un broker reales (Sprint 15, fase 2).

Requiere base de datos y Redis: `pytest tests/ --run-db`. No hay workers de Celery en el CI:
lo que se prueba es que `inspect` real contra un broker real distinga "sin workers" de "broker
inalcanzable", que las colas se midan en Redis de verdad y que `/system` mida latencias reales.
"""

import time
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient

from app.core.security import create_access_token
from app.main import create_app
from app.tasks.celery_config import celery_app

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

BASE = "/api/v1/platform"
BROKER_LOCAL = "redis://localhost:6379/0"


def _super_admin() -> AsyncClient:
    token = create_access_token(
        {
            "user_id": str(uuid.uuid4()),
            "client_id": str(uuid.uuid4()),
            "email": "op@example.com",
            "role": "super_admin",
        }
    )
    return AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


def _descartar_conexiones() -> None:
    """Olvida el pool y los productores que Celery creo con la URL anterior del broker."""
    celery_app._pool = None
    for cache in ("amqp", "control"):
        celery_app.__dict__.pop(cache, None)


@pytest.fixture
def broker_local(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """El broker por defecto apunta al host `redis` de Docker; en CI es localhost."""
    monkeypatch.setattr(celery_app.conf, "broker_url", BROKER_LOCAL)
    # Celery da prioridad a esta variable sobre `conf`: si otro test ya fijo la configuracion,
    # el cambio de `conf` solo no basta y la suite completa fallaba contra el host `redis`.
    monkeypatch.setenv("CELERY_BROKER_URL", BROKER_LOCAL)
    # `inspect` usa el pool de conexiones de la app, que se crea una vez con la URL que habia
    # entonces: si otro test ya lo uso, hay que descartarlo para que tome la de este test.
    _descartar_conexiones()
    yield
    _descartar_conexiones()


@pytest_asyncio.fixture
async def redis_limpio() -> AsyncGenerator[aioredis.Redis, None]:
    cliente = aioredis.from_url(BROKER_LOCAL)
    colas = [q.name for q in celery_app.conf.task_queues]
    await cliente.delete(*colas)
    yield cliente
    await cliente.delete(*colas)
    await cliente.close()


async def test_las_colas_se_miden_en_redis(
    broker_local: None, redis_limpio: aioredis.Redis
) -> None:
    await redis_limpio.rpush("bulk", "a", "b", "c")
    await redis_limpio.rpush("webhooks", "x")

    async with _super_admin() as c:
        r = await c.get(f"{BASE}/celery/queues")

    assert r.status_code == 200, r.text
    colas = {q["name"]: q["pending"] for q in r.json()}
    assert colas["bulk"] == 3
    assert colas["webhooks"] == 1
    assert colas["media"] == 0  # las vacias tambien salen
    assert set(colas) == {q.name for q in celery_app.conf.task_queues}


async def test_sin_workers_con_el_broker_vivo_es_una_lista_vacia(broker_local: None) -> None:
    async with _super_admin() as c:
        r = await c.get(f"{BASE}/celery/workers")
        tareas = await c.get(f"{BASE}/celery/tasks")

    assert r.json() == {"broker_ok": True, "workers": []}
    assert tareas.json() == []


async def test_un_broker_inalcanzable_no_es_cero_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(celery_app.conf, "broker_url", "redis://localhost:1/0")
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://localhost:1/0")
    _descartar_conexiones()

    inicio = time.perf_counter()
    async with _super_admin() as c:
        r = await c.get(f"{BASE}/celery/workers")
    duracion = time.perf_counter() - inicio

    assert r.status_code == 200
    assert r.json() == {"broker_ok": False, "workers": []}
    assert duracion < 20, f"la pantalla no puede quedarse colgada: {duracion:.1f}s"


async def test_redis_devuelve_cifras_reales() -> None:
    async with _super_admin() as c:
        r = await c.get(f"{BASE}/redis")

    assert r.status_code == 200, r.text
    d: dict[str, Any] = r.json()
    assert d["version"][0].isdigit()
    assert d["uptime_seconds"] >= 0
    assert d["connected_clients"] >= 1  # al menos esta consulta
    assert d["used_memory"] > 0
    assert d["max_memory"] >= 0
    assert d["total_keys"] >= 0
    assert d["hit_ratio"] is None or 0 <= d["hit_ratio"] <= 1


async def test_el_estado_del_sistema_mide_cada_componente(broker_local: None) -> None:
    async with _super_admin() as c:
        r = await c.get(f"{BASE}/system")

    assert r.status_code == 200, r.text
    estado = {x["name"]: x for x in r.json()["components"]}
    assert estado["database"]["ok"] is True
    assert estado["redis"]["ok"] is True
    # No hay workers en el CI: el componente lo dice, sin tumbar la respuesta.
    assert estado["celery"]["ok"] is False
    assert estado["celery"]["detail"] == "RuntimeError"
    assert all(x["latency_ms"] > 0 for x in estado.values())
    assert r.json()["version"]
