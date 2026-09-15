"""Acceso al LLM de chat y lectura del consumo de tokens, compartido por los nodos.

`langchain_openai` se importa dentro de `get_chat_model()`, no al nivel del
modulo: asi los nodos se pueden importar (y testear) sin el paquete instalado, y
los tests sustituyen `get_chat_model` en el modulo del nodo que estan probando
sin tocar la API de OpenAI.

`extract_usage()` existe porque las respuestas de LangChain traen el consumo en
dos sitios distintos segun la version y segun si se pidio structured output:
`usage_metadata` (formato nuevo, normalizado) o `response_metadata.token_usage`
(el payload crudo de OpenAI). Si no viene ninguno, el consumo se registra como 0
en vez de estimarse: un numero inventado en `token_usage_logs` es peor que un
hueco visible.
"""

from typing import Any

from app.core.config import get_settings

# Timeout y reintentos del cliente de chat. La tarea de Celery tiene un limite
# blando de 100s (`specs/sprint-06-langgraph.md` §11) y el grafo puede encadenar
# dos llamadas (intent + rag), asi que ninguna puede colgarse mas de ~30s.
LLM_TIMEOUT_SECONDS = 30.0
LLM_MAX_RETRIES = 2


def get_chat_model(model: str, temperature: float = 0.0) -> Any:
    """Construye el cliente de chat de LangChain para un modelo dado.

    Args:
        model: Nombre del modelo de OpenAI (gpt-4o, gpt-4o-mini, ...).
        temperature: Temperatura de muestreo.

    Returns:
        Instancia de `ChatOpenAI` lista para `ainvoke()`.
    """
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model,
        temperature=temperature,
        api_key=get_settings().OPENAI_API_KEY,
        timeout=LLM_TIMEOUT_SECONDS,
        max_retries=LLM_MAX_RETRIES,
    )


def extract_usage(response: Any) -> tuple[int, int]:
    """Lee (prompt_tokens, completion_tokens) de una respuesta de LangChain.

    Args:
        response: `AIMessage` devuelto por `ainvoke()`.

    Returns:
        Tupla `(prompt_tokens, completion_tokens)`; `(0, 0)` si la respuesta no
        trae informacion de consumo.
    """
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict):
        return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))

    metadata = getattr(response, "response_metadata", None)
    if isinstance(metadata, dict):
        token_usage = metadata.get("token_usage") or {}
        if isinstance(token_usage, dict):
            return (
                int(token_usage.get("prompt_tokens", 0)),
                int(token_usage.get("completion_tokens", 0)),
            )

    return 0, 0


def response_text(response: Any) -> str:
    """Normaliza el contenido de una respuesta del LLM a texto plano.

    `AIMessage.content` es `str` en las respuestas normales, pero puede ser una
    lista de bloques cuando el modelo devuelve contenido estructurado.

    Args:
        response: `AIMessage` devuelto por `ainvoke()`.

    Returns:
        Texto de la respuesta, vacio si no hay contenido textual.
    """
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        partes = [
            bloque.get("text", "")
            for bloque in content
            if isinstance(bloque, dict) and bloque.get("type") == "text"
        ]
        return "".join(partes)
    return str(content)
