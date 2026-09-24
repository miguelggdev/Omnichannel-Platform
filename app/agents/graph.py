"""Grafo de conversacion: construccion, routing y checkpointing.

Contrato: `specs/sprint-06-langgraph.md` §2-3, 12; `specs/sprint-07-scheduling-crm.md`
§5-6 (nodo `scheduling`, Sprint 7); y `specs/sprint-07-addendum-agent-logging.md`
§4-5 (cada nodo se registra envuelto en `logged_node()`, que escribe su
actividad en `agent_action_logs`); y `specs/sprint-12-agents-advanced.md` §1-2
y §8 (nodos `financial` y `marketing`, Sprint 12). Ensambla los 9 nodos en el
flujo:

    token_budget_check -> intent_routing -> [rag_query | respond | human_handoff |
                                               scheduling | financial | marketing]
                                              rag_query    -> [training_mode_approval | respond | human_handoff]
                                              scheduling   -> [respond | human_handoff]
                                              financial    -> respond
                                              marketing    -> respond

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

Usa `DATABASE_URL` (el Transaction Pooler de Supavisor), no `DATABASE_URL_DIRECT`:
es la unica variable que `docker-compose.yml` le pasa a los workers de Celery
(incluido `celery-ai`), y ademas es la eleccion correcta para un pool que se
abre y cierra en cada mensaje -- los slots de conexion directa de Supabase
Cloud son limitados, mientras que el pooler esta pensado justo para este
patron de conexiones cortas y frecuentes. `prepare_threshold=0` (ver
`conn_kwargs` en `ainvoke()`) es lo que evita el problema clasico de prepared
statements contra un pooler en modo transaccion -- no hace falta esquivarlo
con una conexion directa, a diferencia de Alembic (que si hace DDL y sostiene
una sola conexion larga, ahi la conexion directa importa).

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

from app.agents.middleware.logging_middleware import logged_node
from app.agents.nodes.financial import financial_node
from app.agents.nodes.human_handoff import human_handoff_node
from app.agents.nodes.intent_router import intent_routing_node
from app.agents.nodes.marketing import marketing_node
from app.agents.nodes.rag_query import rag_query_node
from app.agents.nodes.respond import respond_node
from app.agents.nodes.scheduling import scheduling_node
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
NODE_SCHEDULING = "scheduling"
NODE_FINANCIAL = "financial"
NODE_MARKETING = "marketing"

# Intents que `respond_node` contesta sin pasar por RAG (ver app/agents/nodes/respond.py).
_DIRECT_RESPONSE_INTENTS = frozenset({"greeting", "farewell"})

# Intents que siempre escalan a un humano sin intentar RAG.
_HUMAN_INTENTS = frozenset({"human_request", "complaint"})

# Conexiones que el pool del checkpointer abre para un solo mensaje. El grafo
# encadena a lo sumo dos escrituras de checkpoint (entrada y salida); no hace
# falta un pool grande para una sola tarea de Celery. min_size no puede ser
# mayor que max_size (psycopg_pool lo valida en el constructor), y el default
# de min_size es 4 -- hay que fijar los dos, no solo max_size.
CHECKPOINTER_POOL_MIN_SIZE = 1
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
        `"respond"`, `"human_handoff"`, `"scheduling"`, `"financial"`,
        `"marketing"` o `"rag_query"`.
    """
    intent = state.get("intent") or "unknown"

    if intent in _DIRECT_RESPONSE_INTENTS:
        return "respond"
    if intent in _HUMAN_INTENTS:
        return "human_handoff"
    if intent == "scheduling":
        return "scheduling"
    if intent == "financial":
        return "financial"
    if intent == "marketing":
        return "marketing"
    # rag_query y unknown: se intenta RAG primero.
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


def route_after_scheduling(state: ConversationState) -> str:
    """Decide si responder o escalar tras `scheduling_node`.

    Args:
        state: Estado tras `scheduling_node`.

    Returns:
        `"human_handoff"` si el agendamiento no estaba disponible para el
        tenant; `"respond"` en cualquier otro caso.
    """
    if state.get("requires_handoff"):
        return "human_handoff"
    return "respond"


def _logged(name: str, action_type: str, node: Any) -> Any:
    """Envuelve un nodo con `logged_node()` para `graph.add_node()`.

    Cast a `Any`: igual que en `_CheckpointedGraph.ainvoke()`, los overloads de
    LangGraph son dificiles de matchear estaticamente y un `Callable` que pasa
    por `functools.wraps()` no calza con ninguno de forma estructural.

    Args:
        name: Nombre del nodo, para el log.
        action_type: Tipo de accion del nodo.
        node: Funcion del nodo a envolver.

    Returns:
        El nodo envuelto, tipado `Any` para que `add_node()` lo acepte.
    """
    return cast("Any", logged_node(name, action_type)(node))


def build_conversation_graph() -> "StateGraph[ConversationState]":
    """Arma el grafo de conversacion, sin compilar.

    Returns:
        `StateGraph` con los 7 nodos (envueltos en `logged_node()`) y el
        routing condicional del sprint.
    """
    graph = StateGraph(ConversationState)

    # Cada nodo se envuelve con logged_node() al registrarlo, no en su propio
    # modulo: un unico punto de integracion (este archivo) en vez de tocar los
    # seis modulos de app/agents/nodes/ (addendum de Agent Activity Logging,
    # ver app/agents/middleware/logging_middleware.py).
    graph.add_node(
        NODE_TOKEN_BUDGET, _logged(NODE_TOKEN_BUDGET, "decision", token_budget_check_node)
    )
    graph.add_node(
        NODE_INTENT_ROUTING, _logged(NODE_INTENT_ROUTING, "decision", intent_routing_node)
    )
    graph.add_node(NODE_RAG_QUERY, _logged(NODE_RAG_QUERY, "query", rag_query_node))
    graph.add_node(NODE_RESPOND, _logged(NODE_RESPOND, "response", respond_node))
    graph.add_node(NODE_HUMAN_HANDOFF, _logged(NODE_HUMAN_HANDOFF, "handoff", human_handoff_node))
    graph.add_node(
        NODE_TRAINING_APPROVAL, _logged(NODE_TRAINING_APPROVAL, "decision", training_approval_node)
    )
    graph.add_node(NODE_SCHEDULING, _logged(NODE_SCHEDULING, "tool_call", scheduling_node))
    graph.add_node(NODE_FINANCIAL, _logged(NODE_FINANCIAL, "tool_call", financial_node))
    graph.add_node(NODE_MARKETING, _logged(NODE_MARKETING, "tool_call", marketing_node))

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
            "scheduling": NODE_SCHEDULING,
            "financial": NODE_FINANCIAL,
            "marketing": NODE_MARKETING,
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
    graph.add_conditional_edges(
        NODE_SCHEDULING,
        route_after_scheduling,
        {"respond": NODE_RESPOND, "human_handoff": NODE_HUMAN_HANDOFF},
    )

    # Los agentes especializados siempre terminan en `respond`: sus tools ya
    # devuelven texto util y no tienen una via de escalamiento propia como la
    # que tiene el agendamiento con Google Calendar.
    graph.add_edge(NODE_FINANCIAL, NODE_RESPOND)
    graph.add_edge(NODE_MARKETING, NODE_RESPOND)

    graph.add_edge(NODE_RESPOND, END)
    graph.add_edge(NODE_HUMAN_HANDOFF, END)
    graph.add_edge(NODE_TRAINING_APPROVAL, END)

    return graph


def _checkpointer_conninfo() -> str:
    """Resuelve el connection string de `psycopg` para el checkpointer.

    Siempre `DATABASE_URL` (el Transaction Pooler de Supavisor): es la unica
    variable que `docker-compose.yml` le pasa a los workers de Celery, y es
    ademas la eleccion correcta para un pool que se abre y cierra en cada
    mensaje (ver el docstring del modulo). No hay que esquivar el pooler como
    hace Alembic: `prepare_threshold=0` en `conn_kwargs` ya evita el problema
    de prepared statements, y el checkpointer no hace DDL.

    Returns:
        Connection string en formato `postgresql://...` (sin el sufijo
        `+asyncpg` que usa SQLAlchemy; `psycopg` no lo entiende).
    """
    url = get_settings().DATABASE_URL
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
            min_size=CHECKPOINTER_POOL_MIN_SIZE,
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
