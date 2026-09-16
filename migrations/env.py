"""Alembic environment configuration — Plataforma SaaS Omnicanal Multi-Tenant.

Usa DATABASE_URL_DIRECT (conexión directa, no pooler) para migraciones.
"""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

# Agregar el directorio raíz del proyecto al path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.base import Base  # noqa: E402

# Importar todos los modelos para que Alembic los detecte
from app.models import (  # noqa: E402, F401
    AgentConfig,
    ApprovedResponse,
    Client,
    Contact,
    ContactIdentifier,
    ContactTag,
    Conversation,
    Document,
    DocumentChunk,
    InternalNote,
    Message,
    PendingResponse,
    QuickReply,
    Tag,
    TokenBudget,
    TokenUsageLog,
    User,
    WebhookDedup,
)

# Alembic Config object
config = context.config

# Setup logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# MetaData para autogenerate
target_metadata = Base.metadata

# Tablas de app/models/ (Base.metadata) creadas fuera del ORM, via
# migrations/versions/003_langgraph_checkpoints.py: son de la libreria
# langgraph-checkpoint-postgres, no de nuestros modelos SQLAlchemy. Sin este
# filtro, `alembic check`/autogenerate las ve en la base pero no en la
# metadata y las marca como pendientes de borrar en cada corrida.
CHECKPOINT_TABLES = frozenset(
    {"checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes"}
)


def include_name(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    """Excluye del diff de autogenerate las tablas del checkpointer de LangGraph.

    Args:
        name: Nombre del objeto que Alembic esta considerando.
        type_: Tipo de objeto (`"table"`, `"column"`, etc.).
        parent_names: Nombres de los objetos contenedores (schema, tabla, ...).

    Returns:
        False para las tablas del checkpointer; True para todo lo demas.
    """
    if type_ == "table" and name in CHECKPOINT_TABLES:
        return False
    return True


def get_url() -> str:
    """Obtener URL de base de datos para migraciones.

    Usa DATABASE_URL_DIRECT (conexión directa) si está disponible,
    sino DATABASE_URL. Las migraciones DEBEN correr contra la conexión
    directa, no contra el pooler (Supavisor).
    """
    url = os.getenv("DATABASE_URL_DIRECT") or os.getenv("DATABASE_URL", "")
    # Alembic necesita driver sync, no async
    return url.replace("postgresql+asyncpg://", "postgresql://")


def run_migrations_offline() -> None:
    """Ejecutar migraciones en modo 'offline'.

    Genera SQL sin conectarse a la base de datos.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=include_name,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Ejecutar migraciones en modo 'online'.

    Crea un Engine y asocia la conexión con el contexto.
    """
    connectable = create_engine(
        get_url(),
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_name=include_name,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
