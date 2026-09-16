"""Grafo de conversacion: construccion, routing y checkpointing.

Contrato: `specs/sprint-06-langgraph.md` §2-3, 12. Ensambla los 6 nodos que
entrego Dev B (Sprint 6) en el flujo:

    token_budget_check -> intent_routing -> [rag_query | respond | human_handoff]
                                              rag_query -> [training_mode_approval | respond | human_handoff]

`app/tasks/ai_processor.py` (Dev B, ya en `main`) es el unico consumidor: llama
`await get_graph_with_checkpointer()` una vez por mensaje y despues
`compilado.ainvoke(estado, config=config)`. Ningun otro modulo debe importar
`get_graph_with_checkpointer` para cachear su resultado (ver ADR-035): el grafo
compilado no sobrevive a la tarea que lo pidio.

Checkpointing
-------------
`AsyncPostgresSaver` necesita una conexion (o pool) propia de `psycopg`, ajena
al engine de SQLAlchemy que administra `run_isolated()` (`app/core/database.py`).
Por eso `get_graph_with_checkpointer()` devuelve un envoltorio (`_CheckpointedGraph`)
en vez del grafo compilado de LangGraph directamente: su `ainvoke()` abre el pool
de `psycopg`, compila el grafo con ese checkpointer y cierra el pool al terminar
-- todo dentro de la misma llamada, así ninguna conexion sobrevive al event loop
que la abrio (mismo motivo que ADR-035 y BUG-006/BUG-011).

Se usa `DATABASE_URL_DIRECT` si esta configurada (Supabase Cloud: psycopg tiene
prepared statements por conexion, algo que el Transaction Pooler de Supavisor no
soporta bien -- mismo motivo por el que Alembic tampoco pasa por el pooler) y
cae a `DATABASE_URL` si no -- que es exactamente lo que hay en CI, donde no
existe un pooler real detras.

**Nunca se llama `checkpointer.setup()` aca.** Esa llamada hace `CREATE TABLE`,
y el rol con el que corre la app (`app_user` en CI; el rol de aplicacion en
produccion) solo tiene privilegios DML -- igual que con cualquier otra tabla,
el runtime nunca hace DDL. Las cuatro tablas del checkpointer
(`checkpoint_migrations`, `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`)
las crea `migrations/versions/003_langgraph_checkpoints.py`, una sola vez.
"""

import logging
from typing import TYPE_CHECKING, Any, cast

from langgraph.graph import END, StateGraph

from app.agents.nodes.human_handoff import human_handoff_node
from app.agents.nodes.intent_router import intent_routing_node
from app.agents.nodes.rag_query import rag_query_node
from app.agents.nodes.respond import respond_node
from app.agents.nodes.token_budget import token_budget_check_node
from app.agents.nodes.training_approval import training_approval_node
from app.agents.state import ConversationState
from app.core.config import get_settings

if TYPE_CHECKING:
    from psycopg import AsyncConnection

logger = logging.getLogger(__name__)

NODE_TOKEN_BUDGET = "token_budget_check"  # noqa: S105 -- nombre de nodo, no una credencial
NODE_INTENT_ROUTING = "intent_routing"
NODE_RAG_QUERY = "rag_query"
NODE_RESPOND = "respond"
NODE_HUMAN_HANDOFF = "human_handoff"
NODE_TRAINING_APPROVAL = "training_mode_approval"

# Intents que `respond_node` contesta sin pasar por RAG (ver app/agents/nodes/respond.py).
_DIRECT_RESPONSE_INTENTS = frozenset({"greeting", "farewell"})

# Intents que siempre escalan a un humano sin intentar RAG.
_HUMAN_INTENTS = frozenset({"human_request", "complaint"})

# Maximo de conexiones que el pool del checkpointer abre para un solo mensaje.
# El grafo encadena a lo sumo dos escrituras de checkpoint (entrada y salida);
# no hace falta un pool grande para una sola tarea de Celery.
CHECKPOINTER_POOL_MAX_SIZE = 3


def route_after_budget_check(state: ConversationState) -> str:
    """Decide si seguir con el routing de intents o escalar por presupuesto agotado.

    Args:
        state: Estado tras `token_budget_check_node`.

    Returns:
        `"exceeded"` si el presupuesto esta agotado; `"continue"` en cualquier
        otro caso.
    """
    if state.get("budget_status") == "exceeded":
        return "exceeded"
    return "continue"


def route_after_intent(state: ConversationState) -> str:
    """Enruta segun el intent que detecto `intent_routing_node`.

    `intent_routing_node` ya reencamina los intents que el tenant no tiene
    habilitados (`app/agents/nodes/intent_router.py::_resolve`): para cuando el
    estado llega aca, `intent` es uno que el tenant puede atender de verdad. Acá
    solo queda mapear la categoria final a la siguiente rama.

    Args:
        state: Estado tras `intent_routing_node`.

    Returns:
        `"respond"`, `"human_handoff"` o `"rag_query"`.
    """
    intent = state.get("intent") or "unknown"

    if intent in _DIRECT_RESPONSE_INTENTS:
        return "respond"
    if intent in _HUMAN_INTENTS:
        return "human_handoff"
    # rag_query y unknown (se intenta RAG primero); scheduling tambien cae aca
    # por si el clasificador lo devuelve antes de que exista el agente de
    # Sprint 7 -- mismo criterio que el spec ("por ahora, tratar como rag_query").
    return "rag_query"


def route_after_rag(state: ConversationState) -> str:
    """Decide si responder, escalar o retener para aprobacion tras el RAG.

    Args:
        state: Estado tras `rag_query_node`.

    Returns:
        `"human_handoff"`, `"training_mode"` o `"respond"`.
    """
    if state.get("requires_handoff"):
        return "human_handoff"
    if state.get("training_mode"):
        return "training_mode"
    return "respond"


def build_conversation_graph() -> "StateGraph[ConversationState]":
    """Arma el grafo de conversacion, sin compilar.

    Returns:
        `StateGraph` con los 6 nodos y el routing condicional del sprint.
    """
    graph = StateGraph(ConversationState)

    graph.add_node(NODE_TOKEN_BUDGET, token_budget_check_node)
    graph.add_node(NODE_INTENT_ROUTING, intent_routing_node)
    graph.add_node(NODE_RAG_QUERY, rag_query_node)
    graph.add_node(NODE_RESPOND, respond_node)
    graph.add_node(NODE_HUMAN_HANDOFF, human_handoff_node)
    graph.add_node(NODE_TRAINING_APPROVAL, training_approval_node)

    graph.set_entry_point(NODE_TOKEN_BUDGET)

    graph.add_conditional_edges(
        NODE_TOKEN_BUDGET,
        route_after_budget_check,
        {"continue": NODE_INTENT_ROUTING, "exceeded": NODE_HUMAN_HANDOFF},
    )
    graph.add_conditional_edges(
        NODE_INTENT_ROUTING,
        route_after_intent,
        {
            "rag_query": NODE_RAG_QUERY,
            "human_handoff": NODE_HUMAN_HANDOFF,
            "respond": NODE_RESPOND,
        },
    )
    graph.add_conditional_edges(
        NODE_RAG_QUERY,
        route_after_rag,
        {
            "training_mode": NODE_TRAINING_APPROVAL,
            "respond": NODE_RESPOND,
            "human_handoff": NODE_HUMAN_HANDOFF,
        },
    )

    graph.add_edge(NODE_RESPOND, END)
    graph.add_edge(NODE_HUMAN_HANDOFF, END)
    graph.add_edge(NODE_TRAINING_APPROVAL, END)

    return graph


def _checkpointer_conninfo() -> str:
    """Resuelve el connection string de `psycopg` para el checkpointer.

    Prefiere `DATABASE_URL_DIRECT` (conexion directa a Supabase Cloud, puerto
    5432): `setup()` corre DDL y `psycopg` usa prepared statements por conexion,
    algo que el Transaction Pooler de Supavisor (`DATABASE_URL`, puerto 6543) no
    soporta bien -- mismo motivo por el que Alembic tampoco pasa por el pooler.
    Cae a `DATABASE_URL` si no hay conexion directa configurada, que es lo que
    hay en CI (un solo Postgres de test, sin pooler real detras).

    Returns:
        Connection string en formato `postgresql://...` (sin el sufijo
        `+asyncpg` que usa SQLAlchemy; `psycopg` no lo entiende).
    """
    settings = get_settings()
    url = settings.DATABASE_URL_DIRECT or settings.DATABASE_URL
    return url.replace("postgresql+asyncpg://", "postgresql://")


class _CheckpointedGraph:
    """Compila el grafo y abre/cierra el pool del checkpointer en cada invocacion.

    No compila ni abre nada en `__init__`: todo el trabajo pasa dentro de
    `ainvoke()`, para que el pool de `psycopg` viva exclusivamente en el event
    loop de la llamada que lo uso (ver ADR-035 en MEMORY.md).
    """

    async def ainvoke(self, state: ConversationState, config: dict[str, Any]) -> dict[str, Any]:
        """Ejecuta el grafo completo para un mensaje, con checkpointing.

        Args:
            state: Estado inicial de la conversacion.
            config: Config de LangGraph; debe traer `configurable.thread_id`.

        Returns:
            Estado final del grafo.
        """
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        conn_kwargs: dict[str, Any] = {
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        }

        # AsyncPostgresSaver exige conexiones con row_factory=dict_row; el tipo
        # generico de AsyncConnectionPool no lo infiere de `kwargs` (es un dict
        # en runtime), asi que se declara a mano.
        pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]] = AsyncConnectionPool(
            conninfo=_checkpointer_conninfo(),
            max_size=CHECKPOINTER_POOL_MAX_SIZE,
            kwargs=conn_kwargs,
            open=False,
        )
        async with pool:
            # Sin .setup(): hace CREATE TABLE y el rol de la app no tiene DDL.
            # Las tablas del checkpointer las crea
            # migrations/versions/003_langgraph_checkpoints.py.
            checkpointer = AsyncPostgresSaver(pool)
            # cast a Any: el overload de Pregel.ainvoke() es dificil de matchear
            # estaticamente con un TypedDict + dict de config, y ademas cambia
            # segun la version de langgraph que resuelva pip (requirements.txt
            # no la fija) -- un type: ignore aca seria "unused" en unas
            # versiones y necesario en otras. El comportamiento en runtime es
            # el documentado (version="v1" implicita, devuelve el estado final).
            compiled = cast("Any", build_conversation_graph().compile(checkpointer=checkpointer))
            resultado: dict[str, Any] = await compiled.ainvoke(state, config=config)
            return resultado


async def get_graph_with_checkpointer() -> _CheckpointedGraph:
    """Devuelve un grafo listo para `ainvoke()`, con checkpointing en Postgres.

    `app/tasks/ai_processor.py` la llama una vez por mensaje y nunca cachea el
    resultado (ADR-035): el objeto devuelto no abre ninguna conexion todavia,
    asi que no hay nada que limpiar si el llamante lo descarta sin invocarlo.

    Returns:
        Envoltorio con un `ainvoke(state, config)` compatible con el de un
        grafo compilado de LangGraph.
    """
    return _CheckpointedGraph()
