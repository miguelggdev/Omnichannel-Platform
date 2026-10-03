"""Nodo de envio de la respuesta al contacto.

Contrato: `specs/sprint-06-langgraph.md` §8.

No invoca al LLM: la respuesta viene generada por `rag_query`. Su unico trabajo
extra es cubrir los intents que se contestan sin modelo (`greeting`, `farewell`)
y los casos en que el grafo llega hasta aqui sin texto.

El saludo usa `agent_configs.welcome_message` si el tenant configuro uno; es la
personalizacion mas visible del bot y no tiene sentido ignorarla.

Idioma (Sprint 14b, ADR-077): los textos fijos salen en el idioma del contacto
(`detected_language`). El espanol conserva los de siempre; ver `_texto_por_intent`.
"""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.agents.nodes._delivery import deliver_message
from app.agents.nodes._state import ConversationState
from app.agents.nodes._tenant import get_agent_settings
from app.core.database import tenant_session
from app.models.client import Client
from app.services.clinical_privacy import CONTENIDO_CLINICO_PROTEGIDO
from app.services.i18n import DEFAULT_LANGUAGE, get_system_message, normalizar_idioma

logger = logging.getLogger(__name__)

DEFAULT_GREETING = "Hola! En que puedo ayudarte hoy?"
DEFAULT_FAREWELL = "Hasta luego! Si necesitas algo mas, no dudes en escribirme."
DEFAULT_FALLBACK = "Disculpa, no entendi tu mensaje. Podrias reformularlo?"


async def _nombre_del_negocio(client_id: UUID) -> str:
    """Nombre del tenant, para el saludo por defecto en los idiomas que no son el espanol.

    Args:
        client_id: Tenant.

    Returns:
        `clients.name`, o una cadena vacia si no se encuentra.
    """
    async with tenant_session(client_id) as session:
        nombre = (
            await session.execute(select(Client.name).where(Client.id == client_id))
        ).scalar_one_or_none()
    return str(nombre or "")


async def _texto_por_intent(client_id: UUID, intent: str | None, idioma: str | None = None) -> str:
    """Devuelve la respuesta fija que corresponde a un intent sin generacion.

    El espanol conserva los textos de siempre (tuteo); los demas idiomas salen
    de `app/services/i18n.py`. El saludo que el tenant configuro manda siempre.

    Args:
        client_id: Tenant propietario, para leer su mensaje de bienvenida.
        intent: Intent detectado.
        idioma: Idioma detectado del contacto; `None` o `es` es espanol.

    Returns:
        Texto a enviar.
    """
    traducir = normalizar_idioma(idioma) not in (None, DEFAULT_LANGUAGE)
    if intent == "greeting":
        settings = await get_agent_settings(client_id)
        if settings.welcome_message:
            return settings.welcome_message
        if traducir:
            return get_system_message(
                "welcome", idioma, business_name=await _nombre_del_negocio(client_id)
            )
        return DEFAULT_GREETING
    if intent == "farewell":
        return get_system_message("farewell", idioma) if traducir else DEFAULT_FAREWELL
    return get_system_message("fallback", idioma) if traducir else DEFAULT_FALLBACK


async def respond_node(state: ConversationState) -> dict[str, Any]:
    """Envia la respuesta al contacto y la registra en la conversacion.

    Args:
        state: Estado del grafo; usa `client_id`, `conversation_id`,
            `contact_id`, `channel`, `intent` y `response_text`.

    Returns:
        Dict parcial con el `response_text` realmente enviado.
    """
    client_id = UUID(state["client_id"])
    conversation_id = UUID(state["conversation_id"])
    contact_id = UUID(state["contact_id"])
    channel = state["channel"]

    texto = (state.get("response_text") or "").strip()
    if not texto:
        texto = await _texto_por_intent(
            client_id, state.get("intent"), state.get("detected_language")
        )

    await deliver_message(
        client_id=client_id,
        conversation_id=conversation_id,
        contact_id=contact_id,
        channel=channel,
        text=texto,
        # Lo dictado por un profesional y lo que se le responde son datos de
        # salud: al contacto le llega completo, pero el historial no lo guarda.
        stored_text=CONTENIDO_CLINICO_PROTEGIDO if state.get("intent") == "clinical" else None,
    )
    logger.info("Respuesta enviada en la conversacion %s", conversation_id)
    return {"response_text": texto}
