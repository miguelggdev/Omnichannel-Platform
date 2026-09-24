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
from collections.abc import AsyncGenerator, Generator

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
    """Registrar markers personalizados y setear env vars de testing."""
    # Env vars requeridas por Settings — valores de testing
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
    os.environ.setdefault("JWT_SECRET", "test-secret-key-minimum-32-characters-long!!")
    os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-minimum-32-characters!!")
    config.addinivalue_line(
        "markers",
        "db: marca tests que requieren PostgreSQL activo (usar --run-db)",
    )
    config.addinivalue_line(
        "markers",
        "slow: marca tests lentos que pueden omitirse con -m 'not slow'",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
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


# ─── Eventos salientes (Sprint 11) ──────────────────────────────────────────


@pytest.fixture(autouse=True)
def eventos_emitidos(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict]]:
    """Corta el encolado de `EventEmitter.emit()` hacia el broker.

    `emit()` encola `app.tasks.notification_dispatch_outgoing_webhooks`, y sin
    esto cualquier test que pase por un punto de emision intentaria hablar con
    el Redis del docker-compose. Es autouse a proposito: la lista de eventos
    solo la pide quien quiere afirmar sobre ella.

    Returns:
        Lista `(event, client_id, data)` que se va llenando con lo emitido.
    """
    from app.tasks import outgoing_webhooks

    registrados: list[tuple[str, str, dict]] = []

    def _registrar(*_args: object, **kwargs: object) -> None:
        # `emit()` siempre llama con `args=(event, client_id, data)`.
        event, client_id, data = kwargs["args"]  # type: ignore[misc]
        registrados.append((event, client_id, data))

    monkeypatch.setattr(outgoing_webhooks.dispatch_outgoing_webhooks, "apply_async", _registrar)
    return registrados


@pytest.fixture(autouse=True)
def csat_programada(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Corta el encolado de la encuesta CSAT hacia el broker.

    `app.tasks.csat_tasks` registra su handler sobre `conversation.resolved` al
    importarse (`app.main` lo importa para que el proceso de la API lo tenga),
    asi que cualquier test que resuelva una conversacion via
    `EventEmitter.emit()` — no solo los del propio CSAT — dispara
    `send_csat_survey.apply_async()` de verdad. Mismo motivo y mismo patron que
    `eventos_emitidos`: es autouse porque el efecto es transversal, no algo que
    solo le importe a los tests de CSAT.

    Returns:
        Lista `(client_id, conversation_id)` con lo que se programo.
    """
    from app.tasks import csat_tasks

    programadas: list[tuple[str, str]] = []

    def _registrar(*_args: object, **kwargs: object) -> None:
        datos = kwargs.get("kwargs") or {}
        programadas.append((datos.get("client_id"), datos.get("conversation_id")))  # type: ignore[arg-type]

    monkeypatch.setattr(csat_tasks.send_csat_survey, "apply_async", _registrar)
    return programadas


# ─── Database Fixtures (requieren --run-db) ─────────────────────────────────


@pytest_asyncio.fixture
async def db_engine():
    """Crear engine async para tests de integración.

    Scope de funcion, no de sesion: pytest-asyncio abre un event loop nuevo
    por test por defecto (asyncio_default_test_loop_scope=function), y un
    engine async de SQLAlchemy queda atado al loop en el que se creo. Un
    engine "session"-scoped sobrevive a ese loop y las conexiones se
    corrompen en el siguiente test (RuntimeError "attached to a different
    loop", o incluso SQL con errores de sintaxis erraticos por buffers de
    conexion reusados desde el loop equivocado).
    """
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

    async with AsyncSession(db_engine) as session, session.begin():
        yield session
        # El rollback es automático si no se hizo commit


@pytest_asyncio.fixture
async def tenant_session_a(db_engine, tenant_a_id) -> AsyncGenerator:
    """Sesión DB configurada como Tenant A (SET LOCAL)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(db_engine) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_client_id', :cid, true)"),
            {"cid": str(tenant_a_id)},
        )
        yield session


@pytest_asyncio.fixture
async def tenant_session_b(db_engine, tenant_b_id) -> AsyncGenerator:
    """Sesión DB configurada como Tenant B (SET LOCAL)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(db_engine) as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.current_client_id', :cid, true)"),
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

    # Configurar tenant context. set_config() acepta parametros bind; SET LOCAL no.
    await session_a.execute(
        text("SELECT set_config('app.current_client_id', :cid, true)"),
        {"cid": str(tenant_a_id)},
    )
    await session_b.execute(
        text("SELECT set_config('app.current_client_id', :cid, true)"),
        {"cid": str(tenant_b_id)},
    )

    # Asegurar que los tenants existen
    await session_a.execute(
        text("""
            INSERT INTO clients (id, name, slug, plan, is_active)
            VALUES (:id, 'Tenant A Test', 'tenant-a-test', 'free', true)
            ON CONFLICT (id) DO NOTHING
        """),
        {"id": str(tenant_a_id)},
    )
    await session_b.execute(
        text("""
            INSERT INTO clients (id, name, slug, plan, is_active)
            VALUES (:id, 'Tenant B Test', 'tenant-b-test', 'free', true)
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

    # Cleanup: rollback ambas transacciones.
    #
    # Las sesiones se cierran SIEMPRE, pase lo que pase con el rollback. Si un
    # test deja su transaccion cerrada por su cuenta (p.ej. llamando a
    # `session.rollback()`), `trans.rollback()` lanza ResourceClosedError; sin
    # este `finally` la excepcion abortaba el teardown y las dos conexiones se
    # quedaban "idle in transaction" reteniendo la fila de `clients` que inserta
    # este fixture. El siguiente test bloqueaba para siempre en su
    # `INSERT ... ON CONFLICT` sobre esa fila y la suite entera colgaba hasta el
    # limite de 6 h de GitHub Actions, sin un solo mensaje de error.
    #
    # `AsyncSession.close()` deshace cualquier transaccion viva y devuelve la
    # conexion al pool, asi que el aislamiento entre tests se mantiene igual.
    try:
        await trans_a.rollback()
        await trans_b.rollback()
    finally:
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
    Cliente HTTP con JWT de Tenant A y rol admin.

    El token se genera con `create_access_token`, no a mano: el payload tiene que
    traer exactamente las claims que lee `TenantContextMiddleware`
    (`user_id`, `client_id`, `role`) y firmarse con el `JWT_SECRET` de la app.

    Para otro rol, usar la factory `authenticated_client_factory`.
    """
    from app.core.security import create_access_token

    token = create_access_token(
        {
            "user_id": str(uuid.uuid4()),
            "client_id": str(tenant_a_id),
            "email": "test@example.com",
            "role": "admin",
        }
    )
    api_client.headers["Authorization"] = f"Bearer {token}"
    yield api_client


@pytest_asyncio.fixture
async def authenticated_client_factory(api_client, tenant_a_id):
    """
    Factory de clientes autenticados con el rol que pida el test.

    Uso:
        client = authenticated_client_factory(role="agent")
    """
    from app.core.security import create_access_token

    def _make(role: str = "admin", client_id=None, user_id=None):
        """Devuelve el cliente con un token del rol y tenant indicados."""
        token = create_access_token(
            {
                "user_id": str(user_id or uuid.uuid4()),
                "client_id": str(client_id or tenant_a_id),
                "email": "test@example.com",
                "role": role,
            }
        )
        api_client.headers["Authorization"] = f"Bearer {token}"
        return api_client

    yield _make


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
