"""Nodo del agente de marketing: campanas y segmentacion (Sprint 12).

Contrato: `specs/sprint-12-agents-advanced.md` §2. Tool calling sobre
`marketing_tools.py` (segmentar, crear campana, enviarla, ver metricas).

Quien puede usarlo
-------------------
Este agente no atiende clientes finales: opera sobre la base de contactos del
tenant y dispara envios masivos. Solo se alcanza si el tenant habilito el
agente `marketing` en `agent_configs.config.enabled_agents`, igual que el
resto de los agentes del grafo, **y** solo atiende a los contactos declarados
como operadores en `agent_configs.config.marketing.operator_contact_ids`
(BUG-045): habilitarlo no dice nada de quien escribe, y este grafo es el mismo
que contesta a los clientes por WhatsApp. Las reglas que protegen de un envio indebido
(plantilla de WhatsApp aprobada, sin duplicados en 24 horas, envio siempre por
la cola `bulk`) viven en las tools, no en el prompt: un prompt lo puede
ignorar el modelo.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.agents.nodes._agent_tools import responder_con_tools
from app.agents.nodes._state import ConversationState
from app.agents.nodes._tenant import get_agent_settings
from app.agents.tools.marketing_tools import CANALES_VALIDOS, MARKETING_TOOLS
from app.core.database import tenant_session
from app.services.campaigns import es_operador_de_marketing

logger = logging.getLogger(__name__)

OPERATION = "marketing"
AGENTE = "marketing"
INTENT = "marketing"

MENSAJE_NO_HABILITADO = (
    "El modulo de marketing no esta habilitado para esta cuenta. "
    "Un administrador puede activarlo desde la configuracion."
)

#: Respuesta a un contacto que no es operador de marketing del tenant. No
#: menciona campanas ni envios: quien escribe es, casi siempre, un cliente.
MENSAJE_NO_AUTORIZADO = (
    "No puedo ayudarte con eso por este canal. Si tienes otra consulta, con gusto te ayudo."
)

SYSTEM_PROMPT_TEMPLATE = """Eres un agente de marketing conversacional.

Ayudas al usuario a:
1. Segmentar contactos por criterios
2. Crear campanas de mensajeria masiva
3. Lanzar el envio de una campana
4. Consultar las metricas de una campana

Reglas:
- SIEMPRE muestra cuantos contactos tiene el segmento y CONFIRMA con el usuario
  antes de lanzar una campana.
- Muestra el texto del mensaje tal como lo van a recibir los contactos antes de
  enviarlo.
- Las campanas de WhatsApp solo pueden usar plantillas aprobadas por Meta; si la
  herramienta responde que no lo esta, explicalo y no insistas.
- No lances dos veces la misma campana al mismo segmento.
- Si el usuario pide un criterio de segmentacion que la herramienta no acepta,
  dile cuales si acepta en vez de aproximar con otro.

Canales disponibles: {canales}

Fecha actual: {fecha}
"""


async def marketing_node(state: ConversationState) -> dict[str, Any]:
    """Atiende un mensaje de marketing con el modelo del tenant y sus tools.

    Args:
        state: Estado del grafo; usa `client_id`, `conversation_id`,
            `contact_id`, `message` y `model_to_use`.

    Returns:
        Dict parcial con `response_text` e `intent`.
    """
    client_id = state["client_id"]
    ajustes = await get_agent_settings(UUID(client_id))

    if AGENTE not in ajustes.enabled_agents:
        logger.info("Agente de marketing no habilitado para el tenant %s", client_id)
        return {"response_text": MENSAJE_NO_HABILITADO, "intent": INTENT}

    # Sin gastar una llamada al LLM: un cliente final que pide "manda una
    # promo a todos" no tiene que llegar a ver las tools.
    async with tenant_session(UUID(client_id)) as session:
        autorizado = await es_operador_de_marketing(
            session, UUID(client_id), state.get("contact_id")
        )
    if not autorizado:
        logger.warning(
            "Contacto %s del tenant %s pidio el agente de marketing sin ser operador",
            state.get("contact_id"),
            client_id,
        )
        return {"response_text": MENSAJE_NO_AUTORIZADO, "intent": INTENT}

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        canales=", ".join(CANALES_VALIDOS),
        fecha=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )

    texto = await responder_con_tools(
        state=dict(state),
        tools=MARKETING_TOOLS,
        system_prompt=system_prompt,
        modelo=state.get("model_to_use") or ajustes.model,
        operacion=OPERATION,
        temperatura=0.2,
    )
    return {"response_text": texto, "intent": INTENT}
