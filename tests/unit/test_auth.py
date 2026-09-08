"""Tests unitarios para endpoints de autenticación.

Verifica login, refresh, tokens inválidos, roles y health check.
Usa httpx AsyncClient con ASGITransport (sin DB real).
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Configurar env ANTES de importar la app
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-testing-only-minimum-32-chars")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-minimum-32-characters-long")

from app.core.security import create_access_token, create_refresh_token, hash_password


def _make_user_mock(
    user_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    email: str = "admin@tenant.com",
    role: str = "admin",
    is_active: bool = True,
    password: str = "SecurePassword123",
) -> MagicMock:
    """Crear mock de User para tests."""
    user = MagicMock()
    user.id = user_id or uuid.uuid4()
    user.client_id = client_id or uuid.uuid4()
    user.email = email
    user.role = role
    user.is_active = is_active
    user.password_hash = hash_password(password)
    user.last_login_at = None
    return user


def _make_client_mock(client_id: uuid.UUID, is_active: bool = True) -> MagicMock:
    """Crear mock de Client para tests."""
    client = MagicMock()
    client.id = client_id
    client.is_active = is_active
    return client


@pytest_asyncio.fixture
async def client():
    """Cliente HTTP de test sin lifespan (evita conectar a DB/Redis reales)."""
    from app.main import create_app

    app = create_app()

    # Deshabilitar lifespan para unit tests
    app.router.lifespan_context = _noop_lifespan

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


from contextlib import asynccontextmanager

@asynccontextmanager
async def _noop_lifespan(app):
    yield


class TestHealthEndpoint:
    """Tests del endpoint /internal/health."""

    @pytest.mark.asyncio
    async def test_health_no_auth_required(self, client: AsyncClient) -> None:
        """Health endpoint responde sin token de autenticación."""
        with patch("app.api.internal.health.AsyncSessionLocal") as mock_db, \
             patch("app.api.internal.health.aioredis") as mock_redis:
            # Mock DB
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session.execute = AsyncMock()
            mock_db.return_value = mock_session

            # Mock Redis
            mock_redis_client = AsyncMock()
            mock_redis_client.ping = AsyncMock()
            mock_redis_client.aclose = AsyncMock()
            mock_redis.from_url.return_value = mock_redis_client

            resp = await client.get("/internal/health")
            assert resp.status_code in (200, 503)
            data = resp.json()
            assert "status" in data
            assert "checks" in data


class TestLoginEndpoint:
    """Tests del endpoint POST /api/v1/auth/login."""

    @pytest.mark.asyncio
    async def test_login_success(self, client: AsyncClient) -> None:
        """Login con credenciales válidas retorna tokens."""
        user = _make_user_mock(password="SecurePassword123")
        client_mock = _make_client_mock(user.client_id, is_active=True)

        with patch("app.api.v1.auth.AsyncSessionLocal") as mock_db:
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session.begin = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False)
            ))

            # Primera llamada: buscar User; Segunda: buscar Client
            mock_result_user = MagicMock()
            mock_result_user.scalar_one_or_none.return_value = user
            mock_result_client = MagicMock()
            mock_result_client.scalar_one_or_none.return_value = client_mock
            mock_session.execute = AsyncMock(
                side_effect=[mock_result_user, mock_result_client]
            )
            mock_db.return_value = mock_session

            resp = await client.post(
                "/api/v1/auth/login",
                json={"email": "admin@tenant.com", "password": "SecurePassword123"},
            )

            assert resp.status_code == 200
            data = resp.json()
            assert "access_token" in data
            assert "refresh_token" in data
            assert data["token_type"] == "bearer"

    @pytest.mark.asyncio
    async def test_login_invalid_password(self, client: AsyncClient) -> None:
        """Login con password incorrecto retorna 401."""
        user = _make_user_mock(password="SecurePassword123")

        with patch("app.api.v1.auth.AsyncSessionLocal") as mock_db:
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session.begin = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False)
            ))
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = user
            mock_session.execute = AsyncMock(return_value=mock_result)
            mock_db.return_value = mock_session

            resp = await client.post(
                "/api/v1/auth/login",
                json={"email": "admin@tenant.com", "password": "WrongPassword99"},
            )

            assert resp.status_code == 401
            assert resp.json()["error_code"] == "INVALID_TOKEN"

    @pytest.mark.asyncio
    async def test_login_user_not_found(self, client: AsyncClient) -> None:
        """Login con email inexistente retorna 401."""
        with patch("app.api.v1.auth.AsyncSessionLocal") as mock_db:
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session.begin = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False)
            ))
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = None
            mock_session.execute = AsyncMock(return_value=mock_result)
            mock_db.return_value = mock_session

            resp = await client.post(
                "/api/v1/auth/login",
                json={"email": "noexiste@test.com", "password": "Whatever12345"},
            )

            assert resp.status_code == 401


class TestProtectedEndpoints:
    """Tests de que endpoints protegidos rechazan requests sin JWT."""

    @pytest.mark.asyncio
    async def test_no_token_returns_401(self, client: AsyncClient) -> None:
        """Request sin token a endpoint protegido retorna 401."""
        resp = await client.get("/api/v1/auth/refresh")
        # refresh es POST, pero GET a cualquier ruta protegida sin token
        # debería ser rechazado por el middleware
        assert resp.status_code in (401, 405)

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(self, client: AsyncClient) -> None:
        """Request con token inválido retorna 401."""
        resp = await client.get(
            "/api/v1/some-protected-route",
            headers={"Authorization": "Bearer token.invalido.corrupto"},
        )
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "INVALID_TOKEN"

    @pytest.mark.asyncio
    async def test_expired_token_returns_401(self, client: AsyncClient) -> None:
        """Request con token expirado retorna 401."""
        token = create_access_token(
            {
                "user_id": str(uuid.uuid4()),
                "client_id": str(uuid.uuid4()),
                "email": "test@test.com",
                "role": "admin",
            },
            expires_delta=timedelta(seconds=-1),
        )
        import time
        time.sleep(0.1)

        resp = await client.get(
            "/api/v1/some-protected-route",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "TOKEN_EXPIRED"


class TestRefreshEndpoint:
    """Tests del endpoint POST /api/v1/auth/refresh."""

    @pytest.mark.asyncio
    async def test_refresh_success(self, client: AsyncClient) -> None:
        """Refresh con token válido retorna nuevo access_token."""
        token_data = {
            "user_id": str(uuid.uuid4()),
            "client_id": str(uuid.uuid4()),
            "email": "test@test.com",
            "role": "admin",
        }
        refresh = create_refresh_token(token_data)

        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["refresh_token"] == refresh

    @pytest.mark.asyncio
    async def test_refresh_with_access_token_fails(self, client: AsyncClient) -> None:
        """Usar access_token como refresh retorna 401."""
        token_data = {
            "user_id": str(uuid.uuid4()),
            "client_id": str(uuid.uuid4()),
            "email": "test@test.com",
            "role": "admin",
        }
        access = create_access_token(token_data)

        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": access},
        )

        assert resp.status_code == 401
        assert resp.json()["error_code"] == "INVALID_TOKEN"
