"""Worker de Celery que hace correr el grafo de conversacion.

Cola: `ai_inference` (concurrency=2 en docker-compose). El nombre de la tarea
empieza por `app.tasks.ai_` para que el `task_routes` de `celery_config.py` la
enrute sola a esa cola.

Limites de tiempo: 120s duros, 100s blandos (`specs/sprint-06-langgraph.md` §11).
El grafo encadena como mucho dos llamadas al LLM (intent + generacion), cada una
con timeout de 30s.

Reintentos: 2. Agotados —o ante un fallo que reintentar no arregla— la
conversacion **no** se queda muda: `_emergency_handoff()` la escala a un humano.
Es la misma politica que `document_ingestion.py` aplica a los documentos, con la
diferencia de que aqui hay una persona esperando una respuesta.

El grafo (`app/agents/graph.py`) es entrega de Dev A y se importa de forma
perezosa: el worker arranca y el webhook encola aunque esa pieza no este todavia
en `main`; mientras tanto cada mensaje se escala a un humano con el motivo
visible en `conversations.metadata.handoff`.

**El grafo compilado no se cachea entre invocaciones, a proposito.** La spec
propone un `_graph_cache` por tenant (§12), pero cada tarea de Celery abre su
propio event loop: un grafo compilado guarda el checkpointer —y sus conexiones a
PostgreSQL— atado al loop en el que se creo, y reusarlo en el siguiente loop es
exactamente BUG-006 / BUG-011. Compilar cuesta microsegundos; el checkpointer es
lo unico que abre conexiones, y `run_isolated()` las cierra al terminar.

Por eso la tarea no llama a `asyncio.run()` sino a `run_isolated()`
(`app/core/database.py`, BUG-011): vacia el pool del engine en el mismo loop que
lo lleno, que es la regla para todo `app/tasks/*.py` desde PR #12.
"""

import logging
from typing import Any

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded

from app.agents.nodes._state import ConversationState
from app.core.config import get_settings
from app.core.database import run_isolated

logger = logging.getLogger(__name__)

# Motivo con el que se escala cuando el grafo no pudo correr.
GRAPH_ERROR_REASON = "graph_error"

RETRY_COUNTDOWN_SECONDS = 10


class GraphUnavailableError(RuntimeError):
    """El grafo de conversacion todavia no esta instalado.

    No se reintenta: reintentar no va a hacer aparecer `app/agents/graph.py`. El
    mensaje se escala a un humano, que es la degradacion correcta.
    """


def _initial_state(
    client_id: str,
    conversation_id: str,
    contact_id: str,
    channel: str,
    message_data: dict[str, Any],
) -> ConversationState:
    """Arma el estado inicial con el que entra el mensaje al grafo.

    Args:
        client_id: Tenant propietario.
        conversation_id: Conversacion en curso.
        contact_id: Contacto que escribio.
        channel: Canal de origen.
        message_data: `NormalizedMessage` serializado.

    Returns:
        Estado completo, con todos los campos inicializados.
    """
    return ConversationState(
        client_id=client_id,
        conversation_id=conversation_id,
        contact_id=contact_id,
        channel=channel,
        message=message_data,
        intent=None,
        intent_confidence=None,
        rag_context=None,
        rag_confidence=None,
        response_text=None,
        budget_status="ok",
        model_to_use=get_settings().OPENAI_CHAT_MODEL,
        budget_usage_pct=0.0,
        requires_handoff=False,
        handoff_reason=None,
        training_mode=False,
        approved_examples=None,
        error=None,
    )


async def _compile_graph() -> Any:
    """Obtiene el grafo compilado de Dev A.

    Returns:
        Grafo compilado, con checkpointer si `app/agents/graph.py` lo expone.

    Raises:
        GraphUnavailableError: Si el modulo del grafo no existe o no expone
            ninguna forma conocida de construirlo.
    """
    try:
        from app.agents import graph as graph_module
    except ImportError as exc:
        raise GraphUnavailableError(
            "Grafo de conversacion no disponible (app/agents/graph.py)"
        ) from exc

    con_checkpointer = getattr(graph_module, "get_graph_with_checkpointer", None)
    if con_checkpointer is not None:
        compilado: Any = await con_checkpointer()
        return compilado

    build = getattr(graph_module, "build_conversation_graph", None)
    if build is None:
        raise GraphUnavailableError(
            "app/agents/graph.py no expone get_graph_with_checkpointer() ni "
            "build_conversation_graph()"
        )

    # Sin checkpointer el grafo corre igual, pero no persiste estado entre
    # mensajes: se avisa para que no pase por bueno un multi-turno que no lo es.
    logger.warning("Grafo compilado sin checkpointer: el estado no persiste entre mensajes")
    grafo: Any = build().compile()
    return grafo


async def _invoke_graph(
    client_id: str,
    conversation_id: str,
    contact_id: str,
    channel: str,
    message_data: dict[str, Any],
) -> dict[str, Any]:
    """Ejecuta el grafo de conversacion para un mensaje.

    Args:
        client_id: Tenant propietario.
        conversation_id: Conversacion en curso.
        contact_id: Contacto que escribio.
        channel: Canal de origen.
        message_data: `NormalizedMessage` serializado.

    Returns:
        Estado final del grafo.
    """
    compiled = await _compile_graph()
    estado = _initial_state(client_id, conversation_id, contact_id, channel, message_data)

    # El thread_id agrupa los checkpoints de una conversacion; lleva el client_id
    # por delante para que dos tenants no puedan colisionar en el mismo hilo.
    config = {"configurable": {"thread_id": f"{client_id}:{conversation_id}"}}

    resultado: dict[str, Any] = await compiled.ainvoke(estado, config=config)
    logger.info(
        "Grafo completado para %s: intent=%s handoff=%s",
        conversation_id,
        resultado.get("intent"),
        resultado.get("requires_handoff"),
    )
    return resultado


async def _emergency_handoff(
    client_id: str,
    conversation_id: str,
    contact_id: str,
    channel: str,
    reason: str,
) -> None:
    """Escala la conversacion a un humano cuando el grafo no pudo responder.

    Reusa el nodo de handoff en vez de duplicar su logica: deja la conversacion
    en `waiting_human`, avisa al contacto y encola el aviso al equipo.

    Args:
        client_id: Tenant propietario.
        conversation_id: Conversacion afectada.
        contact_id: Contacto que espera respuesta.
        channel: Canal de origen.
        reason: Motivo que se registra en `conversations.metadata.handoff`.
    """
    from app.agents.nodes.human_handoff import human_handoff_node

    logger.critical("Handoff de emergencia en la conversacion %s (%s)", conversation_id, reason)
    await human_handoff_node(
        ConversationState(
            client_id=client_id,
            conversation_id=conversation_id,
            contact_id=contact_id,
            channel=channel,
            handoff_reason=reason,
        )
    )


@shared_task(
    name="app.tasks.ai_process_response",
    bind=True,
    max_retries=2,
    acks_late=True,
    queue="ai_inference",
    time_limit=120,
    soft_time_limit=100,
)
def process_ai_response(
    self: Any,
    client_id: str,
    conversation_id: str,
    contact_id: str,
    channel: str,
    message_data: dict[str, Any],
) -> dict[str, str]:
    """Procesa un mensaje entrante con el grafo de agentes.

    Args:
        self: Instancia de la tarea (bind=True), para los reintentos.
        client_id: Tenant propietario.
        conversation_id: Conversacion en curso.
        contact_id: Contacto que escribio.
        channel: Canal de origen.
        message_data: `NormalizedMessage` serializado.

    Returns:
        `{"status": "processed"}`, o `{"status": "handoff"}` si hubo que escalar.

    Raises:
        Retry: Reintento (2 como maximo) ante un fallo transitorio.
    """
    try:
        run_isolated(
            _invoke_graph(
                client_id=client_id,
                conversation_id=conversation_id,
                contact_id=contact_id,
                channel=channel,
                message_data=message_data,
            )
        )
    except (GraphUnavailableError, SoftTimeLimitExceeded) as exc:
        # Ninguno de los dos mejora reintentando: el modulo no va a aparecer y un
        # grafo que agoto 100s volveria a agotarlos.
        logger.error("Grafo no ejecutable en %s: %s", conversation_id, exc)
        run_isolated(
            _emergency_handoff(client_id, conversation_id, contact_id, channel, GRAPH_ERROR_REASON)
        )
        return {"status": "handoff"}
    except Exception as exc:
        if self.request.retries < self.max_retries:
            logger.warning(
                "Fallo el grafo en %s (intento %s/%s): %s",
                conversation_id,
                self.request.retries + 1,
                self.max_retries,
                exc,
            )
            raise self.retry(exc=exc, countdown=RETRY_COUNTDOWN_SECONDS) from exc

        logger.critical(
            "Grafo agotado tras %s intentos en %s: %s",
            self.max_retries,
            conversation_id,
            exc,
            exc_info=True,
        )
        run_isolated(
            _emergency_handoff(client_id, conversation_id, contact_id, channel, GRAPH_ERROR_REASON)
        )
        return {"status": "handoff"}

    return {"status": "processed"}
