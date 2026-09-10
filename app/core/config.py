"""Configuración centralizada — Pydantic BaseSettings v2.

Carga variables de entorno desde .env con validación estricta.
DATABASE_URL apunta al Transaction Pooler de Supabase Cloud (Supavisor, puerto 6543).
DATABASE_URL_DIRECT es SOLO para migraciones Alembic (conexión directa, puerto 5432).
"""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración global de la aplicación.

    Attributes:
        DATABASE_URL: Connection string vía Supavisor (Transaction Pooler, puerto 6543).
        DATABASE_URL_DIRECT: Conexión directa para migraciones Alembic solamente.
        SUPABASE_URL: URL del proyecto Supabase Cloud.
        SUPABASE_PUBLISHABLE_KEY: API key pública de Supabase (sb_publishable_*).
        SUPABASE_SECRET_KEY: API key secreta de Supabase (sb_secret_*).
        REDIS_URL: Connection string de Redis.
        JWT_SECRET: Secreto para firmar tokens JWT (mínimo 32 chars).
        ENCRYPTION_KEY: Clave simétrica para pgcrypto (mínimo 32 chars).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Database — Supavisor (Transaction Pooler de Supabase Cloud, puerto 6543)
    DATABASE_URL: str
    # Conexión directa — SOLO para migraciones Alembic
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
    OPENAI_API_KEY: str = ""
    YCLOUD_API_KEY: str = ""
    YCLOUD_WEBHOOK_SECRET: str = ""

    # Cifrado
    ENCRYPTION_KEY: str

    # App
    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # Embedding / Chat models
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    OPENAI_CHAT_MODEL: str = "gpt-4o"
    OPENAI_FALLBACK_MODEL: str = "gpt-4o-mini"

    # Anthropic (Admin Assistant)
    ANTHROPIC_API_KEY: str = ""
    ADMIN_ASSISTANT_MODEL: str = "claude-sonnet-4-20250514"
    ADMIN_ASSISTANT_MAX_TOKENS: int = 4096
    ADMIN_ASSISTANT_RATE_LIMIT: int = 30

    @field_validator("JWT_SECRET")
    @classmethod
    def jwt_secret_min_length(cls, v: str) -> str:
        """Validar que JWT_SECRET tenga al menos 32 caracteres."""
        if len(v) < 32:
            raise ValueError("JWT_SECRET debe tener al menos 32 caracteres")
        return v

    @field_validator("ENCRYPTION_KEY")
    @classmethod
    def encryption_key_min_length(cls, v: str) -> str:
        """Validar que ENCRYPTION_KEY tenga al menos 32 caracteres."""
        if len(v) < 32:
            raise ValueError("ENCRYPTION_KEY debe tener al menos 32 caracteres")
        return v

    @field_validator("DATABASE_URL")
    @classmethod
    def database_url_starts_with_postgres(cls, v: str) -> str:
        """Validar que DATABASE_URL sea un connection string de PostgreSQL."""
        if not v.startswith("postgres"):
            raise ValueError("DATABASE_URL debe empezar con 'postgres'")
        return v


@lru_cache
def get_settings() -> Settings:
    """Obtener instancia singleton de Settings (lazy).

    Usa lru_cache para instanciar solo una vez, evitando
    errores de validación al importar sin env vars (e.g. en tests).
    """
    return Settings()
