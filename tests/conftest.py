"""
conftest.py — Fixtures compartidas para toda la suite de tests.

Proporciona:
  - Opción --run-db para tests que requieren PostgreSQL
  - Fixtures de tenant para aislamiento RLS
  - Fixtures de sesión DB async con SET LOCAL
  - Cliente HTTP de prueba (httpx)
  - Helpers de factories para crear datos de test

Uso:
    pytest tests/ -v                  # Solo unit tests (sin DB)
    pytest tests/ -v --run-db         # Incluye tests de integración con DB
"""

import asyncio
import os
import uuid
from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio

# ─── Opciones de CLI ────────────────────────────────────────────────────────


def pytest_addoption(parser: pytest.Parser) -> None:
    """Agregar opciones personalizadas al parser de pytest."""
    parser.addoption(
        "--run-db",
        action="store_true",
        default=False,
        help="Ejecutar tests que requieren PostgreSQL activo",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Registrar markers personalizados."""
    config.addinivalue_line(
        "markers",
        "db: marca tests que requieren PostgreSQL activo (usar --run-db)",
    )
    config.addinivalue_line(
        "markers",
        "slow: marca tests lentos que pueden omitirse con -m 'not slow'",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Omitir tests marcados con @pytest.mark.db si no se pasa --run-db."""
    if config.getoption("--run-db"):
        return

    skip_db = pytest.mark.skip(reason="Requiere --run-db y PostgreSQL activo")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip_db)


# ─── Event Loop ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Crear un event loop para toda la sesión de tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ─── Tenant IDs de prueba ───────────────────────────────────────────────────

TENANT_A_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TENANT_B_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
TENANT_C_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
SUPERADMIN_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


@pytest.fixture(scope="session")
def tenant_ids() -> dict[str, uuid.UUID]:
    """IDs fijos de tenant para tests de aislamiento."""
    return {
        "tenant_a": TENANT_A_ID,
        "tenant_b": TENANT_B_ID,
        "tenant_c": TENANT_C_ID,
        "superadmin": SUPERADMIN_ID,
    }


@pytest.fixture
def tenant_a_id() -> uuid.UUID:
    """UUID fijo para Tenant A en tests."""
    return TENANT_A_ID


@pytest.fixture
def tenant_b_id() -> uuid.UUID:
    """UUID fijo para Tenant B en tests."""
    return TENANT_B_ID


@pytest.fixture
def tenant_c_id() -> uuid.UUID:
    """UUID fijo para Tenant C (tercer tenant para tests de aislamiento cruzado)."""
    return TENANT_C_ID


@pytest.fixture
def random_tenant_id() -> uuid.UUID:
    """Genera un tenant ID aleatorio para tests que necesitan unicidad."""
    return uuid.uuid4()


# ─── Database Fixtures (requieren --run-db) ─────────────────────────────────


@pytest_asyncio.fixture(scope="session")
async def db_engine():
    """Crear engine async para tests de integración."""
    from sqlalchemy.ext.asyncio import create_async_engine

    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://test_user:test_password@localhost:5432/test_omnichannel",
    )
    engine = create_async_engine(database_url, echo=False, pool_size=5)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncGenerator:
    """Sesión DB async con rollback automático al final del test."""
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(db_engine) as session:
        async with session.begin():
            yield session
        # El rollback es automático si no se hizo commit


@pytest_asyncio.fixture
async def tenant_session_a(db_engine, tenant_a_id) -> AsyncGenerator:
    """Sesión DB configurada como Tenant A (SET LOCAL)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(db_engine) as session:
        async with session.begin():
            await session.execute(
                text("SET LOCAL app.current_client_id = :cid"),
                {"cid": str(tenant_a_id)},
            )
            yield session


@pytest_asyncio.fixture
async def tenant_session_b(db_engine, tenant_b_id) -> AsyncGenerator:
    """Sesión DB configurada como Tenant B (SET LOCAL)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(db_engine) as session:
        async with session.begin():
            await session.execute(
                text("SET LOCAL app.current_client_id = :cid"),
                {"cid": str(tenant_b_id)},
            )
            yield session


@pytest_asyncio.fixture
async def rls_harness(db_engine, tenant_a_id, tenant_b_id) -> dict:
    """
    Harness completo para tests de aislamiento RLS.

    Retorna dict con:
      - session_a: sesión como Tenant A
      - session_b: sesión como Tenant B
      - tenant_a: UUID de Tenant A
      - tenant_b: UUID de Tenant B

    Ambas sesiones están dentro de transacciones con SET LOCAL.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    session_a = AsyncSession(db_engine)
    session_b = AsyncSession(db_engine)

    # Iniciar transacciones
    trans_a = await session_a.begin()
    trans_b = await session_b.begin()

    # Configurar tenant context
    await session_a.execute(
        text("SET LOCAL app.current_client_id = :cid"),
        {"cid": str(tenant_a_id)},
    )
    await session_b.execute(
        text("SET LOCAL app.current_client_id = :cid"),
        {"cid": str(tenant_b_id)},
    )

    # Asegurar que los tenants existen
    await session_a.execute(
        text("""
            INSERT INTO clients (id, name, slug, plan, max_agents, is_active)
            VALUES (:id, 'Tenant A Test', 'tenant-a-test', 'free', 1, true)
            ON CONFLICT (id) DO NOTHING
        """),
        {"id": str(tenant_a_id)},
    )
    await session_b.execute(
        text("""
            INSERT INTO clients (id, name, slug, plan, max_agents, is_active)
            VALUES (:id, 'Tenant B Test', 'tenant-b-test', 'free', 1, true)
            ON CONFLICT (id) DO NOTHING
        """),
        {"id": str(tenant_b_id)},
    )

    yield {
        "session_a": session_a,
        "session_b": session_b,
        "tenant_a": tenant_a_id,
        "tenant_b": tenant_b_id,
    }

    # Cleanup: rollback ambas transacciones
    await trans_a.rollback()
    await trans_b.rollback()
    await session_a.close()
    await session_b.close()


# ─── HTTP Client Fixture ────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def api_client() -> AsyncGenerator:
    """Cliente HTTP para tests de API (e2e)."""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def authenticated_client(api_client, tenant_a_id) -> AsyncGenerator:
    """
    Cliente HTTP con JWT de Tenant A.

    Incluye header Authorization con un token válido.
    El token se genera con el JWT_SECRET de testing.
    """
    import jwt

    secret = os.getenv("JWT_SECRET", "test-secret-key-for-testing-only")
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "client_id": str(tenant_a_id),
            "role": "admin",
            "exp": 9999999999,
        },
        secret,
        algorithm="HS256",
    )
    api_client.headers["Authorization"] = f"Bearer {token}"
    yield api_client


# ─── Factory Helpers ─────────────────────────────────────────────────────────


class TestDataFactory:
    """Factory para crear datos de test de forma consistente."""

    @staticmethod
    def contact(
        client_id: uuid.UUID,
        phone: str = "+5215500000001",
        channel: str = "whatsapp",
    ) -> dict:
        """Generar datos de un contacto de prueba."""
        return {
            "id": str(uuid.uuid4()),
            "client_id": str(client_id),
            "phone_number": phone,
            "channel": channel,
        }

    @staticmethod
    def conversation(
        client_id: uuid.UUID,
        contact_id: uuid.UUID,
        status: str = "active",
        channel: str = "whatsapp",
    ) -> dict:
        """Generar datos de una conversación de prueba."""
        return {
            "id": str(uuid.uuid4()),
            "client_id": str(client_id),
            "contact_id": str(contact_id),
            "status": status,
            "channel": channel,
        }

    @staticmethod
    def message(
        client_id: uuid.UUID,
        conversation_id: uuid.UUID,
        direction: str = "inbound",
        content: str = "test message",
    ) -> dict:
        """Generar datos de un mensaje de prueba."""
        return {
            "id": str(uuid.uuid4()),
            "client_id": str(client_id),
            "conversation_id": str(conversation_id),
            "direction": direction,
            "content": content,
            "channel": "whatsapp",
        }

    @staticmethod
    def document(
        client_id: uuid.UUID,
        title: str = "Test Document",
        doc_type: str = "pdf",
    ) -> dict:
        """Generar datos de un documento de prueba."""
        return {
            "id": str(uuid.uuid4()),
            "client_id": str(client_id),
            "title": title,
            "doc_type": doc_type,
            "status": "active",
        }


@pytest.fixture
def factory() -> TestDataFactory:
    """Instancia del factory de datos de test."""
    return TestDataFactory()
