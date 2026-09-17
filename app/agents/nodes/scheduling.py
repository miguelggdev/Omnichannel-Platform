"""Nodo de agendamiento del grafo (Sprint 7).

Contrato: `specs/sprint-07-scheduling-crm.md` §5. Usa GPT-4o con function
calling: la primera llamada decide si hace falta invocar una tool (consultar
disponibilidad, crear/modificar/cancelar una cita, listar las existentes); si
la llama, una segunda llamada redacta la respuesta final a partir del
resultado de la tool. El dialogo multi-turno ("¿a las 10 o a las 11?") lo
sostiene el checkpointing de LangGraph (Sprint 6), no este nodo.

Corrección sobre el spec: `tool_fn.ainvoke({**tool_args, "config": tool_config})`
mete el config DENTRO del diccionario de argumentos de la tool, en vez de
pasarlo como el segundo parámetro `config=` de `.ainvoke()`. Con eso, la tool
nunca recibe el `RunnableConfig` real (queda vacío) y explota con un
`KeyError` al leer `client_id` — se confirmó con un smoke test contra la
versión instalada de `langchain-core`. La forma correcta es
`tool_fn.ainvoke(tool_call["args"], config=tool_config)`.

Solo se atrapan las excepciones que significan "el agendamiento no está
usable para este tenant ahora mismo" (`SchedulingNotConfiguredError`,
`CalendarCredentialsError`, `HttpError` de Google): igual que
`intent_routing_node`, un fallo transitorio del LLM no se atrapa aquí y sube a
`app/tasks/ai_processor.py`, que reintenta y, agotados los intentos, escala a
un humano. Atraparlo aquí convertiría un fallo pasajero de OpenAI en un
handoff inmediato.
"""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from googleapiclient.errors import HttpError
from sqlalchemy import select

from app.agents.nodes._llm import extract_usage, get_chat_model, response_text
from app.agents.nodes._state import ConversationState
from app.agents.tools.calendar_tools import SCHEDULING_TOOLS
from app.core.config import get_settings
from app.core.database import tenant_session
from app.middleware.token_budget import TokenBudgetGuard
from app.models.agent_config import AgentConfig
from app.models.service_type import ServiceType
from app.services.calendar import (
    DEFAULT_TIMEZONE,
    CalendarCredentialsError,
    SchedulingNotConfiguredError,
)

logger = logging.getLogger(__name__)

OPERATION = "scheduling"

# Motivo de handoff cuando el agendamiento no esta disponible para el tenant
# (sin calendario configurado, sin service account, o la API de Google falla).
SCHEDULING_ERROR_REASON = "scheduling_unavailable"

_TOOLS_BY_NAME: dict[str, Any] = {herramienta.name: herramienta for herramienta in SCHEDULING_TOOLS}

SYSTEM_PROMPT_TEMPLATE = """Eres un asistente de agendamiento. Tu rol es ayudar a los clientes a:
1. Consultar disponibilidad de horarios
2. Crear citas nuevas
3. Modificar citas existentes
4. Cancelar citas
5. Consultar sus citas programadas

Reglas:
- SIEMPRE confirma con el usuario antes de crear o modificar una cita.
- Pregunta por el tipo de servicio si no lo especifica.
- Pregunta por la fecha y hora deseada si no lo especifica.
- Si no hay disponibilidad, ofrece fechas alternativas.
- Usa un tono amable y profesional.
- Responde en el idioma del usuario.
- Formatea las fechas de forma legible (dd/mm/yyyy HH:MM).

Tipos de servicio disponibles:
{service_types}

Fecha y hora actual: {current_datetime}
Zona horaria: {timezone}
"""


async def _tenant_scheduling_context(client_id: UUID) -> tuple[str, str]:
    """Arma el texto de tipos de servicio y resuelve el timezone del tenant.

    Args:
        client_id: Tenant para el que se agenda.

    Returns:
        Tupla `(texto_service_types, timezone)`.
    """
    async with tenant_session(client_id) as session:
        tipos = (
            (await session.execute(select(ServiceType).where(ServiceType.is_active.is_(True))))
            .scalars()
            .all()
        )
        config = (
            await session.execute(
                select(AgentConfig)
                .where(AgentConfig.is_active.is_(True))
                .order_by(AgentConfig.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if not tipos:
        texto = "No hay tipos de servicio configurados."
    else:
        texto = "\n".join(
            f"- {tipo.name}: {tipo.description or 'Sin descripción'} ({tipo.duration_minutes} minutos)"
            for tipo in tipos
        )

    scheduling_config: dict[str, Any] = (
        (config.config or {}).get("scheduling", {}) if config else {}
    )
    timezone = scheduling_config.get("timezone") or DEFAULT_TIMEZONE
    return texto, timezone


async def _final_response(
    model_to_use: str, system_prompt: str, user_message: str, tool_results: list[str]
) -> Any:
    """Genera la respuesta final incorporando los resultados de las tools.

    Args:
        model_to_use: Modelo de chat a usar.
        system_prompt: Prompt de sistema ya armado con el contexto del tenant.
        user_message: Mensaje original del usuario.
        tool_results: Textos devueltos por cada tool ejecutada.

    Returns:
        El `AIMessage` crudo (para poder extraer el consumo de tokens).
    """
    llm = get_chat_model(model_to_use, temperature=0.0)
    resultados_texto = "\n\n".join(tool_results)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
        {
            "role": "system",
            "content": (
                f"Resultados de las operaciones realizadas:\n{resultados_texto}\n\n"
                "Genera una respuesta amable para el usuario basandote en estos resultados."
            ),
        },
    ]
    return await llm.ainvoke(messages)


async def scheduling_node(state: ConversationState) -> dict[str, Any]:
    """Atiende un mensaje con intent de agendamiento usando GPT-4o + tools.

    Args:
        state: Estado del grafo; usa `client_id`, `conversation_id`, `message`
            y `model_to_use`.

    Returns:
        Dict parcial con `response_text` e `intent`, o con `requires_handoff`
        y `handoff_reason` si el agendamiento no está disponible para el tenant.
    """
    client_id = state["client_id"]
    conversation_id = state.get("conversation_id")
    message_text = (state.get("message") or {}).get("text") or ""
    model_to_use = state.get("model_to_use") or get_settings().OPENAI_CHAT_MODEL

    service_types_text, timezone = await _tenant_scheduling_context(UUID(client_id))
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        service_types=service_types_text,
        current_datetime=datetime.now().strftime("%Y-%m-%d %H:%M"),
        timezone=timezone,
    )

    llm = get_chat_model(model_to_use, temperature=0.0).bind_tools(SCHEDULING_TOOLS)
    response = await llm.ainvoke(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message_text},
        ]
    )
    prompt_tokens, completion_tokens = extract_usage(response)
    await TokenBudgetGuard.record_usage(
        client_id=client_id,
        conversation_id=conversation_id,
        model=model_to_use,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        operation=OPERATION,
    )

    tool_calls = getattr(response, "tool_calls", None) or []
    if not tool_calls:
        return {"response_text": response_text(response), "intent": "scheduling"}

    tool_config = {"configurable": {"client_id": client_id, "conversation_id": conversation_id}}
    tool_results: list[str] = []
    try:
        for tool_call in tool_calls:
            tool_fn = _TOOLS_BY_NAME.get(tool_call["name"])
            if tool_fn is None:
                tool_results.append(f"Herramienta '{tool_call['name']}' no reconocida.")
                continue
            resultado = await tool_fn.ainvoke(tool_call["args"], config=tool_config)
            tool_results.append(str(resultado))
    except (SchedulingNotConfiguredError, CalendarCredentialsError, HttpError):
        logger.exception("Agendamiento no disponible para el tenant %s", client_id)
        return {"requires_handoff": True, "handoff_reason": SCHEDULING_ERROR_REASON}

    final = await _final_response(
        model_to_use=model_to_use,
        system_prompt=system_prompt,
        user_message=message_text,
        tool_results=tool_results,
    )
    prompt_tokens, completion_tokens = extract_usage(final)
    await TokenBudgetGuard.record_usage(
        client_id=client_id,
        conversation_id=conversation_id,
        model=model_to_use,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        operation=OPERATION,
    )

    return {"response_text": response_text(final), "intent": "scheduling"}
