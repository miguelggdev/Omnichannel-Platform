"""Generacion de embeddings vectoriales via la API de OpenAI.

Contrato: `specs/sprint-05-rag.md` §5. Verificado por
`tests/unit/test_rag.py::TestEmbeddings`.
"""

import asyncio

from openai import AsyncOpenAI

# Dimensiones por modelo de embedding de OpenAI. `document_chunks.embedding` y
# `approved_responses.embedding` son Vector(1536) (Sprint 1): un modelo con otra
# dimension no entra en esa columna sin migrar el schema.
_MODEL_DIMENSIONS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}
_DEFAULT_DIMENSIONS = 1536

# Batches de 100: OpenAI acepta hasta 2048 inputs por request, pero un batch mas
# chico evita timeouts y deja margen frente al rate limit.
_BATCH_PAUSE_SECONDS = 0.5


class EmbeddingService:
    """Genera embeddings de texto con la API de OpenAI.

    Attributes:
        model: Modelo de embedding configurado.
        dimensions: Dimensiones del vector que produce `model`.
    """

    def __init__(self, api_key: str, model: str = "text-embedding-3-small") -> None:
        """Inicializa el cliente de OpenAI para embeddings.

        Args:
            api_key: `OPENAI_API_KEY` del tenant o global.
            model: Modelo de embedding a usar.
        """
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.dimensions = _MODEL_DIMENSIONS.get(model, _DEFAULT_DIMENSIONS)

    async def embed_single(self, text: str) -> list[float]:
        """Genera el embedding de un solo texto.

        Args:
            text: Texto a vectorizar.

        Returns:
            Vector de `self.dimensions` componentes.
        """
        response = await self.client.embeddings.create(model=self.model, input=text)
        return list(response.data[0].embedding)

    async def embed_batch(self, texts: list[str], batch_size: int = 100) -> list[list[float]]:
        """Genera embeddings para varios textos, en lotes.

        Args:
            texts: Textos a vectorizar, en el orden en que se quieren de vuelta.
            batch_size: Tamano de cada lote enviado a la API.

        Returns:
            Un vector por texto, en el mismo orden que `texts`. Lista vacia si
            `texts` esta vacio (no se llama a la API sin nada que embeber).
        """
        if not texts:
            return []

        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            response = await self.client.embeddings.create(model=self.model, input=batch)
            all_embeddings.extend(item.embedding for item in response.data)

            if i + batch_size < len(texts):
                await asyncio.sleep(_BATCH_PAUSE_SECONDS)

        return all_embeddings
