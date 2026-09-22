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
        # `extra="ignore"` es obligatorio, no cosmetico: el `.env` del proyecto es
        # compartido con docker-compose y declara 44 claves que esta clase no modela
        # (POSTGRES_*, REDIS_HOST, S3_*, GF_*, CELERY_*_CONCURRENCY, TRAEFIK_*, ...).
        # Con el `extra="forbid"` que trae BaseSettings por defecto, `Settings()`
        # lanzaba ValidationError y la app no arrancaba con su propio `.env.example`.
        extra="ignore",
    )

    # Database — Supavisor (Transaction Pooler de Supabase Cloud, puerto 6543)
    DATABASE_URL: str
    # Conexión directa — SOLO para migraciones Alembic
    DATABASE_URL_DIRECT: str = ""

    # Supabase Cloud
    SUPABASE_URL: str = ""
    SUPABASE_PUBLISHABLE_KEY: str = ""
    SUPABASE_SECRET_KEY: str = ""
    # Bucket privado del knowledge base. El aislamiento entre tenants es por
    # prefijo de ruta ({client_id}/...), ver app/services/storage.py.
    SUPABASE_STORAGE_BUCKET: str = "documents"

    # Redis
    REDIS_URL: str = "redis://redis:6379/0"

    # JWT
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_MINUTES: int = 30
    JWT_REFRESH_EXPIRATION_DAYS: int = 7

    # API Keys
    OPENAI_API_KEY: str = ""

    # YCloud (WhatsApp Business API) — Sprint 4
    YCLOUD_API_KEY: str = ""
    YCLOUD_WEBHOOK_SECRET: str = ""
    YCLOUD_BASE_URL: str = "https://api.ycloud.com/v2"
    # Numero de WhatsApp desde el que responde el bot (`from` de la API de
    # YCloud). Lo necesita el nodo `respond` del grafo (Sprint 6); hasta que
    # exista la tabla `channel_configs` (Fase 2) es global, no por tenant.
    YCLOUD_PHONE_NUMBER_ID: str = ""

    # Meta Graph API (Instagram DM + Facebook Messenger) — Sprint 4
    META_APP_SECRET: str = ""
    META_PAGE_ACCESS_TOKEN: str = ""
    META_WEBHOOK_VERIFY_TOKEN: str = ""
    META_GRAPH_API_VERSION: str = "v19.0"

    # Telegram como canal de clientes (Bot API) — Sprint 9. Es un bot distinto
    # del de super admin (Sprint 8). Como YCloud/Meta, las credenciales son
    # globales hasta que exista la tabla `channel_configs` (Fase 2).
    TELEGRAM_CHANNEL_BOT_TOKEN: str = ""
    # `secret_token` que se registra en `setWebhook`; Telegram lo devuelve en el
    # header `X-Telegram-Bot-Api-Secret-Token` de cada update.
    TELEGRAM_WEBHOOK_SECRET: str = ""
    TELEGRAM_API_BASE_URL: str = "https://api.telegram.org"

    # Webchat (WebSocket) — Sprint 9. `WEBCHAT_CHANNEL_TOKEN` identifica el canal
    # en la URL del widget (`/api/v1/webchat/{token}`); NO es un secreto (va en el
    # JS de la pagina del cliente) y vacio deja el webchat desactivado. Lo que
    # protege el canal es el Origin, el limite de mensajes y la sesion firmada.
    WEBCHAT_CHANNEL_TOKEN: str = ""
    # Origenes desde los que se acepta el widget; vacio = los de `CORS_ORIGINS`.
    WEBCHAT_ALLOWED_ORIGINS: list[str] = []
    WEBCHAT_SESSION_TTL_DAYS: int = 30
    WEBCHAT_MAX_MESSAGE_CHARS: int = 4000
    WEBCHAT_MAX_FRAME_BYTES: int = 16 * 1024
    WEBCHAT_MAX_MESSAGES_PER_MINUTE: int = 20
    WEBCHAT_MAX_CONNECTIONS_PER_VISITOR: int = 5
    WEBCHAT_HELLO_TIMEOUT_SECONDS: float = 10.0
    # Sin ningun frame (el widget manda `ping` cada ~25 s) se cierra la conexion.
    WEBCHAT_IDLE_TIMEOUT_SECONDS: float = 120.0
    WEBCHAT_REPLAY_LIMIT: int = 50

    # Email como canal de clientes — Sprint 9. Salida por SMTP; entrada por el
    # Inbound Parse de SendGrid o Mailgun (POST /api/v1/webhooks/email/email).
    # Globales, como el resto de canales, hasta que exista `channel_configs`.
    EMAIL_SMTP_HOST: str = ""
    # 587 = STARTTLS, 465 = TLS implicito (el modo se deduce del puerto).
    EMAIL_SMTP_PORT: int = 587
    EMAIL_SMTP_USER: str = ""
    EMAIL_SMTP_PASSWORD: str = ""
    # Direccion desde la que responde el bot; tambien sirve para no procesar los
    # mensajes que salgan de ella (un bucle contra si mismo).
    EMAIL_FROM_ADDRESS: str = ""
    EMAIL_FROM_NAME: str = "Soporte"
    # "usuario:password" que se pone en la URL del Inbound Parse
    # (https://usuario:password@host/...): ni SendGrid ni Mailgun firman ese
    # webhook, pero los dos envian la cabecera Authorization: Basic.
    EMAIL_INBOUND_WEBHOOK_SECRET: str = ""
    # Adjuntos del Inbound Parse: solo se registra su METADATA (nombre, tipo,
    # tamano), nunca el contenido — guardarlo requiere Storage y una superficie
    # de seguridad propia que queda fuera de esta entrega (ADR-062). Sin esto un
    # adjunto desaparecia sin dejar rastro.
    EMAIL_MAX_ATTACHMENTS: int = 5
    EMAIL_MAX_ATTACHMENT_BYTES: int = 5 * 1024 * 1024

    # Google Calendar (Sprint 7) — un service account global (no OAuth2 por
    # tenant): cada tenant comparte su calendario con el email del service
    # account y guarda su calendar_id en agent_configs.config.scheduling.
    GOOGLE_CALENDAR_CREDENTIALS_JSON: str = ""
    GOOGLE_CALENDAR_ID: str = "primary"

    # Tenant por defecto para webhooks entrantes (MVP).
    # Los webhooks no llevan JWT, asi que el client_id no se puede deducir del request.
    # Hasta que exista la tabla `channel_configs` (Fase 2), el tenant se resuelve por
    # esta variable de entorno. Vacio = no se puede resolver -> el mensaje va a la DLQ.
    DEFAULT_CLIENT_ID: str = ""

    # Cifrado
    ENCRYPTION_KEY: str

    # App
    APP_ENV: str = "development"
    APP_VERSION: str = "1.0.0"
    LOG_LEVEL: str = "INFO"
    # "json" en produccion (agregadores de logs), "console" en desarrollo.
    LOG_FORMAT: str = "console"
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # Observabilidad (Sprint 8)
    # Vacio = tracing deshabilitado. Es el valor por defecto a proposito: sin
    # collector escuchando, el exporter OTLP reintenta en background y ensucia
    # el log de cada test y de cualquier arranque local sin docker-compose.
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""
    OTEL_SERVICE_NAME: str = "omnichannel-api"
    # Fraccion de traces muestreados (1.0 = todos). El muestreo es
    # `parentbased_traceidratio`: si el request ya llega con un `traceparent`
    # muestreado, se respeta esa decision y no se corta la traza a la mitad.
    OTEL_TRACES_SAMPLER_RATIO: float = 1.0
    # Puerto del servidor de metricas que cada worker de Celery levanta para
    # que Prometheus lo scrapee (el worker no expone la API HTTP).
    METRICS_WORKER_PORT: int = 9100

    # Embedding / Chat models
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    OPENAI_CHAT_MODEL: str = "gpt-4o"
    OPENAI_FALLBACK_MODEL: str = "gpt-4o-mini"

    # Transcripcion de audios entrantes (Whisper) — Sprint 9
    WHISPER_MODEL: str = "whisper-1"
    # Vacio = Whisper detecta el idioma (la plataforma atiende 6 idiomas).
    WHISPER_LANGUAGE: str = ""
    # OpenAI acepta hasta 25 MB; se deja margen y se corta antes de gastar la llamada.
    WHISPER_MAX_AUDIO_BYTES: int = 20 * 1024 * 1024
    WHISPER_COST_PER_MINUTE_USD: float = 0.006
    WHISPER_TIMEOUT_SECONDS: float = 60.0
    MEDIA_DOWNLOAD_TIMEOUT_SECONDS: float = 30.0

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
