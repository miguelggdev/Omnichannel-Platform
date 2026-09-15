"""Nodo de RAG con grounding estricto.

Contrato: `specs/sprint-06-langgraph.md` §5. Se apoya en `RAGService` (Sprint 5,
Dev A) para el retrieval y la construccion del prompt; aqui vive la decision de
si hay contexto suficiente para contestar.

Regla del sprint: **nunca responder sin contexto**. Si el retrieval no devuelve
un solo chunk por encima del umbral del tenant, el nodo no llama al LLM: marca
`requires_handoff` con motivo `insufficient_context` y el grafo escala a un
humano. Lo mismo si el modelo devuelve una respuesta vacia.

Los few-shot de `approved_responses` se recuperan con un umbral mas estricto
(`agent_configs.similarity_threshold`, 0.80 por defecto) que los chunks de
contexto (`config.rag_threshold`, 0.75): un ejemplo mal elegido contamina el
estilo de la respuesta mas de lo que un chunk de mas la degrada.

Las excepciones (OpenAI, base de datos) no se atrapan: suben a la tarea de
Celery, que reintenta y termina escalando a un humano.
"""

import logging
from typing import Any
from uuid import UUID

from app.agents.nodes._llm import extract_usage, get_chat_model, response_text
from app.agents.nodes._state import ConversationState
from app.agents.nodes._tenant import get_agent_settings
from app.core.config import get_settings
from app.middleware.token_budget import TokenBudgetGuard
from app.services.embedding import EmbeddingService
from app.services.rag import FEW_SHOT_TOP_K, RAGService

logger = logging.getLogger(__name__)

OPERATION = "rag_query"


def build_rag_service() -> RAGService:
    """Construye el servicio de RAG con el embedding configurado.

    Returns:
        `RAGService` listo para `retrieve()`.
    """
    settings = get_settings()
    return RAGService(
        embedding_service=EmbeddingService(
            api_key=settings.OPENAI_API_KEY,
            model=settings.OPENAI_EMBEDDING_MODEL,
        )
    )


def _handoff(reason: str) -> dict[str, Any]:
    """Arma la salida del nodo cuando no se puede contestar.

    Args:
        reason: Motivo del handoff.

    Returns:
        Dict parcial que deja la conversacion lista para escalar.
    """
    return {
        "rag_context": [],
        "rag_confidence": 0.0,
        "requires_handoff": True,
        "handoff_reason": reason,
    }


async def rag_query_node(state: ConversationState) -> dict[str, Any]:
    """Recupera contexto del knowledge base y genera una respuesta fundamentada.

    Args:
        state: Estado del grafo; usa `client_id`, `conversation_id`, `message` y
            `model_to_use`.

    Returns:
        Dict parcial con el contexto recuperado, la confianza promedio y la
        respuesta generada; o con `requires_handoff` si no hubo contexto
        suficiente.
    """
    client_id = state["client_id"]
    tenant_id = UUID(client_id)
    mensaje = state.get("message") or {}
    consulta = (mensaje.get("text") or "").strip()

    if not consulta:
        logger.info("Mensaje sin texto en %s; RAG no tiene que buscar", client_id)
        return _handoff("insufficient_context")

    settings = await get_agent_settings(tenant_id)
    rag_service = build_rag_service()

    chunks = await rag_service.retrieve(
        query=consulta,
        client_id=tenant_id,
        top_k=settings.rag_top_k,
        threshold=settings.rag_threshold,
    )
    if not chunks:
        logger.info(
            "Sin contexto sobre el umbral %.2f para %s; se escala a humano",
            settings.rag_threshold,
            state.get("conversation_id"),
        )
        return _handoff("insufficient_context")

    confianza = sum(chunk.similarity for chunk in chunks) / len(chunks)

    few_shot = await rag_service.retrieve_few_shot_examples(
        query=consulta,
        client_id=tenant_id,
        top_k=FEW_SHOT_TOP_K,
        threshold=settings.few_shot_threshold,
    )

    prompt = rag_service.build_grounded_prompt(
        query=consulta,
        context_chunks=chunks,
        few_shot_examples=few_shot or None,
    )

    modelo = state.get("model_to_use") or settings.model or get_settings().OPENAI_CHAT_MODEL
    llm = get_chat_model(modelo, temperature=settings.temperature)

    mensajes: list[dict[str, str]] = []
    if settings.system_prompt:
        mensajes.append({"role": "system", "content": settings.system_prompt})
    mensajes.append({"role": "user", "content": prompt})

    respuesta = await llm.ainvoke(mensajes)
    texto = response_text(respuesta).strip()

    prompt_tokens, completion_tokens = extract_usage(respuesta)
    await TokenBudgetGuard.record_usage(
        client_id=client_id,
        conversation_id=state.get("conversation_id"),
        model=modelo,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        operation=OPERATION,
    )

    if not texto:
        logger.warning(
            "El modelo devolvio una respuesta vacia para %s; se escala a humano",
            state.get("conversation_id"),
        )
        return _handoff("insufficient_context")

    logger.info(
        "RAG resuelto para %s: %s chunks, confianza %.2f, %s few-shot",
        state.get("conversation_id"),
        len(chunks),
        confianza,
        len(few_shot),
    )
    return {
        "rag_context": [
            {
                "content": chunk.content,
                "similarity": chunk.similarity,
                "citation": chunk.citation,
            }
            for chunk in chunks
        ],
        "rag_confidence": confianza,
        "response_text": texto,
        "training_mode": settings.training_mode,
        "approved_examples": few_shot or None,
        "requires_handoff": False,
    }
