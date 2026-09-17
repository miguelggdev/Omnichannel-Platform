"""ConversationState — estado que fluye por el grafo de conversacion.

Contrato: `specs/sprint-06-langgraph.md` §1. Cada nodo lee lo que necesita del
estado y devuelve un dict parcial con solo los campos que modifica; LangGraph
mergea ese dict con el estado existente (PAT-003, ver MEMORY.md). Ningun nodo
muta el estado directamente.

`total=False`, a diferencia del pseudocodigo de la spec (que lo declara
`total=True`): los nodos leen con `.get()` y los tests construyen estados
parciales (`tests/unit/agent_doubles.py::estado()`, `tests/integration/
test_graph_flow.py::Escenario.estado()`). La comprobacion de mypy entre
TypedDicts es estructural, asi que este cambio de totalidad no le pide nada a
los nodos que ya consumen este contrato.

Este modulo reemplaza la copia temporal en `app/agents/nodes/_state.py` (ver el
TODO ahi): a partir de este commit, `_state.py` es un simple re-export.
"""

from typing import Any, TypedDict


class ConversationState(TypedDict, total=False):
    """Estado que fluye por el grafo de conversacion.

    Attributes:
        client_id: Tenant duenno de la conversacion.
        conversation_id: Conversacion en curso.
        contact_id: Contacto que escribio.
        channel: Canal de origen (whatsapp, instagram, facebook).
        message: `NormalizedMessage` serializado.
        intent: Intent detectado por el router semantico.
        intent_confidence: Confianza (0.0 - 1.0) de la clasificacion.
        rag_context: Chunks recuperados, con su citacion.
        rag_confidence: Similaridad promedio de los chunks recuperados.
        response_text: Respuesta generada o enviada.
        budget_status: `ok` | `degraded` | `exceeded`.
        model_to_use: Modelo que deben usar los nodos que invocan al LLM.
        budget_usage_pct: Porcentaje de presupuesto consumido.
        requires_handoff: Si la conversacion debe pasar a un humano.
        handoff_reason: Motivo del handoff.
        training_mode: Si el tenant tiene modo entrenamiento activo.
        approved_examples: Few-shot recuperados de `approved_responses`.
        error: Ultimo error no fatal registrado por un nodo.
        partial_results: Resultados de tools que si se ejecutaron antes de un
            error en el mismo turno (ej. `scheduling_node` con varias
            tool_calls), para que un handoff no pierda esa informacion.
    """

    client_id: str
    conversation_id: str
    contact_id: str
    channel: str
    message: dict[str, Any]

    intent: str | None
    intent_confidence: float | None

    rag_context: list[dict[str, Any]] | None
    rag_confidence: float | None

    response_text: str | None

    budget_status: str
    model_to_use: str
    budget_usage_pct: float

    requires_handoff: bool
    handoff_reason: str | None

    training_mode: bool
    approved_examples: list[dict[str, Any]] | None

    error: str | None
    partial_results: list[str] | None
