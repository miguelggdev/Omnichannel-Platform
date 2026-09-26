"""Bucle de tool calling que comparten los agentes especializados (Sprint 12).

`scheduling.py` (Sprint 7) trae este mismo baile escrito a mano: una primera
llamada que decide si hace falta invocar tools, la ejecucion de las que el
modelo pidio, y una segunda llamada que redacta la respuesta con los
resultados. Los agentes financiero y de marketing lo repetirian tal cual, asi
que aca vive una sola vez. `scheduling.py` no se toca en esta entrega: tiene
su propio manejo de errores de Google Calendar y sus tests, y reescribirlo
para que use esto es refactor, no sprint.

Dos cosas que se mantienen del original porque son deliberadas:

- `tool_fn.ainvoke(args, config=tool_config)`: el `config` va como parametro
  real de `.ainvoke()`, no dentro del dict de argumentos — metido ahi, la tool
  recibe un `RunnableConfig` vacio y no encuentra el `client_id`.
- Las excepciones del LLM no se atrapan: suben a `app/tasks/ai_processor.py`,
  que reintenta y, agotados los intentos, escala a un humano. Atraparlas aca
  convertiria un fallo pasajero de OpenAI en una respuesta generica.

Las de las **tools** si (BUG-045): unos argumentos mal formados del modelo
(`ValidationError`) o un fallo dentro de la tool tumbaban el turno entero, y el
reintento de `ai_processor.py` volvia a ejecutar las tools que ya habian
escrito — una segunda campana creada, por ejemplo. El fallo de una tool se le
devuelve al modelo como resultado, igual que una tool inexistente.
"""

import logging
from typing import Any

from app.agents.nodes._llm import extract_usage, get_chat_model, response_text
from app.middleware.token_budget import TokenBudgetGuard

logger = logging.getLogger(__name__)

# Cuantas respuestas de tools se le pasan al modelo para redactar el cierre.
MAX_TOOLS_POR_TURNO = 5


async def _registrar_consumo(
    respuesta: Any, client_id: str, conversation_id: str | None, modelo: str, operacion: str
) -> None:
    """Anota en el presupuesto del tenant lo que costo una llamada al LLM.

    Args:
        respuesta: Mensaje devuelto por el modelo.
        client_id: Tenant que consume.
        conversation_id: Conversacion en curso, si la hay.
        modelo: Modelo usado.
        operacion: Etiqueta de la operacion para el desglose.
    """
    prompt_tokens, completion_tokens = extract_usage(respuesta)
    await TokenBudgetGuard.record_usage(
        client_id=client_id,
        conversation_id=conversation_id,
        model=modelo,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        operation=operacion,
    )


async def responder_con_tools(
    *,
    state: dict[str, Any],
    tools: list[Any],
    system_prompt: str,
    modelo: str,
    operacion: str,
    temperatura: float = 0.0,
) -> str:
    """Resuelve un turno del agente: decide tools, las ejecuta y redacta.

    Args:
        state: Estado del grafo; usa `client_id`, `conversation_id`,
            `contact_id` y `message`.
        tools: Tools que se le ofrecen al modelo.
        system_prompt: Prompt de sistema ya armado con el contexto del tenant.
        modelo: Modelo de chat a usar.
        operacion: Etiqueta para el desglose del presupuesto de tokens.
        temperatura: Temperatura del modelo.

    Returns:
        El texto de la respuesta para el contacto.
    """
    client_id = state["client_id"]
    conversation_id = state.get("conversation_id")
    mensaje = (state.get("message") or {}).get("text") or ""

    llm = get_chat_model(modelo, temperature=temperatura).bind_tools(tools)
    primera = await llm.ainvoke(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": mensaje},
        ]
    )
    await _registrar_consumo(primera, client_id, conversation_id, modelo, operacion)

    tool_calls = (getattr(primera, "tool_calls", None) or [])[:MAX_TOOLS_POR_TURNO]
    if not tool_calls:
        return response_text(primera)

    por_nombre = {herramienta.name: herramienta for herramienta in tools}
    tool_config = {
        "configurable": {
            "client_id": client_id,
            "conversation_id": conversation_id,
            "contact_id": state.get("contact_id"),
        }
    }

    resultados: list[str] = []
    for tool_call in tool_calls:
        herramienta = por_nombre.get(tool_call["name"])
        if herramienta is None:
            logger.warning("El modelo pidio una tool inexistente: %s", tool_call["name"])
            resultados.append(f"Herramienta '{tool_call['name']}' no reconocida.")
            continue
        try:
            resultado = await herramienta.ainvoke(tool_call["args"], config=tool_config)
        except Exception as exc:
            logger.exception("La tool %s fallo", tool_call["name"])
            resultados.append(
                f"La herramienta '{tool_call['name']}' no pudo completarse "
                f"({type(exc).__name__}). No se hizo el cambio; explicaselo al usuario."
            )
            continue
        resultados.append(str(resultado))

    llm_final = get_chat_model(modelo, temperature=temperatura)
    final = await llm_final.ainvoke(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": mensaje},
            {
                "role": "system",
                "content": (
                    "Resultados de las operaciones realizadas:\n" + "\n\n".join(resultados) + "\n\n"
                    "Redacta la respuesta para el usuario a partir de estos resultados. "
                    "No inventes datos que no aparezcan arriba."
                ),
            },
        ]
    )
    await _registrar_consumo(final, client_id, conversation_id, modelo, operacion)
    return response_text(final)
