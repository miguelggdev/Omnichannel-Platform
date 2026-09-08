# Sprint 3 — FastAPI Core & Auth

## Objetivo
App factory completa, middleware stack, autenticacion JWT, RBAC y modelos SQLAlchemy para las 18 tablas MVP. Al finalizar este sprint, la API acepta requests autenticados, aplica RLS via middleware y rechaza accesos no autorizados.

## Prerequisitos
- Sprint 2 completado: infraestructura Docker funcionando, todos los servicios healthy
- Redis accesible para cache
- Python 3.12 con las dependencias: fastapi, uvicorn, sqlalchemy[asyncio], asyncpg, pydantic-settings, python-jose[cryptography], passlib[bcrypt], redis[hiredis], pydantic-settings

> **ADR-020**: PostgreSQL, Auth, Storage y Realtime son provistos por **Supabase Cloud**.
> No se levantan contenedores locales para estos componentes.
> `DATABASE_URL` apunta al Transaction Pooler de Supabase Cloud (Supavisor, puerto 6543).
> Ya NO existe `PGBOUNCER_URL` — el pooling lo gestiona Supavisor de forma transparente.

## Archivos a Crear

### Estructura del proyecto
```
app/
  __init__.py
  main.py                          # App factory con lifespan
  core/
    __init__.py
    config.py                      # Pydantic BaseSettings
    security.py                    # JWT, password hashing
    database.py                    # Async SQLAlchemy 2.0, SET LOCAL helper
    dependencies.py                # DI: TenantSession, CurrentUser, require_role
    exceptions.py                  # AppException y handlers
  middleware/
    __init__.py
    tenant_context.py              # JWT → SET LOCAL
    token_budget.py                # TokenBudgetGuard (placeholder)
  api/
    __init__.py
    v1/
      __init__.py
      auth.py                      # Login, refresh token
    internal/
      __init__.py
      health.py                    # Health check endpoint
  models/
    __init__.py
    base.py                        # TenantBaseModel con client_id
    client.py
    user.py
    contact.py
    contact_identifier.py
    tag.py
    contact_tag.py
    internal_note.py
    conversation.py
    message.py
    document.py
    document_chunk.py
    token_budget.py
    token_usage_log.py
    webhook_dedup.py
    agent_config.py
    quick_reply.py
    pending_response.py
    approved_response.py
  schemas/
    __init__.py
    auth.py                        # LoginRequest, TokenResponse
    client.py
    user.py
    contact.py
    conversation.py
    message.py
    document.py
    agent_config.py
    common.py                      # PaginatedResponse, ErrorResponse
tests/
  __init__.py
  conftest.py                      # Fixtures: async client, tenant session, test DB
  unit/
    __init__.py
    test_rls_isolation.py
    test_auth.py
    test_security.py
```

## Tareas Detalladas

### 1. Config (`app/core/config.py`)

Usar Pydantic BaseSettings v2 con validacion estricta:

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Database — Supavisor (Transaction Pooler de Supabase Cloud, puerto 6543)
    DATABASE_URL: str
    # Conexion directa — SOLO para migraciones Alembic (no soporta pooling)
    DATABASE_URL_DIRECT: str = ""

    # Supabase Cloud
    SUPABASE_URL: str = ""
    SUPABASE_PUBLISHABLE_KEY: str = ""
    SUPABASE_SECRET_KEY: str = ""

    # Redis
    REDIS_URL: str = "redis://redis:6379/0"

    # JWT
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_MINUTES: int = 30
    JWT_REFRESH_EXPIRATION_DAYS: int = 7

    # API Keys
    OPENAI_API_KEY: str
    YCLOUD_API_KEY: str = ""
    YCLOUD_WEBHOOK_SECRET: str = ""

    # Cifrado
    ENCRYPTION_KEY: str

    # App
    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # Embedding
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    OPENAI_CHAT_MODEL: str = "gpt-4o"


settings = Settings()
```

Validaciones requeridas:
- `JWT_SECRET` debe tener minimo 32 caracteres
- `ENCRYPTION_KEY` debe tener minimo 32 caracteres
- `DATABASE_URL` debe empezar con `postgres`

### 2. Database Layer (`app/core/database.py`)

```python
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
)
from sqlalchemy import text

# Motor asincrono contra Supavisor (Transaction Pooler de Supabase Cloud)
engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=300,
    echo=settings.APP_ENV == "development",
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)
```

Helper critico para SET LOCAL:

```python
from contextlib import asynccontextmanager
from uuid import UUID

@asynccontextmanager
async def tenant_session(client_id: UUID):
    """
    Context manager que abre una sesion con SET LOCAL para RLS.
    SIEMPRE usar SET LOCAL, nunca SET.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # SET LOCAL tiene scope de transaccion - seguro con Supavisor (transaction mode)
            await session.execute(
                text("SET LOCAL app.current_client_id = :client_id"),
                {"client_id": str(client_id)},
            )
            yield session
            # El COMMIT o ROLLBACK al salir del context manager
            # limpia automaticamente el SET LOCAL
```

**CRITICO**: SIEMPRE usar `SET LOCAL`, NUNCA `SET`. `SET LOCAL` tiene scope de transaccion, lo cual es esencial para Supavisor (Transaction Pooler de Supabase Cloud) en transaction mode. `SET` sin LOCAL persiste por sesion y puede causar fuga de datos entre tenants. Supavisor resetea variables de sesion entre transacciones, igual que pgBouncer.

### 3. App Factory (`app/main.py`)

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializacion y limpieza de recursos."""
    # Startup
    logger.info("Iniciando aplicacion...")
    # Inicializar pool de DB
    # Inicializar conexion Redis
    # Log de configuracion (sin secrets)
    yield
    # Shutdown
    logger.info("Cerrando aplicacion...")
    await engine.dispose()
    # Cerrar conexion Redis

def create_app() -> FastAPI:
    app = FastAPI(
        title="Omnichannel Conversational AI",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        lifespan=lifespan,
    )

    # Middleware stack (orden importa: ultimo registrado = primero ejecutado)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(TenantContextMiddleware)

    # Exception handlers
    app.add_exception_handler(AppException, app_exception_handler)

    # Routers
    app.include_router(health_router, prefix="/internal", tags=["internal"])
    app.include_router(auth_router, prefix="/api/v1/auth", tags=["auth"])

    return app
```

### 4. TenantContextMiddleware (`app/middleware/tenant_context.py`)

```python
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Rutas que NO requieren autenticacion
PUBLIC_PATHS = {
    "/internal/health",
    "/api/docs",
    "/api/redoc",
    "/api/openapi.json",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
}

# Rutas de webhook (autenticacion por firma, no JWT)
WEBHOOK_PATHS_PREFIX = "/api/v1/webhooks/"

class TenantContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Rutas publicas: no requieren JWT
        if path in PUBLIC_PATHS or path.startswith(WEBHOOK_PATHS_PREFIX):
            return await call_next(request)

        # Extraer y validar JWT
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"error_code": "MISSING_TOKEN", "message": "Token de autenticacion requerido"},
            )

        token = auth_header.split(" ")[1]

        try:
            payload = decode_jwt(token)
        except JWTExpiredError:
            return JSONResponse(status_code=401, content={"error_code": "TOKEN_EXPIRED", ...})
        except JWTInvalidError:
            return JSONResponse(status_code=401, content={"error_code": "INVALID_TOKEN", ...})

        # Inyectar en request.state
        request.state.client_id = UUID(payload["client_id"])
        request.state.user_id = UUID(payload["user_id"])
        request.state.user_role = payload["role"]

        return await call_next(request)
```

### 5. Security (`app/core/security.py`)

```python
from jose import jwt, JWTError, ExpiredSignatureError
from passlib.context import CryptContext
from datetime import datetime, timedelta, timezone

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=settings.JWT_EXPIRATION_MINUTES))
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)

def create_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=settings.JWT_REFRESH_EXPIRATION_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)

def decode_jwt(token: str) -> dict:
    """
    Decodifica y valida un JWT.
    Raises JWTExpiredError o JWTInvalidError.
    """
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except ExpiredSignatureError:
        raise JWTExpiredError("Token expirado")
    except JWTError:
        raise JWTInvalidError("Token invalido")
```

Estructura del payload JWT:
```json
{
    "user_id": "uuid",
    "client_id": "uuid",
    "email": "user@example.com",
    "role": "admin",
    "exp": 1234567890,
    "type": "access"
}
```

### 6. Dependencies (`app/core/dependencies.py`)

```python
from fastapi import Request, Depends, HTTPException
from uuid import UUID

async def get_current_user(request: Request) -> dict:
    """Extrae el usuario actual del request.state (inyectado por middleware)."""
    if not hasattr(request.state, "user_id"):
        raise HTTPException(status_code=401, detail="No autenticado")
    return {
        "user_id": request.state.user_id,
        "client_id": request.state.client_id,
        "role": request.state.user_role,
    }

def require_role(*allowed_roles: str):
    """
    Dependency factory que verifica que el usuario tenga uno de los roles permitidos.

    Jerarquia de roles:
    - super_admin: acceso total a todos los tenants
    - admin: gestion completa de su tenant
    - supervisor: visualizacion de metricas y aprobacion de respuestas
    - agent: operacion basica (ver contactos, responder mensajes)
    """
    async def role_checker(current_user: dict = Depends(get_current_user)):
        if current_user["role"] not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Rol '{current_user['role']}' no tiene acceso. Roles requeridos: {allowed_roles}",
            )
        return current_user
    return role_checker

async def get_tenant_session(request: Request):
    """
    Dependency que provee una sesion de DB con SET LOCAL ya aplicado.
    Usar en endpoints que necesitan acceso a datos del tenant.
    """
    client_id = request.state.client_id
    async with tenant_session(client_id) as session:
        yield session
```

Uso en endpoints:
```python
@router.get("/contacts")
async def list_contacts(
    session: AsyncSession = Depends(get_tenant_session),
    user: dict = Depends(require_role("admin", "supervisor", "agent")),
):
    result = await session.execute(select(Contact))
    # RLS filtra automaticamente por client_id gracias a SET LOCAL
    return result.scalars().all()
```

### 7. Exceptions (`app/core/exceptions.py`)

```python
class AppException(Exception):
    def __init__(self, status_code: int, error_code: str, message: str):
        self.status_code = status_code
        self.error_code = error_code
        self.message = message

async def app_exception_handler(request: Request, exc: AppException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error_code": exc.error_code,
            "message": exc.message,
        },
    )
```

Codigos de error estandar:
- `MISSING_TOKEN` (401): No se proporciono token
- `TOKEN_EXPIRED` (401): Token expirado
- `INVALID_TOKEN` (401): Token invalido o corrupto
- `FORBIDDEN` (403): Rol insuficiente
- `NOT_FOUND` (404): Recurso no encontrado
- `DUPLICATE` (409): Recurso duplicado (e.g., email ya existe)
- `VALIDATION_ERROR` (422): Error de validacion de datos
- `INTERNAL_ERROR` (500): Error interno (NUNCA exponer traceback)

### 8. SQLAlchemy Models (`app/models/`)

#### Base Model (`app/models/base.py`)

```python
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import UUID, DateTime, func
from uuid import UUID as PyUUID
from datetime import datetime

class Base(DeclarativeBase):
    pass

class TenantBaseModel(Base):
    """Modelo base para todas las tablas con client_id (multi-tenant)."""
    __abstract__ = True

    id: Mapped[PyUUID] = mapped_column(UUID, primary_key=True, server_default=func.gen_random_uuid())
    client_id: Mapped[PyUUID] = mapped_column(UUID, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

#### Ejemplo: Client Model (`app/models/client.py`)

```python
from sqlalchemy import String, Boolean, Enum, DateTime
from sqlalchemy.dialects.postgresql import JSONB

class Client(Base):
    """Tabla raiz de tenants. NO hereda de TenantBaseModel (no tiene client_id)."""
    __tablename__ = "clients"

    id: Mapped[PyUUID] = mapped_column(UUID, primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    plan: Mapped[str] = mapped_column(Enum("free", "starter", "professional", "enterprise", name="plan_type"), default="free")
    settings: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    users: Mapped[list["User"]] = relationship("User", back_populates="client")
    contacts: Mapped[list["Contact"]] = relationship("Contact", back_populates="client")
```

Crear modelos para TODAS las 18 tablas siguiendo este patron:
- `Client` (NO hereda TenantBaseModel, usa `id` como scope)
- `User` (hereda TenantBaseModel, relationship con Client)
- `Contact` (hereda TenantBaseModel, self-ref con merged_into_id)
- `ContactIdentifier` (hereda TenantBaseModel)
- `Tag` (hereda TenantBaseModel)
- `ContactTag` (hereda TenantBaseModel)
- `InternalNote` (hereda TenantBaseModel)
- `Conversation` (hereda TenantBaseModel, 7 estados como Enum)
- `Message` (hereda TenantBaseModel)
- `Document` (hereda TenantBaseModel)
- `DocumentChunk` (hereda TenantBaseModel, columna vector con pgvector)
- `TokenBudget` (hereda TenantBaseModel)
- `TokenUsageLog` (hereda TenantBaseModel)
- `WebhookDedup` (hereda TenantBaseModel)
- `AgentConfig` (hereda TenantBaseModel)
- `QuickReply` (hereda TenantBaseModel)
- `PendingResponse` (hereda TenantBaseModel)
- `ApprovedResponse` (hereda TenantBaseModel, columna vector)

Para columnas `vector(1536)`, usar:
```python
from pgvector.sqlalchemy import Vector

class DocumentChunk(TenantBaseModel):
    __tablename__ = "document_chunks"
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
```

### 9. Pydantic Schemas (`app/schemas/`)

Para cada modelo, crear al menos 4 schemas:

```python
# app/schemas/contact.py
from pydantic import BaseModel, ConfigDict
from uuid import UUID
from datetime import datetime

class ContactCreate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None
    metadata: dict = {}

class ContactUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None
    metadata: dict | None = None

class ContactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    client_id: UUID
    first_name: str | None
    last_name: str | None
    display_name: str | None
    merged_into_id: UUID | None
    metadata: dict
    created_at: datetime
    updated_at: datetime

class ContactListResponse(BaseModel):
    items: list[ContactResponse]
    total: int
    page: int
    page_size: int
```

Schemas comunes (`app/schemas/common.py`):
```python
class PaginationParams(BaseModel):
    page: int = 1
    page_size: int = 20

class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    total_pages: int

class ErrorResponse(BaseModel):
    error_code: str
    message: str
```

### 10. Auth Endpoints (`app/api/v1/auth.py`)

```python
@router.post("/login", response_model=TokenResponse)
async def login(credentials: LoginRequest):
    """
    Autenticar usuario con email y password.
    Retorna access_token y refresh_token.
    """
    # 1. Buscar usuario por email (sin RLS, consulta directa)
    # 2. Verificar password con bcrypt
    # 3. Verificar que el usuario esta activo
    # 4. Verificar que el tenant esta activo
    # 5. Generar tokens JWT con client_id, user_id, role
    # 6. Actualizar last_login_at
    # 7. Retornar tokens

@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(refresh: RefreshRequest):
    """
    Renovar access_token usando refresh_token.
    """
    # 1. Decodificar refresh_token
    # 2. Verificar type == "refresh"
    # 3. Generar nuevo access_token
    # 4. Retornar nuevo token (refresh_token se mantiene)
```

### 11. Health Check (`app/api/internal/health.py`)

```python
@router.get("/health")
async def health_check():
    """
    Health check para Docker y load balancers.
    Verifica conectividad con DB y Redis.
    """
    checks = {}
    try:
        # Check DB via Supavisor (Supabase Cloud)
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "error"

    try:
        # Check Redis
        redis = get_redis()
        await redis.ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "error"

    status = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    status_code = 200 if status == "ok" else 503

    return JSONResponse(
        status_code=status_code,
        content={"status": status, "checks": checks},
    )
```

### 12. TokenBudgetGuard Placeholder (`app/middleware/token_budget.py`)

Crear como placeholder que sera implementado completamente en Sprint 6:

```python
class TokenBudgetGuard:
    """
    Verifica el presupuesto de tokens del tenant antes de procesar con IA.

    Niveles:
    - < 90%: ok (usa modelo configurado del tenant)
    - 90-99%: degraded (cambia a gpt-4o-mini)
    - >= 100%: exceeded (rechaza y hace handoff a humano)

    Implementacion completa en Sprint 6 (LangGraph).
    """

    async def check_budget(self, client_id: UUID) -> dict:
        # Placeholder: siempre retorna ok
        return {"status": "ok", "usage_pct": 0, "model_to_use": "gpt-4o"}
```

### 13. Tests

#### conftest.py

```python
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from uuid import uuid4

@pytest_asyncio.fixture
async def app():
    """Crear instancia de la app para testing."""
    from app.main import create_app
    return create_app()

@pytest_asyncio.fixture
async def client(app):
    """Async HTTP client para testing."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

@pytest_asyncio.fixture
async def tenant_a():
    """Fixture: Tenant A con usuario admin."""
    client_id = uuid4()
    # Insertar tenant y usuario en DB de test
    # Generar JWT para el tenant
    return {"client_id": client_id, "token": "..."}

@pytest_asyncio.fixture
async def tenant_b():
    """Fixture: Tenant B con usuario admin."""
    # Similar a tenant_a pero con otro client_id
    ...
```

#### test_rls_isolation.py

```python
@pytest.mark.asyncio
async def test_tenant_isolation(client, tenant_a, tenant_b):
    """Verificar que un tenant no puede ver datos de otro."""
    # 1. Crear contacto para tenant A
    resp = await client.post(
        "/api/v1/contacts",
        json={"first_name": "Juan", "last_name": "Perez"},
        headers={"Authorization": f"Bearer {tenant_a['token']}"},
    )
    assert resp.status_code == 201

    # 2. Listar contactos como tenant B
    resp = await client.get(
        "/api/v1/contacts",
        headers={"Authorization": f"Bearer {tenant_b['token']}"},
    )
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 0  # No ve datos de tenant A
```

#### test_auth.py

```python
@pytest.mark.asyncio
async def test_login_success(client):
    """Login con credenciales validas retorna tokens."""
    ...

@pytest.mark.asyncio
async def test_login_invalid_password(client):
    """Login con password incorrecta retorna 401."""
    ...

@pytest.mark.asyncio
async def test_expired_token_rejected(client):
    """Token expirado retorna 401."""
    ...

@pytest.mark.asyncio
async def test_role_insufficient(client, agent_user):
    """Rol agent no puede acceder a endpoints de admin (403)."""
    ...

@pytest.mark.asyncio
async def test_no_token_rejected(client):
    """Request sin token retorna 401."""
    ...

@pytest.mark.asyncio
async def test_health_no_auth(client):
    """Health endpoint no requiere autenticacion."""
    resp = await client.get("/internal/health")
    assert resp.status_code in (200, 503)
```

## Criterios de Aceptacion
- [ ] Middleware aplica `SET LOCAL` correctamente (verificar con log SQL o query `SHOW app.current_client_id`)
- [ ] Endpoints protegidos rechazan requests sin JWT con HTTP 401
- [ ] RBAC bloquea roles insuficientes con HTTP 403
- [ ] Health endpoint responde 200 sin autenticacion
- [ ] Login retorna access_token y refresh_token validos
- [ ] Refresh token genera nuevo access_token
- [ ] Tests de aislamiento RLS pasan via API
- [ ] Todos los modelos SQLAlchemy mapean correctamente a las 18 tablas
- [ ] Pydantic schemas validan datos de entrada (422 para datos invalidos)
- [ ] AppException nunca expone tracebacks al cliente
- [ ] Type hints en TODAS las funciones publicas
- [ ] Docstrings Google-style en todas las funciones publicas

## Notas Tecnicas

### SET LOCAL y Supavisor (ADR-020)
`SET LOCAL` afecta solo la transaccion actual. Supavisor (Transaction Pooler de Supabase Cloud) opera en transaction mode, igual que pgBouncer: cada transaccion puede usar una conexion diferente del pool. Esto hace que `SET LOCAL` sea la unica opcion segura para establecer el contexto del tenant. `DATABASE_URL` apunta al pooler (puerto 6543); `DATABASE_URL_DIRECT` (puerto 5432) se usa SOLO para migraciones Alembic.

### SQLAlchemy 2.0 con Async
- Usar `Mapped` y `mapped_column` (estilo 2.0), no `Column` (estilo 1.x)
- Todas las operaciones de I/O deben ser async (`await`)
- `expire_on_commit=False` evita lazy loading accidental despues del commit
- `pool_pre_ping=True` detecta conexiones muertas antes de usarlas

### Patron App Factory
El patron factory (`create_app()`) permite:
- Crear instancias aisladas para testing
- Configurar middleware y routers en un solo lugar
- Usar lifespan para init/cleanup de recursos

### RBAC — Jerarquia de Roles
```
super_admin > admin > supervisor > agent
```
- `super_admin`: acceso a todos los tenants, gestion de la plataforma
- `admin`: gestion completa de su tenant (usuarios, configs, documentos)
- `supervisor`: metricas, aprobacion de respuestas, supervision de agentes
- `agent`: operacion basica (contactos, conversaciones, mensajes)

Cada endpoint debe declarar los roles minimos con `require_role()`.

## Dependencias para Sprint 4
- `TenantContextMiddleware` funcional y validado
- `AppException` registrada como handler global
- Router `/api/v1/` montado y accesible via Traefik
- `AsyncSessionLocal` y `tenant_session()` disponibles para inyeccion
- `get_current_user` y `require_role` disponibles como dependencies
- Modelos SQLAlchemy importables desde `app.models`
