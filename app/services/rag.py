"""Retrieval-Augmented Generation con grounding estricto y citacion de fuentes.

Contrato: `specs/sprint-05-rag.md` §6. Verificado por
`tests/unit/test_rag.py::TestFiltroPreVectorial`, `TestBug001`, `TestParametros`
y `TestCitaciones`.

Dos reglas absolutas de CLAUDE.md se aplican aqui:

- **Regla 2 (filtro pre-vectorial):** el aislamiento por `client_id` va en el
  WHERE, antes del calculo de distancia — nunca como filtro post-ranking.
  Se resuelve via `current_setting('app.current_client_id')`, que ya trae el
  contexto que puso `tenant_session()`, no un parametro que el llamante pueda
  manipular.
- **BUG-001:** PostgreSQL no admite el alias de SELECT (`similarity`) dentro del
  propio WHERE. El threshold repite la expresion completa
  `1 - (embedding <=> (:query_embedding)::vector)`.

`tenant_session` se importa al nivel de modulo (no dentro de los metodos) para
que los tests puedan sustituirlo con `monkeypatch.setattr(rag, "tenant_session",
...)` sin tocar una sesion real.

Cast `::vector` obligatorio en cada `:query_embedding`: el parametro llega como
`str` (bindeado como `text` por asyncpg) y `vector <=> text` no tiene cast
implicito, asi que sin el cast la query falla en Postgres real. Ningun test
unitario lo detecta porque `tests/unit/test_rag.py` sustituye la sesion por un
espia que solo inspecciona el SQL emitido, sin ejecutarlo — la cobertura contra
Postgres real vive en `tests/integration/test_document_pipeline.py`.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text as sql_text

from app.core.database import tenant_session
from app.services.embedding import EmbeddingService

# Defaults documentados en la spec: RAG general es mas permisivo (0.75) que los
# few-shot examples (0.80), que deben ser casi identicos a la pregunta para
# inyectarse como ejemplo.
DEFAULT_TOP_K = 5
DEFAULT_THRESHOLD = 0.75
FEW_SHOT_TOP_K = 3
FEW_SHOT_THRESHOLD = 0.80


@dataclass
class RetrievalResult:
    """Un chunk recuperado, listo para citarse en una respuesta.

    Attributes:
        chunk_id: Id del chunk en `document_chunks`.
        content: Texto del chunk.
        similarity: Similaridad coseno con la query (0.0 - 1.0).
        metadata: Metadata del chunk (pagina, seccion, archivo de origen).
        citation: Cita formateada `[Fuente: titulo, pag. X]`.
    """

    chunk_id: UUID
    content: str
    similarity: float
    metadata: dict[str, Any]
    citation: str


class RAGService:
    """Recupera contexto relevante para grounding estricto de respuestas.

    Attributes:
        embedding_service: Servicio usado para vectorizar las queries.
    """

    def __init__(self, embedding_service: EmbeddingService) -> None:
        """Inicializa el servicio de retrieval.

        Args:
            embedding_service: Servicio de embeddings a usar para las queries.
        """
        self.embedding_service = embedding_service

    async def retrieve(
        self,
        query: str,
        client_id: UUID,
        top_k: int = DEFAULT_TOP_K,
        threshold: float = DEFAULT_THRESHOLD,
        document_ids: list[UUID] | None = None,
    ) -> list[RetrievalResult]:
        """Recupera los chunks mas relevantes para una query.

        Args:
            query: Texto de busqueda.
            client_id: Tenant activo — abre el contexto de `SET LOCAL`/`set_config`
                del que sale el filtro de aislamiento.
            top_k: Maximo de resultados a devolver.
            threshold: Similaridad minima (0.0 - 1.0) para considerar un chunk.
            document_ids: Si se da, acota la busqueda a esos documentos.

        Returns:
            Chunks ordenados por similaridad descendente, con su citacion. Lista
            vacia si nada supera el threshold (no se inventa contexto).
        """
        query_embedding = await self.embedding_service.embed_single(query)

        sql = """
            SELECT
                dc.id,
                dc.content,
                dc.metadata,
                d.title AS document_title,
                1 - (dc.embedding <=> (:query_embedding)::vector) AS similarity
            FROM document_chunks dc
            JOIN documents d ON dc.document_id = d.id
            WHERE dc.client_id = current_setting('app.current_client_id')::uuid
              AND d.status = 'completed'
              AND 1 - (dc.embedding <=> (:query_embedding)::vector) > :threshold
        """
        params: dict[str, Any] = {
            "query_embedding": str(query_embedding),
            "threshold": threshold,
            "top_k": top_k,
        }

        if document_ids:
            sql += " AND dc.document_id = ANY((:document_ids)::uuid[])"
            params["document_ids"] = [str(doc_id) for doc_id in document_ids]

        sql += " ORDER BY dc.embedding <=> (:query_embedding)::vector LIMIT :top_k"

        async with tenant_session(client_id) as session:
            rows = (await session.execute(sql_text(sql), params)).fetchall()

        results = []
        for row in rows:
            metadata = row.metadata or {}
            page_number = metadata.get("page_number") or "?"
            results.append(
                RetrievalResult(
                    chunk_id=row.id,
                    content=row.content,
                    similarity=row.similarity,
                    metadata=metadata,
                    citation=f"[Fuente: {row.document_title}, pag. {page_number}]",
                )
            )
        return results

    async def retrieve_few_shot_examples(
        self,
        query: str,
        client_id: UUID,
        top_k: int = FEW_SHOT_TOP_K,
        threshold: float = FEW_SHOT_THRESHOLD,
    ) -> list[dict[str, Any]]:
        """Recupera ejemplos few-shot de `approved_responses` para la query.

        Usa un threshold mas estricto que `retrieve()`: un ejemplo mal elegido
        contamina el estilo de la respuesta mas de lo que un chunk de contexto
        de mas la degrada.

        Args:
            query: Texto de busqueda.
            client_id: Tenant activo.
            top_k: Maximo de ejemplos a devolver.
            threshold: Similaridad minima para considerar un ejemplo.

        Returns:
            Lista de `{"question", "answer", "similarity"}`, ordenada por
            similaridad descendente.
        """
        query_embedding = await self.embedding_service.embed_single(query)

        sql = """
            SELECT
                question,
                response,
                1 - (embedding <=> (:query_embedding)::vector) AS similarity
            FROM approved_responses
            WHERE client_id = current_setting('app.current_client_id')::uuid
              AND 1 - (embedding <=> (:query_embedding)::vector) > :threshold
            ORDER BY embedding <=> (:query_embedding)::vector
            LIMIT :top_k
        """
        params = {
            "query_embedding": str(query_embedding),
            "threshold": threshold,
            "top_k": top_k,
        }

        async with tenant_session(client_id) as session:
            rows = (await session.execute(sql_text(sql), params)).fetchall()

        return [
            {"question": row.question, "answer": row.response, "similarity": row.similarity}
            for row in rows
        ]

    def build_grounded_prompt(
        self,
        query: str,
        context_chunks: list[RetrievalResult],
        few_shot_examples: list[dict[str, Any]] | None = None,
    ) -> str:
        """Arma el prompt con grounding estricto: solo el contexto dado, con citas.

        Args:
            query: Pregunta del usuario.
            context_chunks: Chunks recuperados por `retrieve()`.
            few_shot_examples: Ejemplos de `retrieve_few_shot_examples()`, si hay.

        Returns:
            Prompt listo para el LLM, con instrucciones de grounding, ejemplos
            (si los hay), contexto citado y la pregunta.
        """
        partes = [
            "Eres un asistente que responde UNICAMENTE basado en el contexto proporcionado.",
            "Si la informacion no esta en el contexto, indica que no tienes suficiente informacion.",
            "Incluye las citaciones de fuente en tu respuesta.",
            "",
        ]

        if few_shot_examples:
            partes.append("=== EJEMPLOS DE RESPUESTAS APROBADAS ===")
            for ejemplo in few_shot_examples:
                partes.append(f"Pregunta: {ejemplo['question']}")
                partes.append(f"Respuesta: {ejemplo['answer']}")
                partes.append("")

        partes.append("=== CONTEXTO ===")
        for chunk in context_chunks:
            partes.append(chunk.citation)
            partes.append(chunk.content)
            partes.append("")

        partes.append("=== PREGUNTA DEL USUARIO ===")
        partes.append(query)

        return "\n".join(partes)
