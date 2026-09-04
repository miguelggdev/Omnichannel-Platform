"""
Fixtures globales para el proyecto omnichannel-platform.

Incluye:
- RLS Test Harness: fixtures para validar aislamiento multi-tenant
- Database session fixtures con SET LOCAL
- Test client para FastAPI
"""

import uuid
from typing import AsyncGenerator

import pytest
import pytest_asyncio

# ─── RLS Test Harness ───────────────────────────────────────────────────────────
# Estas fixtures proveen el andamiaje necesario para verificar que las políticas
# RLS aíslan correctamente los datos entre tenants.
#
# Uso en tests:
#   async def test_isolation(rls_harness):
#       tenant_a = rls_harness["tenant_a"]
#       tenant_b = rls_harness["tenant_b"]
#       session_a = rls_harness["session_a"]  # con SET LOCAL de tenant A
#       session_b = rls_harness["session_b"]  # con SET LOCAL de tenant B
# ─────────────────────────────────────────────────────────────────────────────────


# IDs estables para tests (determinísticos para reproducibilidad)
TENANT_A_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TENANT_B_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SUPERADMIN_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


@pytest.fixture(scope="session")
def tenant_ids() -> dict[str, uuid.UUID]:
    """IDs fijos de tenant para tests de aislamiento."""
    return {
        "tenant_a": TENANT_A_ID,
        "tenant_b": TENANT_B_ID,
        "superadmin": SUPERADMIN_ID,
    }


@pytest.fixture
def random_tenant_id() -> uuid.UUID:
    """Genera un tenant ID aleatorio para tests que necesitan unicidad."""
    return uuid.uuid4()


# ─── Database Fixtures (se activarán cuando exista app/core/database.py) ──────
# Las siguientes fixtures están comentadas como stubs hasta que el Sprint 1
# genere la infraestructura de base de datos. Descomentar cuando estén
# disponibles: AsyncSession, engine, etc.

# @pytest_asyncio.fixture(scope="session")
# async def db_engine():
#     """Crea un engine async para tests (usa DATABASE_URL de test)."""
#     from app.core.database import create_engine
#     engine = create_engine(testing=True)
#     yield engine
#     await engine.dispose()

# @pytest_asyncio.fixture
# async def db_session(db_engine) -> AsyncGenerator:
#     """Sesión de base de datos para un test individual."""
#     from sqlalchemy.ext.asyncio import AsyncSession
#     async with AsyncSession(db_engine) as session:
#         async with session.begin():
#             yield session
#         await session.rollback()

# @pytest_asyncio.fixture
# async def tenant_session_a(db_session) -> AsyncGenerator:
#     """Sesión con SET LOCAL para Tenant A."""
#     from sqlalchemy import text
#     await db_session.execute(
#         text("SET LOCAL app.current_client_id = :cid"),
#         {"cid": str(TENANT_A_ID)},
#     )
#     yield db_session

# @pytest_asyncio.fixture
# async def tenant_session_b(db_session) -> AsyncGenerator:
#     """Sesión con SET LOCAL para Tenant B."""
#     from sqlalchemy import text
#     await db_session.execute(
#         text("SET LOCAL app.current_client_id = :cid"),
#         {"cid": str(TENANT_B_ID)},
#     )
#     yield db_session

# @pytest_asyncio.fixture
# async def rls_harness(db_engine) -> AsyncGenerator[dict, None]:
#     """
#     Harness completo para tests de aislamiento RLS.
#
#     Provee dos sesiones independientes, cada una con su tenant ID,
#     dentro de transacciones separadas con rollback automático.
#
#     Returns:
#         dict con keys: tenant_a, tenant_b, session_a, session_b
#     """
#     from sqlalchemy import text
#     from sqlalchemy.ext.asyncio import AsyncSession
#
#     async with AsyncSession(db_engine) as session_a, \
#                AsyncSession(db_engine) as session_b:
#         async with session_a.begin(), session_b.begin():
#             await session_a.execute(
#                 text("SET LOCAL app.current_client_id = :cid"),
#                 {"cid": str(TENANT_A_ID)},
#             )
#             await session_b.execute(
#                 text("SET LOCAL app.current_client_id = :cid"),
#                 {"cid": str(TENANT_B_ID)},
#             )
#             yield {
#                 "tenant_a": TENANT_A_ID,
#                 "tenant_b": TENANT_B_ID,
#                 "session_a": session_a,
#                 "session_b": session_b,
#             }
#         # Rollback automático — no se persisten datos de test


# ─── FastAPI Test Client (stub) ──────────────────────────────────────────────
# @pytest_asyncio.fixture
# async def test_client():
#     """HTTP test client con tenant headers."""
#     from httpx import ASGITransport, AsyncClient
#     from app.main import create_app
#     app = create_app()
#     async with AsyncClient(
#         transport=ASGITransport(app=app),
#         base_url="http://test",
#     ) as client:
#         yield client
