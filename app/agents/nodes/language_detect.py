"""Nodo `language_detect`: idioma del contacto (Sprint 14b, ADR-077).

Contrato: `specs/sprint-14-sandbox-i18n.md` §3 y §4. Va entre
`token_budget_check` e `intent_routing`, y deja `detected_language` en el estado
para que los nodos que generan texto respondan en ese idioma.

Reglas:

- **Una vez por conversacion.** El idioma se guarda en
  `conversations.metadata.detected_language` y las demas llamadas lo leen de ahi:
  no cambia de un mensaje a otro, y sobrevive a la purga de checkpoints de las
  conversaciones clinicas (el estado de LangGraph no es una fuente fiable).
- **Solo se guarda lo que se sabe.** `langdetect` decide con textos de 20
  caracteres o mas y seguridad alta (ver `app/services/i18n.py`); si no, un LLM.
  Si ninguno decide, se responde en el idioma del tenant **sin guardar nada**,
  para que el mensaje siguiente pueda volver a intentarlo en vez de quedar
  fijado a una conjetura sobre un "ok".
- **Idioma de rescate:** `clients.settings.default_language`, o `es`.
- **Una conversacion clinica nunca llega al LLM.** Lo dictado es dato de salud y
  ADR-072 lo mantiene fuera de otros modelos (el nodo de sentimiento hace lo
  mismo). `langdetect` es local, asi que ahi si se usa.
- **El nodo no rompe la conversacion:** cualquier fallo cae al idioma del tenant.
"""

import logging
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select, text

from app.agents.nodes._llm import extract_usage, get_chat_model
from app.agents.nodes._state import ConversationState
from app.core.config import get_settings
from app.core.database import tenant_session
from app.middleware.token_budget import TokenBudgetGuard
from app.models.client import Client
from app.services.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    detectar_idioma,
    normalizar_idioma,
)

logger = logging.getLogger(__name__)

OPERATION = "language_detection"

#: Clave de `clients.settings` con el idioma de rescate del tenant.
TENANT_LANGUAGE_KEY = "default_language"

LLM_PROMPT = (
    "Identifica el idioma del mensaje de un usuario. Responde solo con uno de estos "
    f"codigos: {', '.join(sorted(SUPPORTED_LANGUAGES))}. Si el mensaje es demasiado corto "
    "o ambiguo para saberlo con seguridad, o esta en otro idioma, responde 'unknown'."
)

_LEER_CONVERSACION = text(
    """
    SELECT metadata ->> 'detected_language' AS idioma,
           COALESCE((metadata ->> 'clinical')::boolean, false) AS clinica
    FROM conversations
    WHERE id = :conversation_id AND client_id = :client_id
    """
)

#: `IS NULL` en el WHERE: dos mensajes simultaneos no se pisan, gana el primero. El
#: `RETURNING` dice si este fue el que gano; si no, se lee el que quedo guardado.
_GUARDAR_IDIOMA = text(
    """
    UPDATE conversations
    SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
        'detected_language', CAST(:idioma AS text)
    )
    WHERE id = :conversation_id AND client_id = :client_id
      AND metadata ->> 'detected_language' IS NULL
    RETURNING metadata ->> 'detected_language'
    """
)


class IdiomaDetectado(BaseModel):
    """Salida estructurada del fallback con LLM.

    Attributes:
        language: Codigo de idioma soportado, o `unknown`.
    """

    language: Literal["es", "en", "pt", "it", "de", "fr", "unknown"]


async def _idioma_del_tenant(client_id: UUID) -> str:
    """Idioma de rescate del tenant (`clients.settings.default_language`).

    Args:
        client_id: Tenant.

    Returns:
        Un idioma soportado; `es` si el tenant no lo configuro o es invalido.
    """
    async with tenant_session(client_id) as session:
        ajustes = (
            await session.execute(select(Client.settings).where(Client.id == client_id))
        ).scalar_one_or_none()
    if isinstance(ajustes, dict):
        return normalizar_idioma(ajustes.get(TENANT_LANGUAGE_KEY)) or DEFAULT_LANGUAGE
    return DEFAULT_LANGUAGE


async def _leer_conversacion(client_id: UUID, conversation_id: str) -> tuple[str | None, bool]:
    """Lee el idioma ya guardado y si la conversacion es clinica.

    Args:
        client_id: Tenant.
        conversation_id: Conversacion en curso.

    Returns:
        `(idioma guardado o None, es_clinica)`.
    """
    async with tenant_session(client_id) as session:
        fila = (
            await session.execute(
                _LEER_CONVERSACION,
                {"client_id": str(client_id), "conversation_id": conversation_id},
            )
        ).first()
    if fila is None:
        return None, False
    return normalizar_idioma(fila.idioma), bool(fila.clinica)


async def _guardar_idioma(client_id: UUID, conversation_id: str, idioma: str) -> str:
    """Fija el idioma de la conversacion si todavia no tenia uno.

    Args:
        client_id: Tenant.
        conversation_id: Conversacion en curso.
        idioma: Idioma detectado en este turno.

    Returns:
        El idioma que quedo guardado: `idioma` si este llamante gano, o el que
        guardo antes otro mensaje simultaneo. Asi la conversacion no responde
        en dos idiomas a la vez.
    """
    async with tenant_session(client_id) as session:
        ganador = (
            await session.execute(
                _GUARDAR_IDIOMA,
                {
                    "client_id": str(client_id),
                    "conversation_id": conversation_id,
                    "idioma": idioma,
                },
            )
        ).scalar_one_or_none()
    if ganador is not None:
        return idioma
    guardado, _ = await _leer_conversacion(client_id, conversation_id)
    return guardado or idioma


async def detect_with_llm(state: ConversationState, texto: str) -> str | None:
    """Pregunta el idioma a un modelo barato y registra el consumo.

    Args:
        state: Estado del grafo (para el presupuesto de tokens).
        texto: Mensaje del contacto.

    Returns:
        Un idioma soportado, o `None` si el modelo no pudo decidir.
    """
    modelo = get_settings().OPENAI_FALLBACK_MODEL
    structured = get_chat_model(modelo, temperature=0.0).with_structured_output(
        IdiomaDetectado, include_raw=True
    )
    resultado = await structured.ainvoke(
        [
            {"role": "system", "content": LLM_PROMPT},
            {"role": "user", "content": texto},
        ]
    )

    parsed: Any = resultado.get("parsed") if isinstance(resultado, dict) else resultado
    crudo = resultado.get("raw") if isinstance(resultado, dict) else None
    if crudo is not None:
        prompt_tokens, completion_tokens = extract_usage(crudo)
        await TokenBudgetGuard.record_usage(
            client_id=state["client_id"],
            conversation_id=state.get("conversation_id"),
            model=modelo,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            operation=OPERATION,
        )
    if isinstance(parsed, IdiomaDetectado):
        return normalizar_idioma(parsed.language)
    return None


async def language_detect_node(state: ConversationState) -> dict[str, Any]:
    """Fija `detected_language` para el turno, detectandolo si hace falta.

    Args:
        state: Estado del grafo; usa `client_id`, `conversation_id` y `message`.

    Returns:
        `{"detected_language": <idioma soportado>}`. Siempre trae un idioma:
        el de la conversacion, el detectado o el de rescate del tenant.
    """
    client_id = UUID(state["client_id"])
    conversation_id = state["conversation_id"]
    texto = ((state.get("message") or {}).get("text") or "").strip()

    try:
        guardado, es_clinica = await _leer_conversacion(client_id, conversation_id)
        if guardado is not None:
            return {"detected_language": guardado}

        rescate = await _idioma_del_tenant(client_id)
        if not texto:
            return {"detected_language": rescate}

        idioma = detectar_idioma(texto)
        if idioma is None and not es_clinica:
            try:
                idioma = await detect_with_llm(state, texto)
            except Exception:
                logger.exception(
                    "Deteccion de idioma con LLM fallida en %s; se usa el del tenant",
                    conversation_id,
                )
        if idioma is None:
            return {"detected_language": rescate}

        return {"detected_language": await _guardar_idioma(client_id, conversation_id, idioma)}
    except Exception:
        logger.exception(
            "language_detect fallo en %s; la conversacion sigue en el idioma de rescate",
            conversation_id,
        )
        return {"detected_language": DEFAULT_LANGUAGE}
