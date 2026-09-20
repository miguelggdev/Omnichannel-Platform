"""Tests unitarios para endpoints de autenticación.

Verifica login, refresh, tokens inválidos, roles y health check.
Usa httpx AsyncClient con ASGITransport (sin DB real).
"""

import os
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
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


class _FilaAuth:
    """Fila que devuelve `auth_lookup_user()`: usuario y estado de su tenant."""

    def __init__(self, **kwargs: object) -> None:
        """Construye la fila con valores por defecto razonables."""
        self.id = kwargs.get("id") or uuid.uuid4()
        self.client_id = kwargs.get("client_id") or uuid.uuid4()
        self.email = kwargs.get("email", "admin@tenant.com")
        self.password_hash = kwargs["password_hash"]
        self.first_name = kwargs.get("first_name", "Ada")
        self.last_name = kwargs.get("last_name", "Admin")
        self.role = kwargs.get("role", "admin")
        self.is_active = kwargs.get("is_active", True)
        self.client_is_active = kwargs.get("client_is_active", True)


def _fila_auth(password: str = "SecurePassword123", **kwargs: object) -> _FilaAuth:
    """Arma la fila de `auth_lookup_user()` con el hash ya calculado.

    Args:
        password: Password en claro del que sale el hash.
        **kwargs: Campos a sobreescribir.

    Returns:
        La fila lista para que la devuelva la sesión falsa.
    """
    return _FilaAuth(password_hash=hash_password(password), **kwargs)


class _SesionDeLogin:
    """Sesión falsa que devuelve una única fila y registra lo que se le pidió."""

    def __init__(self, fila: _FilaAuth | None) -> None:
        """Guarda la fila que devolverá la consulta."""
        self.fila = fila
        self.ejecutadas: list[object] = []

    async def __aenter__(self) -> "_SesionDeLogin":
        """Entra al contexto."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Sale del contexto."""
        return

    @asynccontextmanager
    async def begin(self):
        """`login` abre la transacción con `session.begin()`."""
        yield

    async def execute(self, stmt: object = None, params: object = None) -> "_SesionDeLogin":
        """Registra la sentencia y se devuelve como resultado."""
        self.ejecutadas.append(stmt)
        return self

    def one_or_none(self) -> _FilaAuth | None:
        """Devuelve la fila prefijada."""
        return self.fila


def _tenant_session_falso(registro: list | None = None):
    """Sustituto de `tenant_session` que anota con qué contexto se abrió.

    Args:
        registro: Lista donde apuntar los `(client_id, user_id)` recibidos.

    Returns:
        Un context manager asíncrono con la misma firma.
    """

    @asynccontextmanager
    async def _cm(client_id, user_id=None):
        if registro is not None:
            registro.append((client_id, user_id))
        yield _SesionDeLogin(None)

    return _cm


def _login_falso(fila: _FilaAuth | None):
    """Parchea de una vez la búsqueda y la escritura del último acceso.

    Args:
        fila: Lo que devuelve `auth_lookup_user()`; None si el email no existe.

    Returns:
        Context manager que aplica los dos parches.
    """
    from contextlib import ExitStack

    pila = ExitStack()
    pila.enter_context(
        patch("app.api.v1.auth.AsyncSessionLocal", return_value=_SesionDeLogin(fila))
    )
    pila.enter_context(patch("app.api.v1.auth.tenant_session", _tenant_session_falso()))
    return pila


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


@asynccontextmanager
async def _noop_lifespan(app):
    yield


class TestHealthEndpoint:
    """Tests del endpoint /internal/health."""

    @pytest.mark.asyncio
    async def test_health_no_auth_required(self, client: AsyncClient) -> None:
        """Health endpoint responde sin token de autenticación."""
        with (
            patch("app.api.internal.health.AsyncSessionLocal") as mock_db,
            patch("app.api.internal.health.aioredis") as mock_redis,
        ):
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
    """Tests del endpoint POST /api/v1/auth/login.

    Desde BUG-025 el login no usa el ORM: busca con `auth_lookup_user()`, una
    funcion SECURITY DEFINER que devuelve una fila con todo lo que hace falta
    (incluido el estado del tenant), porque `users` y `clients` tienen RLS y
    aqui todavia no se sabe a que tenant pertenece el email.
    """

    @pytest.mark.asyncio
    async def test_login_success(self, client: AsyncClient) -> None:
        """Login con credenciales válidas retorna tokens."""
        fila = _fila_auth(password="SecurePassword123")

        with _login_falso(fila):
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
    async def test_login_una_sola_consulta_sin_contexto_de_tenant(
        self, client: AsyncClient
    ) -> None:
        """El estado del tenant viene en la misma fila, no en una segunda consulta.

        `clients` tambien tiene RLS: consultarla aparte desde aqui volveria a
        chocar con lo mismo que BUG-025.
        """
        fila = _fila_auth()
        sesion = _SesionDeLogin(fila)

        with (
            patch("app.api.v1.auth.AsyncSessionLocal", return_value=sesion),
            patch("app.api.v1.auth.tenant_session", _tenant_session_falso()),
        ):
            await client.post(
                "/api/v1/auth/login",
                json={"email": "admin@tenant.com", "password": "SecurePassword123"},
            )

        assert len(sesion.ejecutadas) == 1
        assert "auth_lookup_user" in str(sesion.ejecutadas[0])

    @pytest.mark.asyncio
    async def test_login_invalid_password(self, client: AsyncClient) -> None:
        """Login con password incorrecto retorna 401."""
        with _login_falso(_fila_auth(password="SecurePassword123")):
            resp = await client.post(
                "/api/v1/auth/login",
                json={"email": "admin@tenant.com", "password": "WrongPassword99"},
            )

        assert resp.status_code == 401
        assert resp.json()["error_code"] == "INVALID_TOKEN"

    @pytest.mark.asyncio
    async def test_login_user_not_found(self, client: AsyncClient) -> None:
        """Login con email inexistente retorna 401."""
        with _login_falso(None):
            resp = await client.post(
                "/api/v1/auth/login",
                json={"email": "noexiste@test.com", "password": "Whatever12345"},
            )

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_email_inexistente_tambien_pasa_por_bcrypt(self, client: AsyncClient) -> None:
        """Sin usuario, el login gasta el mismo tiempo que con uno existente.

        Si un email inexistente respondiera sin llamar a bcrypt (~100 ms) y uno
        existente si, la diferencia de latencia permitiria enumerar los emails
        registrados. Se verifica que `verify_password` se ejecuta igual.
        """
        llamadas: list[tuple[str, str]] = []

        def _espia(plano: str, hash_: str) -> bool:
            llamadas.append((plano, hash_))
            return False

        with _login_falso(None), patch("app.api.v1.auth.verify_password", _espia):
            resp = await client.post(
                "/api/v1/auth/login",
                json={"email": "noexiste@test.com", "password": "Whatever12345"},
            )

        assert resp.status_code == 401
        assert resp.json()["error_code"] == "INVALID_TOKEN"
        assert len(llamadas) == 1
        assert llamadas[0][0] == "Whatever12345"
        assert llamadas[0][1].startswith("$2"), "debe verificarse contra un hash bcrypt real"

    @pytest.mark.asyncio
    async def test_bcrypt_corre_fuera_del_event_loop(self, client: AsyncClient) -> None:
        """bcrypt es CPU (~100 ms): en el hilo del event loop congelaria la API.

        CLAUDE.md, regla 4. Se compara el hilo donde corre `verify_password` con
        el del event loop del test, que es el mismo que atiende la peticion.
        """
        import threading

        hilos: list[int] = []

        def _espia(plano: str, hash_: str) -> bool:
            hilos.append(threading.get_ident())
            return True

        with (
            _login_falso(_fila_auth(password="SecurePassword123")),
            patch("app.api.v1.auth.verify_password", _espia),
        ):
            resp = await client.post(
                "/api/v1/auth/login",
                json={"email": "admin@tenant.com", "password": "SecurePassword123"},
            )

        assert resp.status_code == 200
        assert hilos, "verify_password no se ejecuto"
        assert threading.get_ident() not in hilos

    @pytest.mark.asyncio
    async def test_login_usuario_desactivado(self, client: AsyncClient) -> None:
        """Un usuario dado de baja no entra, aunque el password sea correcto."""
        with _login_falso(_fila_auth(is_active=False)):
            resp = await client.post(
                "/api/v1/auth/login",
                json={"email": "admin@tenant.com", "password": "SecurePassword123"},
            )

        assert resp.status_code == 401
        assert resp.json()["error_code"] == "FORBIDDEN"

    @pytest.mark.asyncio
    async def test_login_tenant_suspendido(self, client: AsyncClient) -> None:
        """Si la organización está suspendida, ninguno de sus usuarios entra."""
        with _login_falso(_fila_auth(client_is_active=False)):
            resp = await client.post(
                "/api/v1/auth/login",
                json={"email": "admin@tenant.com", "password": "SecurePassword123"},
            )

        assert resp.status_code == 401
        assert resp.json()["error_code"] == "FORBIDDEN"

    @pytest.mark.asyncio
    async def test_login_registra_el_ultimo_acceso_con_contexto_de_tenant(
        self, client: AsyncClient
    ) -> None:
        """El UPDATE de `last_login_at` va dentro del tenant, no suelto.

        Es la otra mitad de BUG-025: escribir en `users` sin contexto tambien
        choca con la RLS. Aqui ya se conoce el tenant, asi que vuelve al camino
        normal y ademas queda firmado por el propio usuario.
        """
        fila = _fila_auth()
        contextos: list[tuple] = []

        with (
            patch("app.api.v1.auth.AsyncSessionLocal", return_value=_SesionDeLogin(fila)),
            patch("app.api.v1.auth.tenant_session", _tenant_session_falso(contextos)),
        ):
            resp = await client.post(
                "/api/v1/auth/login",
                json={"email": "admin@tenant.com", "password": "SecurePassword123"},
            )

        assert resp.status_code == 200
        assert contextos == [(fila.client_id, fila.id)]

    @pytest.mark.asyncio
    async def test_un_fallo_al_anotar_el_acceso_no_tumba_el_login(
        self, client: AsyncClient
    ) -> None:
        """La fecha de último acceso es contabilidad, no autenticación.

        Negar un login válido por no poder escribir un dato accesorio dejaría al
        usuario fuera por algo que no tiene que ver con sus credenciales.
        """
        fila = _fila_auth()

        def _revienta(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("base caida")

        with (
            patch("app.api.v1.auth.AsyncSessionLocal", return_value=_SesionDeLogin(fila)),
            patch("app.api.v1.auth.tenant_session", _revienta),
        ):
            resp = await client.post(
                "/api/v1/auth/login",
                json={"email": "admin@tenant.com", "password": "SecurePassword123"},
            )

        assert resp.status_code == 200


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
        import asyncio

        await asyncio.sleep(0.1)

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
