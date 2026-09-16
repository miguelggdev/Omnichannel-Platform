"""langgraph_checkpoints — tablas del checkpointer de LangGraph (Sprint 6).

`AsyncPostgresSaver.setup()` (langgraph-checkpoint-postgres) crea estas tablas
en tiempo de ejecucion con `CREATE TABLE IF NOT EXISTS`, pero eso exige
privilegio `CREATE` en el schema — el rol con el que corre la app (`app_user`
en CI; el pooler en produccion) solo tiene DML (Regla de seguridad: el runtime
nunca hace DDL). Por eso `app/agents/graph.py` NO llama a `.setup()`: estas
cuatro tablas se crean una sola vez, aca, igual que el resto del schema.

Contenido: la union de las migraciones internas de `AsyncPostgresSaver`
(`langgraph.checkpoint.postgres.base.MIGRATIONS`) al dia de esta migracion —
schema final, no el historial paso a paso de esa libreria. Los indices se
crean sin `CONCURRENTLY`: las tablas nacen vacias aca, no hace falta evitar el
lock que esa clausula existe para esquivar (y `CONCURRENTLY` no puede correr
dentro de una transaccion, que es como Alembic ejecuta cada migracion).

Estas tablas no tienen `client_id` ni RLS: el aislamiento entre tenants es
estructural (`thread_id` se arma como `f"{client_id}:{conversation_id}"`,
unico por tenant) y toda query de LangGraph filtra por ese thread_id completo.

Revision ID: 003_langgraph_checkpoints
Revises: 002_rls_policies
Create Date: 2026-09-15
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "003_langgraph_checkpoints"
down_revision: Union[str, None] = "002_rls_policies"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS checkpoint_migrations (
            v INTEGER PRIMARY KEY
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS checkpoints (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL,
            parent_checkpoint_id TEXT,
            type TEXT,
            checkpoint JSONB NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}',
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS checkpoint_blobs (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL,
            version TEXT NOT NULL,
            type TEXT NOT NULL,
            blob BYTEA,
            PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS checkpoint_writes (
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            task_path TEXT NOT NULL DEFAULT '',
            idx INTEGER NOT NULL,
            channel TEXT NOT NULL,
            type TEXT,
            blob BYTEA NOT NULL,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
        )
    """)

    op.execute("CREATE INDEX IF NOT EXISTS checkpoints_thread_id_idx ON checkpoints(thread_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS checkpoint_blobs_thread_id_idx ON checkpoint_blobs(thread_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS checkpoint_writes_thread_id_idx ON checkpoint_writes(thread_id)"
    )

    # Registra el schema como "al dia" para que un .setup() corrido por un rol
    # con privilegio CREATE (fuera del camino normal de la app) no reintente
    # crear nada: son 10 migraciones internas en la libreria, version 0-9.
    op.execute("""
        INSERT INTO checkpoint_migrations (v)
        SELECT generate_series(0, 9)
        ON CONFLICT (v) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS checkpoint_writes")
    op.execute("DROP TABLE IF EXISTS checkpoint_blobs")
    op.execute("DROP TABLE IF EXISTS checkpoints")
    op.execute("DROP TABLE IF EXISTS checkpoint_migrations")
