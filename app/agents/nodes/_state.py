"""Contrato de `ConversationState` mientras Dev A no entrega `app/agents/state.py`.

`app/agents/state.py` es de Dev A (ver matriz de asignacion, METHODOLOGY.md §6):
este modulo NO lo sustituye, solo copia el contrato de
`specs/sprint-06-langgraph.md` §1 para que los nodos puedan tiparse y testearse
antes de que esa pieza entre en `main`.

TODO(Dev A): cuando `app/agents/state.py` este en `main`, reemplazar el cuerpo de
este modulo por `from app.agents.state import ConversationState` y borrar la copia.
La comprobacion de mypy es estructural entre TypedDicts, asi que el cambio no
toca a los nodos.

`total=False` (la spec lo declara total) a proposito: los nodos leen el estado
con `.get()` y los tests construyen estados parciales. Ningun nodo asume que una
clave opcional exista.
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
