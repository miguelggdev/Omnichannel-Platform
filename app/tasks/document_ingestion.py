"""Worker de Celery que ejecuta la ingesta de documentos.

Cola: `documents` (concurrency=2 en docker-compose). El nombre de la tarea empieza
por `app.tasks.document_` para que el `task_routes` de `celery_config.py` la enrute
sola a esa cola.

Limites de tiempo: 10 min duros, 9 min blandos. Un PDF escaneado grande con OCR es
lento, y el limite blando deja margen para marcar el documento como fallido antes de
que Celery mate el proceso.

Reintentos: 2 con 30s de espera. Un fallo de red hacia Storage o hacia la API de
embeddings es tipicamente transitorio; agotados los intentos, el documento queda en
`failed` con el motivo en `metadata.error`, visible desde GET /api/v1/documents/{id}.

El pipeline en si (`app/services/document_pipeline.py`) es entrega de Dev A y se
importa de forma perezosa: asi el worker arranca y el endpoint encola aunque esa
pieza todavia no este en `main`.
"""

import asyncio
import logging
from typing import Any
from uuid import UUID

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded

from app.core.config import get_settings
from app.core.database import tenant_session
from app.models.document import Document

logger = logging.getLogger(__name__)

# Motivo que se guarda cuando el pipeline todavia no esta disponible.
PIPELINE_UNAVAILABLE = "Pipeline de ingesta no disponible (app/services/document_pipeline.py)"


class PipelineUnavailableError(RuntimeError):
    """El pipeline de ingesta no esta instalado todavia.

    No se reintenta: reintentar no va a hacer aparecer el modulo. El documento se
    marca como fallido y se recupera con POST /api/v1/documents/{id}/reprocess
    cuando la pieza este en `main`.
    """


async def _run_pipeline(document_id: UUID, client_id: UUID) -> None:
    """Construye el pipeline y procesa el documento.

    Args:
        document_id: Documento a procesar.
        client_id: Tenant propietario.

    Raises:
        PipelineUnavailableError: Si los servicios de Dev A no estan disponibles.
    """
    try:
        from app.services.chunker import DocumentChunker
        from app.services.document_pipeline import DocumentPipeline
        from app.services.embedding import EmbeddingService
        from app.services.ocr import OCRService
    except ImportError as exc:
        raise PipelineUnavailableError(f"{PIPELINE_UNAVAILABLE}: {exc}") from exc

    settings = get_settings()
    pipeline = DocumentPipeline(
        chunker=DocumentChunker(),
        embedder=EmbeddingService(
            api_key=settings.OPENAI_API_KEY,
            model=settings.OPENAI_EMBEDDING_MODEL,
        ),
        ocr=OCRService(),
    )
    await pipeline.process(document_id, client_id)


async def _mark_document_failed(document_id: UUID, client_id: UUID, error: str) -> None:
    """Marca el documento como fallido y deja el motivo en su metadata.

    El motivo es lo que ve el usuario en GET /api/v1/documents/{id}, asi que se
    recorta: un traceback completo ahi no aporta y puede filtrar detalles internos.

    Args:
        document_id: Documento afectado.
        client_id: Tenant propietario.
        error: Motivo del fallo.
    """
    try:
        async with tenant_session(client_id) as session:
            document = await session.get(Document, document_id)
            if document is None:
                logger.warning("Documento %s ya no existe; no se marca como fallido", document_id)
                return
            document.status = "failed"
            metadata: dict[str, Any] = dict(document.metadata_ or {})
            metadata["error"] = error[:500]
            # Reasignar en vez de mutar: SQLAlchemy no detecta cambios in-place en JSONB.
            document.metadata_ = metadata
    except Exception:
        # Si tampoco se puede escribir en base, el log es el ultimo rastro que queda.
        logger.exception("No se pudo marcar como fallido el documento %s", document_id)


@shared_task(
    name="app.tasks.document_ingest",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    acks_late=True,
    queue="documents",
    time_limit=600,
    soft_time_limit=540,
)
def ingest_document(self: Any, document_id: str, client_id: str) -> dict[str, str]:
    """Procesa un documento subido: extrae texto, lo chunkea y genera embeddings.

    Args:
        self: Instancia de la tarea (bind=True), para los reintentos.
        document_id: UUID del documento, serializado.
        client_id: UUID del tenant, serializado.

    Returns:
        `{"status": "completed"}`, `{"status": "failed"}` o `{"status": "timeout"}`.

    Raises:
        Retry: Reintento (2 como maximo, 30s de espera) ante fallos transitorios.
    """
    doc_uuid = UUID(document_id)
    client_uuid = UUID(client_id)

    try:
        asyncio.run(_run_pipeline(doc_uuid, client_uuid))

    except SoftTimeLimitExceeded:
        # No se reintenta: si no cupo en 9 minutos, otro intento tampoco va a caber.
        logger.error("Timeout procesando el documento %s", document_id)
        asyncio.run(_mark_document_failed(doc_uuid, client_uuid, "Timeout de procesamiento"))
        return {"status": "timeout"}

    except PipelineUnavailableError as exc:
        # Tampoco se reintenta: reintentar no instala el modulo que falta.
        logger.error("Documento %s sin procesar: %s", document_id, exc)
        asyncio.run(_mark_document_failed(doc_uuid, client_uuid, str(exc)))
        return {"status": "failed"}

    except Exception as exc:
        if self.request.retries < self.max_retries:
            logger.warning(
                "Fallo procesando el documento %s (intento %s/%s): %s",
                document_id,
                self.request.retries + 1,
                self.max_retries,
                exc,
            )
            raise self.retry(exc=exc) from exc

        logger.error(
            "Documento %s fallido tras %s intentos: %s",
            document_id,
            self.max_retries,
            exc,
            exc_info=True,
        )
        asyncio.run(_mark_document_failed(doc_uuid, client_uuid, str(exc)))
        return {"status": "failed"}

    return {"status": "completed"}
