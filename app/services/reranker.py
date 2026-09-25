"""Re-ranking de resultados del RAG con un cross-encoder (Sprint 12, Dev B).

Que resuelve
-------------
La busqueda por embeddings compara la query y cada chunk por separado: dos
vectores que nunca se vieron. Un cross-encoder los lee juntos y puntua el par,
que es bastante mas preciso pero demasiado caro para correr sobre el corpus
entero. El pipeline estandar es traer 20 candidatos por embeddings y reordenar
esos 20 con el cross-encoder, devolviendo los 5 mejores.

Por que ONNX y no sentence-transformers
----------------------------------------
El spec (§6.1) usa `sentence_transformers.CrossEncoder`, que arrastra `torch`:
~2,5 GB de wheel, minutos de instalacion en CI y cientos de MB de RAM en cada
worker que lo cargue. Este repo no tiene **ninguna** dependencia de ML local —
hasta los embeddings se piden a la API de OpenAI — asi que meter torch por un
reranker cambia el perfil de despliegue del proyecto entero.

El mismo modelo (`cross-encoder/ms-marco-MiniLM-L-6-v2`) exportado a ONNX pesa
~90 MB y corre con `onnxruntime`, que son ~15 MB y no depende de torch. La
calidad es la del mismo modelo; lo que cambia es el runtime.

Degradacion, no fallo
----------------------
Si el modelo no esta en la imagen, si `onnxruntime` no esta instalado o si la
sesion no carga, se registra el motivo una sola vez y `rerank()` devuelve los
candidatos **en el orden que traian**. Una consulta del RAG no puede morir
porque falte un archivo de modelo: el peor caso es la calidad de retrieval que
ya teniamos antes de este sprint. Misma postura que `services/dian.py`.

El modelo se descarga en el `docker build`, nunca en runtime: un contenedor de
produccion no sale a internet a buscar pesos la primera vez que alguien
pregunta algo.

Bloqueo del event loop
-----------------------
`onnxruntime.InferenceSession.run()` es sincrono y tarda decenas de ms. Llamarlo
derecho desde una corrutina para el event loop del worker entero, no solo esa
consulta — la regla 4 de CLAUDE.md, y exactamente el hallazgo que la revision
del PR #40 le hizo a `socket.getaddrinfo()`. Va en un executor.
"""

import asyncio
import logging
import threading
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from app.core.config import get_settings

logger = logging.getLogger(__name__)

#: Largo maximo de tokens del par (query, chunk). Es el del modelo del spec.
MAX_LENGTH = 512

#: Candidatos que se traen por embeddings antes de reordenar.
DEFAULT_INITIAL_TOP_K = 20

#: Resultados que se devuelven despues de reordenar.
DEFAULT_FINAL_TOP_K = 5


class Puntuable(Protocol):
    """Lo unico que el reranker necesita de un resultado: su texto."""

    content: str


@dataclass(frozen=True)
class _Motor:
    """Sesion ONNX y tokenizer ya cargados.

    Attributes:
        sesion: `onnxruntime.InferenceSession` del cross-encoder.
        tokenizer: Tokenizer de `tokenizers` con truncado ya configurado.
    """

    sesion: Any
    tokenizer: Any


_candado = threading.Lock()


@lru_cache(maxsize=1)
def _cargar_motor() -> _Motor | None:
    """Carga el cross-encoder una sola vez por proceso.

    Cacheado con `lru_cache`: el modelo pesa ~90 MB y tarda cerca de un segundo
    en levantar, asi que se paga una vez y no por consulta.

    Returns:
        El motor listo, o `None` si no se pudo cargar por cualquier motivo
        (falta el archivo, falta `onnxruntime`, el modelo esta corrupto). En
        ese caso el reranking queda desactivado y el RAG sigue funcionando.
    """
    settings = get_settings()
    ruta = getattr(settings, "RERANKER_MODEL_PATH", "") or ""
    if not ruta:
        logger.info("Re-ranking desactivado: RERANKER_MODEL_PATH esta vacia")
        return None

    try:
        # Imports adentro a proposito: si `onnxruntime` no esta instalado, este
        # modulo tiene que seguir importandose y el RAG seguir andando.
        import onnxruntime
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_file(f"{ruta}/tokenizer.json")
        tokenizer.enable_truncation(max_length=MAX_LENGTH)
        tokenizer.enable_padding()

        sesion = onnxruntime.InferenceSession(
            f"{ruta}/model.onnx", providers=["CPUExecutionProvider"]
        )
    except Exception as exc:
        logger.warning(
            "Re-ranking desactivado: no se pudo cargar el cross-encoder de %s (%s). "
            "El RAG sigue devolviendo el orden de los embeddings.",
            ruta,
            exc,
        )
        return None

    logger.info("Cross-encoder de re-ranking cargado desde %s", ruta)
    return _Motor(sesion=sesion, tokenizer=tokenizer)


def _puntuar(motor: _Motor, query: str, textos: list[str]) -> list[float]:
    """Corre el cross-encoder sobre los pares (query, texto).

    Sincrona a proposito: la llama `rerank()` dentro de un executor.

    Args:
        motor: Motor ya cargado.
        query: Consulta del usuario.
        textos: Contenido de cada candidato.

    Returns:
        Un puntaje por candidato, en el mismo orden.
    """
    codificados = motor.tokenizer.encode_batch([(query, texto) for texto in textos])

    entradas = {
        "input_ids": [c.ids for c in codificados],
        "attention_mask": [c.attention_mask for c in codificados],
    }
    nombres = {e.name for e in motor.sesion.get_inputs()}
    if "token_type_ids" in nombres:
        entradas["token_type_ids"] = [c.type_ids for c in codificados]

    salida = motor.sesion.run(None, {k: v for k, v in entradas.items() if k in nombres})[0]
    # El modelo es de regresion: un solo logit por par. Segun como se exporte,
    # sale con forma (n, 1) o (n,).
    return [float(fila[0]) if hasattr(fila, "__len__") else float(fila) for fila in salida]


def disponible() -> bool:
    """Si el cross-encoder esta cargado y utilizable.

    Returns:
        `True` si hay motor; `False` si el reranking esta degradado.
    """
    return _cargar_motor() is not None


async def rerank(query: str, candidatos: list[Any], top_k: int = DEFAULT_FINAL_TOP_K) -> list[Any]:
    """Reordena los candidatos por relevancia real y devuelve los mejores.

    Si el cross-encoder no esta disponible, devuelve los primeros `top_k` en el
    orden en que venian: el orden de los embeddings, que es lo que el RAG hacia
    antes de este sprint.

    Args:
        query: Consulta del usuario.
        candidatos: Resultados del retrieval inicial; cada uno con `.content`.
        top_k: Cuantos devolver.

    Returns:
        Los `top_k` candidatos mas relevantes.
    """
    if not candidatos or len(candidatos) <= 1:
        return candidatos[:top_k]

    motor = _cargar_motor()
    if motor is None:
        return candidatos[:top_k]

    textos = [getattr(c, "content", "") or "" for c in candidatos]
    try:
        # El executor evita que la inferencia sincrona (decenas de ms) pare el
        # event loop del worker entero. El candado serializa las corridas: una
        # `InferenceSession` compartida entre hilos no da garantias, y de paso
        # evita que N consultas simultaneas peleen por los mismos nucleos.
        def _correr() -> list[float]:
            with _candado:
                return _puntuar(motor, query, textos)

        puntajes = await asyncio.get_running_loop().run_in_executor(None, _correr)
    except Exception:
        logger.exception("Fallo el re-ranking; se devuelve el orden de los embeddings")
        return candidatos[:top_k]

    ordenados = sorted(zip(candidatos, puntajes, strict=True), key=lambda par: par[1], reverse=True)
    return [candidato for candidato, _ in ordenados[:top_k]]


def config_del_tenant(config_agente: dict[str, Any] | None) -> tuple[bool, int]:
    """Lee la configuracion de re-ranking del tenant.

    Vive en `agent_configs.config.rag` — la columna se llama `config`, no
    `settings` como dice el spec (§6.2).

    El `final_top_k` del spec no se agrega como clave nueva: eso ya es
    `rag_top_k`, el knob que decide cuantos chunks entran en la respuesta.
    Tener dos settings para el mismo numero solo sirve para que un dia no
    coincidan.

    Args:
        config_agente: El JSONB `agent_configs.config` del tenant, o None.

    Returns:
        `(usar_reranking, initial_top_k)`, con los defaults del spec si el
        tenant no configuro nada.
    """
    rag = (config_agente or {}).get("rag", {})
    if not isinstance(rag, dict):
        return True, DEFAULT_INITIAL_TOP_K

    usar = rag.get("use_reranking", True)
    inicial = rag.get("initial_top_k", DEFAULT_INITIAL_TOP_K)

    # Un tenant puede escribir cualquier cosa en su JSONB; un valor raro
    # degrada al default en vez de tumbar la consulta.
    if not isinstance(inicial, int) or isinstance(inicial, bool) or inicial < 1:
        inicial = DEFAULT_INITIAL_TOP_K

    return bool(usar), inicial
