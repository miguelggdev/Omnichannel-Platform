"""Nodo del agente financiero: facturacion electronica DIAN (Sprint 12).

Contrato: `specs/sprint-12-agents-advanced.md` §1. Usa el modelo del tenant con
tool calling sobre `invoice_tools.py` (validar NIT, emitir factura, consultar
estado, listar facturas). El dialogo multi-turno ("me falta el NIT") lo
sostiene el checkpointing de LangGraph, no este nodo.

Como se sabe si el agente esta habilitado
------------------------------------------
El spec lo mira en `state["agent_configs"]["financial"]`, una clave que
`ConversationState` no tiene en este repo. La fuente real es
`agent_configs.config.enabled_agents` del tenant, que ya lee
`get_agent_settings()` y de donde salen los intents disponibles
(`intent_router.py`). Sin `financial` ahi, el nodo ni siquiera se alcanza por
routing; si se alcanza por otra via, contesta que el modulo no esta habilitado
en vez de gastar una llamada al LLM.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.agents.nodes._agent_tools import responder_con_tools
from app.agents.nodes._state import ConversationState
from app.agents.nodes._tenant import get_agent_settings
from app.agents.tools.invoice_tools import INVOICE_TOOLS
from app.core.database import tenant_session
from app.models.contact import Contact

logger = logging.getLogger(__name__)

OPERATION = "financial"
AGENTE = "financial"
INTENT = "financial"

MENSAJE_NO_HABILITADO = (
    "El modulo de facturacion no esta habilitado para esta cuenta. "
    "Si necesitas una factura, un agente puede ayudarte."
)

SYSTEM_PROMPT_TEMPLATE = """Eres un agente financiero especializado en facturacion electronica colombiana (DIAN).

Ayudas al usuario a:
1. Validar un NIT o cedula
2. Emitir facturas electronicas
3. Consultar el estado de una factura
4. Revisar su historial de facturacion

Reglas:
- SIEMPRE valida el NIT antes de emitir una factura.
- Reune TODOS los datos antes de emitir: NIT, razon social o nombre, y cada
  item con su descripcion, cantidad, precio unitario e IVA.
- El IVA solo puede ser 0, 5 o 19 por ciento. Si el usuario no lo dice,
  preguntalo; no lo asumas.
- CONFIRMA con el usuario el detalle y el total antes de emitir la factura.
- Si falta informacion, pide exactamente lo que falta, sin rodeos.
- Si una factura queda pendiente de validacion ante la DIAN, dilo tal cual:
  no afirmes que fue aprobada.
- Responde en el idioma del usuario y con importes legibles.

Contexto de la conversacion:
{contexto}

Fecha actual: {fecha}
"""


async def construir_contexto(client_id: UUID, contact_id: str | None) -> str:
    """Arma el contexto de facturacion del contacto para el prompt.

    Args:
        client_id: Tenant dueno de la conversacion.
        contact_id: Contacto de la conversacion, si lo hay.

    Returns:
        Texto con lo que ya se sabe del comprador, o una nota de que no hay
        datos previos.
    """
    if not contact_id:
        return "Sin contacto identificado en esta conversacion."

    async with tenant_session(client_id) as session:
        contacto = (
            await session.execute(
                select(Contact).where(
                    Contact.id == UUID(contact_id), Contact.client_id == client_id
                )
            )
        ).scalar_one_or_none()

    if contacto is None:
        return "Sin contacto identificado en esta conversacion."

    partes: list[str] = []
    nombre = (
        contacto.display_name
        or " ".join(filter(None, [contacto.first_name, contacto.last_name])).strip()
    )
    if nombre:
        partes.append(f"Contacto: {nombre}.")

    metadata: dict[str, Any] = contacto.metadata_ or {}
    if metadata.get("nit"):
        partes.append(f"NIT registrado del contacto: {metadata['nit']}.")
    if metadata.get("razon_social"):
        partes.append(f"Razon social registrada: {metadata['razon_social']}.")

    return " ".join(partes) or "Sin datos de facturacion previos."


async def financial_node(state: ConversationState) -> dict[str, Any]:
    """Atiende un mensaje de facturacion con el modelo del tenant y sus tools.

    Args:
        state: Estado del grafo; usa `client_id`, `conversation_id`,
            `contact_id`, `message` y `model_to_use`.

    Returns:
        Dict parcial con `response_text` e `intent`.
    """
    client_id = state["client_id"]
    ajustes = await get_agent_settings(UUID(client_id))

    if AGENTE not in ajustes.enabled_agents:
        logger.info("Agente financiero no habilitado para el tenant %s", client_id)
        return {"response_text": MENSAJE_NO_HABILITADO, "intent": INTENT}

    contexto = await construir_contexto(UUID(client_id), state.get("contact_id"))
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        contexto=contexto,
        fecha=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )

    texto = await responder_con_tools(
        state=dict(state),
        tools=INVOICE_TOOLS,
        system_prompt=system_prompt,
        modelo=state.get("model_to_use") or ajustes.model,
        operacion=OPERATION,
        # Facturar es aritmetica y datos fiscales: nada que "redactar con
        # creatividad". La temperatura mas baja del grafo.
        temperatura=0.0,
    )
    return {"response_text": texto, "intent": INTENT}
