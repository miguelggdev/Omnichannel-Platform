"""Feature flags contra PostgreSQL y Redis reales (Sprint 14, ADR-076).

Lo que los dobles no pueden probar: que el merge JSONB conserva el resto de
`agent_configs.config`, que RLS aisla las flags entre tenants, que el cache de
Redis se invalida de verdad y que una flag apagada saca al agente del router.
"""

import json
import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


def _url_admin() -> str:
    return os.environ.get(
        "DATABASE_URL_ADMIN",
        "postgresql+asyncpg://test_user:test_password@localhost:5432/test_omnichannel",
    )


@pytest_asyncio.fixture(autouse=True)
async def _redis_por_test() -> AsyncGenerator[None, None]:
    """Un cliente Redis por test: el global queda atado al event loop que lo creo."""
    from app.services.dedup import close_redis

    await close_redis()
    yield
    await close_redis()


async def _crear_tenant(config: dict[str, Any]) -> uuid.UUID:
    from app.core.database import engine, tenant_session

    await engine.dispose()
    client_id = uuid.uuid4()
    async with tenant_session(client_id) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active) "
                "VALUES (:id, 'Tenant Flags', :slug, 'free', true)"
            ),
            {"id": str(client_id), "slug": f"flags-{client_id.hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO agent_configs (client_id, name, config) "
                "VALUES (:cid, 'asistente', CAST(:config AS jsonb))"
            ),
            {"cid": str(client_id), "config": json.dumps(config)},
        )
    return client_id


async def _borrar(tenants: list[uuid.UUID]) -> None:
    from app.services.dedup import get_redis

    admin = create_async_engine(_url_admin())
    try:
        async with admin.begin() as conexion:
            for client_id in tenants:
                for tabla in ("agent_configs",):
                    await conexion.execute(
                        text(f"DELETE FROM {tabla} WHERE client_id = :cid"),  # noqa: S608
                        {"cid": str(client_id)},
                    )
                await conexion.execute(
                    text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)}
                )
    finally:
        await admin.dispose()
    await get_redis().delete(*[f"ff:{t}" for t in tenants])


@pytest_asyncio.fixture
async def tenant() -> AsyncGenerator[uuid.UUID, None]:
    client_id = await _crear_tenant(
        {"enabled_agents": ["rag", "clinical", "marketing"], "rag_top_k": 7}
    )
    yield client_id
    await _borrar([client_id])


async def _config(client_id: uuid.UUID) -> dict[str, Any]:
    from app.core.database import tenant_session

    async with tenant_session(client_id) as session:
        fila = (
            await session.execute(
                text("SELECT config FROM agent_configs WHERE client_id = :cid"),
                {"cid": str(client_id)},
            )
        ).one()
    return dict(fila[0])


async def test_set_flag_conserva_el_resto_del_config_y_se_acumula(tenant: uuid.UUID) -> None:
    from app.core.feature_flags import FeatureFlags

    servicio = FeatureFlags()

    await servicio.set_flag(tenant, "enable_clinical", False)
    await servicio.set_flag(tenant, "enable_reranking", 25)

    config = await _config(tenant)
    assert config["enabled_agents"] == ["rag", "clinical", "marketing"]
    assert config["rag_top_k"] == 7
    assert config["feature_flags"] == {"enable_clinical": False, "enable_reranking": 25}


async def test_una_flag_apagada_saca_al_agente_del_router_y_al_encenderla_vuelve(
    tenant: uuid.UUID,
) -> None:
    from app.agents.nodes._tenant import get_agent_settings
    from app.agents.nodes.intent_router import available_intents
    from app.core.feature_flags import FeatureFlags

    servicio = FeatureFlags()
    antes = await get_agent_settings(tenant)
    assert antes.enabled_agents == ("rag", "clinical", "marketing")

    await servicio.set_flag(tenant, "enable_clinical", False)
    apagada = await get_agent_settings(tenant)

    assert apagada.enabled_agents == ("rag", "marketing")
    assert "clinical" not in available_intents(apagada.enabled_agents)

    await servicio.set_flag(tenant, "enable_clinical", True)
    encendida = await get_agent_settings(tenant)

    assert encendida.enabled_agents == ("rag", "clinical", "marketing")


async def test_las_flags_de_un_tenant_no_las_ve_otro() -> None:
    from app.agents.nodes._tenant import get_agent_settings
    from app.core.database import tenant_session
    from app.core.feature_flags import FeatureFlags

    a = await _crear_tenant({"enabled_agents": ["rag", "clinical"]})
    b = await _crear_tenant({"enabled_agents": ["rag", "clinical"]})
    try:
        servicio = FeatureFlags()
        await servicio.set_flag(a, "enable_clinical", False)

        assert await servicio.get_all(b) == {}
        assert (await get_agent_settings(b)).enabled_agents == ("rag", "clinical")
        # RLS: desde el contexto de B la fila de A ni existe.
        async with tenant_session(b) as session:
            filas = (
                await session.execute(
                    text("SELECT 1 FROM agent_configs WHERE client_id = :a"), {"a": str(a)}
                )
            ).all()
        assert filas == []
    finally:
        await _borrar([a, b])


async def test_el_cache_de_redis_se_llena_con_ttl_y_se_invalida_al_escribir(
    tenant: uuid.UUID,
) -> None:
    from app.core.feature_flags import FeatureFlags
    from app.services.dedup import get_redis

    servicio = FeatureFlags()
    clave = f"ff:{tenant}"
    redis = get_redis()

    assert await servicio.get_all(tenant) == {}
    assert await redis.get(clave) == "{}"
    assert 290 <= await redis.ttl(clave) <= 300

    await servicio.set_flag(tenant, "enable_clinical", False)
    assert await redis.get(clave) is None

    assert await servicio.get_all(tenant) == {"enable_clinical": False}
    assert json.loads(await redis.get(clave)) == {"enable_clinical": False}


async def test_un_cache_viejo_se_corrige_al_invalidar_aunque_otro_proceso_escriba(
    tenant: uuid.UUID,
) -> None:
    """Dos instancias de `FeatureFlags` (API y worker) comparten el mismo Redis."""
    from app.core.feature_flags import FeatureFlags

    api, worker = FeatureFlags(), FeatureFlags()
    assert await worker.get_all(tenant) == {}

    await api.set_flag(tenant, "enable_marketing", False)

    assert await worker.get_all(tenant) == {"enable_marketing": False}


async def test_la_api_cambia_la_flag_de_punta_a_punta(
    tenant: uuid.UUID, authenticated_client_factory: Any
) -> None:
    from app.agents.nodes._tenant import get_agent_settings

    cliente = authenticated_client_factory(role="admin", client_id=tenant)

    respuesta = await cliente.put(
        "/api/v1/admin/feature-flags/enable_clinical", json={"value": False}
    )

    assert respuesta.status_code == 200, respuesta.text
    valores = {f["flag"]: f["value"] for f in respuesta.json()["flags"]}
    assert valores["enable_clinical"] is False
    assert (await get_agent_settings(tenant)).enabled_agents == ("rag", "marketing")

    listado = await cliente.get("/api/v1/admin/feature-flags")
    assert {f["flag"]: f["value"] for f in listado.json()["flags"]}["enable_clinical"] is False


async def test_un_tenant_sin_agente_activo_no_puede_guardar_flags() -> None:
    from app.core.database import engine, tenant_session
    from app.core.feature_flags import FeatureFlags, SinAgenteConfigurableError

    await engine.dispose()
    client_id = uuid.uuid4()
    async with tenant_session(client_id) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active) "
                "VALUES (:id, 'Sin agente', :slug, 'free', true)"
            ),
            {"id": str(client_id), "slug": f"sin-agente-{client_id.hex[:8]}"},
        )
    try:
        with pytest.raises(SinAgenteConfigurableError):
            await FeatureFlags().set_flag(client_id, "enable_clinical", True)
    finally:
        await _borrar([client_id])
