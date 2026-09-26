"""`logged_node` — decorator que envuelve un nodo del grafo con logging automático.

Desviación de arquitectura sobre `specs/sprint-07-addendum-agent-logging.md`
§4 (ya anticipada por la nota de reprogramación al inicio del spec): el
pseudocódigo espera una sesión de DB inyectada en el estado
(`state["_db_session"]`). Esa sesión no existe — `app/agents/state.py` es
`total=False` y cada nodo real abre su propia `tenant_session()` (ver
`app/agents/nodes/_tenant.py` y el resto de `app/agents/nodes/`). Por eso
`logged_node` abre su **propia** `tenant_session()`, después de que el nodo
envuelto termina (o falla): una transacción corta, aislada, solo para
escribir el log — no comparte conexión ni transacción con lo que hizo el nodo.

El logging es "best effort": si escribir el log falla (una caída pasajera de
Postgres, por ejemplo), el error se registra con `logger.exception()` y se
descarta. Una tabla de auditoría que se cae no debe tumbar la respuesta al
contacto — eso sería peor que no tener el log.

`_tokens_used`/`_model_used` en el resultado del nodo son opcionales: hoy
ningún nodo de Sprint 6 los agrega a su dict de retorno (habría que tocar cada
uno para sumarlos), así que por ahora `tokens_used`/`model_used` quedan en
`0`/`None` salvo que un nodo futuro los incluya a propósito.

`_build_input_summary`/`_build_output_summary`/`_extract_details` se
reescriben contra los campos reales de `ConversationState` (`message`,
`intent`, `intent_confidence`, `response_text`, `rag_confidence`,
`requires_handoff`, `handoff_reason`, `budget_status`, `budget_usage_pct`,
`training_mode`) — el spec original los escribió contra un estado más viejo
(`last_user_message`, `detected_language`, `sentiment`, ...) que Sprint 6
nunca implementó así.
"""

import functools
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from app.agents.state import ConversationState
from app.core.database import tenant_session
from app.services.agent_logger import AgentLogger

logger = logging.getLogger(__name__)

# Campos de ConversationState que interesan en el resumen del output de un nodo.
_OUTPUT_SUMMARY_FIELDS: tuple[str, ...] = (
    "intent",
    "intent_confidence",
    "response_text",
    "rag_confidence",
    "requires_handoff",
    "handoff_reason",
    "budget_status",
    "current_sentiment",
)

# Campos que se guardan tal cual en `details` (JSONB), sin resumir a texto.
_DETAIL_FIELDS: frozenset[str] = frozenset(
    {
        "intent_confidence",
        "rag_confidence",
        "budget_status",
        "budget_usage_pct",
        "requires_handoff",
        "handoff_reason",
        "training_mode",
        "current_sentiment",
        "sentiment_score",
        "consecutive_very_negative",
    }
)

Node = Callable[[ConversationState], Awaitable[dict[str, Any]]]


def _build_input_summary(state: ConversationState, node_name: str) -> str:
    """Arma un resumen legible de lo que el nodo recibió.

    Args:
        state: Estado del grafo antes de correr el nodo.
        node_name: Nombre del nodo, para el fallback si no hay nada que resumir.

    Returns:
        Texto corto con el mensaje y el intent conocido, o un placeholder.
    """
    partes: list[str] = []
    mensaje = state.get("message") or {}
    texto = mensaje.get("text") if isinstance(mensaje, dict) else None
    if texto:
        partes.append(f"msg: {texto[:200]}")
    if state.get("intent"):
        partes.append(f"intent: {state['intent']}")
    return " | ".join(partes) if partes else f"[{node_name} input]"


def _build_output_summary(result: dict[str, Any], node_name: str) -> str:
    """Arma un resumen legible de lo que el nodo devolvió.

    Args:
        result: Dict parcial que devolvió el nodo.
        node_name: Nombre del nodo, para el fallback si no hay nada que resumir.

    Returns:
        Texto corto con los campos relevantes presentes en `result`.
    """
    partes: list[str] = []
    for campo in _OUTPUT_SUMMARY_FIELDS:
        if campo not in result:
            continue
        valor = result[campo]
        if isinstance(valor, str) and len(valor) > 200:
            valor = valor[:200] + "..."
        partes.append(f"{campo}: {valor}")
    return " | ".join(partes) if partes else f"[{node_name} output]"


def _extract_details(result: dict[str, Any]) -> dict[str, Any]:
    """Copia a `details` (JSONB) los campos estructurados que trae `result`.

    Args:
        result: Dict parcial que devolvió el nodo.

    Returns:
        Solo las claves de `_DETAIL_FIELDS` presentes en `result`.
    """
    return {campo: result[campo] for campo in _DETAIL_FIELDS if campo in result}


async def _registrar(
    *,
    client_id: str,
    conversation_id: str,
    node_name: str,
    action_type: str,
    input_summary: str,
    output_summary: str | None = None,
    details: dict[str, Any] | None = None,
    duration_ms: int,
    tokens_used: int = 0,
    model_used: str | None = None,
    status: str,
    error_message: str | None = None,
) -> None:
    """Escribe una entrada de `agent_action_logs` en su propia transacción.

    Nunca propaga: un fallo al loguear no debe tumbar la respuesta al contacto.

    Args:
        client_id: Tenant dueño de la conversación.
        conversation_id: Conversación en la que corrió el nodo.
        node_name: Nombre del nodo.
        action_type: Tipo de acción del nodo.
        input_summary: Resumen del input.
        output_summary: Resumen del output, si el nodo no falló.
        details: Detalles estructurados para el JSONB.
        duration_ms: Duración de ejecución del nodo, en milisegundos.
        tokens_used: Tokens consumidos, si el nodo los reportó.
        model_used: Modelo de LLM usado, si el nodo lo reportó.
        status: `success` o `error`.
        error_message: Mensaje de error, si `status == "error"`.
    """
    try:
        tenant_id = UUID(client_id)
        async with tenant_session(tenant_id) as session:
            await AgentLogger(session, tenant_id).log_action(
                conversation_id=UUID(conversation_id),
                node_name=node_name,
                action_type=action_type,
                input_summary=input_summary,
                output_summary=output_summary,
                details=details,
                duration_ms=duration_ms,
                tokens_used=tokens_used,
                model_used=model_used,
                status=status,
                error_message=error_message,
            )
    except Exception:
        logger.exception("No se pudo registrar la actividad del nodo %s", node_name)


def logged_node(node_name: str, action_type: str = "decision") -> Callable[[Node], Node]:
    """Decorator que envuelve un nodo del grafo con logging automático.

    Args:
        node_name: Nombre del nodo tal como debe quedar en `agent_action_logs`.
        action_type: Tipo de acción por defecto (`decision`, `query`,
            `response`, `tool_call`, `handoff`).

    Returns:
        Decorator que envuelve la función del nodo sin cambiar su firma.
    """

    def decorator(func: Node) -> Node:
        @functools.wraps(func)
        async def wrapper(state: ConversationState) -> dict[str, Any]:
            client_id = state.get("client_id")
            conversation_id = state.get("conversation_id")
            if not client_id or not conversation_id:
                # Sin tenant/conversacion no hay donde loguear (RLS los exige);
                # el nodo corre igual, solo sin traza.
                return await func(state)

            input_summary = _build_input_summary(state, node_name)
            inicio = time.monotonic()

            try:
                result = await func(state)
            except Exception as exc:
                duracion_ms = int((time.monotonic() - inicio) * 1000)
                # El traceback completo (rutas de archivo, nombres internos) va
                # SOLO al log del servidor, nunca a `details`: ese JSONB lo
                # devuelve tal cual `AgentActionLogResponse` (app/schemas/agent_log.py)
                # a cualquier admin/supervisor del tenant via GET /agent-logs/...,
                # y CLAUDE.md prohibe exponer tracebacks al cliente (regla 3/5).
                logger.exception("Fallo en el nodo %s", node_name)
                await _registrar(
                    client_id=client_id,
                    conversation_id=conversation_id,
                    node_name=node_name,
                    action_type="error",
                    input_summary=input_summary,
                    details={"exception_type": type(exc).__name__},
                    duration_ms=duracion_ms,
                    status="error",
                    error_message=str(exc)[:500],
                )
                raise

            duracion_ms = int((time.monotonic() - inicio) * 1000)
            await _registrar(
                client_id=client_id,
                conversation_id=conversation_id,
                node_name=node_name,
                action_type=action_type,
                input_summary=input_summary,
                output_summary=_build_output_summary(result, node_name),
                details=_extract_details(result),
                duration_ms=duracion_ms,
                tokens_used=result.get("_tokens_used", 0),
                model_used=result.get("_model_used"),
                status="success",
            )
            return result

        return wrapper

    return decorator
