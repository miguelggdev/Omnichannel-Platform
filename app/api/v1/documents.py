"""CRUD de documentos de la base de conocimiento del tenant.

POST   /api/v1/documents                   sube un archivo y encola su ingesta
GET    /api/v1/documents                   lista paginada, filtrable por status
GET    /api/v1/documents/{id}              detalle con conteo de chunks
DELETE /api/v1/documents/{id}              borra documento, chunks y archivo
POST   /api/v1/documents/{id}/reprocess    reencola la ingesta

Manejo de la transaccion
------------------------
Estos endpoints abren `tenant_session()` a mano en vez de usar la dependency
`get_tenant_session`. El motivo es el orden respecto a Celery: la ingesta solo se
puede encolar **despues** del commit. Con la dependency, la transaccion se cierra
cuando FastAPI limpia las dependencias, o sea despues de que el endpoint retorna:
el worker podria abrir su conexion y no encontrar el documento todavia. Manejando
el bloque aqui, el `delay()` ocurre con la fila ya visible para el worker.

Lo mismo aplica al borrado del archivo en Storage: primero se confirma el borrado
en base y luego se toca Storage, para no quedarnos sin archivo si la transaccion
termina revirtiendose.
"""

import logging
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import NOT_FOUND, VALIDATION_ERROR, AppException
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.schemas.document import DocumentListResponse, DocumentResponse
from app.services.storage import (
    StorageError,
    build_object_path,
    delete_from_storage,
    storage_metadata,
    upload_to_storage,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# MIME types aceptados -> extension normalizada que se guarda en `file_type`.
ALLOWED_TYPES: dict[str, str] = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/plain": "txt",
    "text/csv": "csv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

# Estados validos del documento a lo largo del pipeline de ingesta.
VALID_STATUSES = ("pending", "processing", "completed", "failed")

STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"


async def _get_document_or_404(session: AsyncSession, document_id: UUID) -> Document:
    """Obtiene un documento del tenant activo o levanta 404.

    La sesion ya trae el contexto de tenant aplicado, asi que RLS garantiza que
    un documento de otro tenant se comporte igual que uno inexistente.

    Args:
        session: Sesion con contexto de tenant.
        document_id: Documento buscado.

    Returns:
        El documento.

    Raises:
        AppException: 404 si no existe para este tenant.
    """
    document = await session.get(Document, document_id)
    if document is None:
        raise AppException(
            status_code=404,
            error_code=NOT_FOUND,
            message="Documento no encontrado",
        )
    return document


@router.post("", status_code=201, response_model=DocumentResponse)
async def upload_document(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    user: dict[str, Any] = Depends(require_role("super_admin", "admin", "supervisor")),
) -> DocumentResponse:
    """Sube un documento al knowledge base del tenant y encola su ingesta.

    Args:
        file: Archivo subido (multipart/form-data).
        title: Titulo opcional. Por defecto, el nombre del archivo.
        user: Usuario autenticado; aporta el tenant y queda en metadata.

    Returns:
        El documento creado con status `pending`.

    Raises:
        AppException: 400 si el tipo no esta soportado o excede el tamano maximo;
            503 si Storage no esta disponible.
    """
    if file.content_type not in ALLOWED_TYPES:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message=f"Tipo de archivo no soportado: {file.content_type}",
        )

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message=f"El archivo excede el limite de {MAX_FILE_SIZE // (1024 * 1024)} MB",
        )
    if not contents:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message="El archivo esta vacio",
        )

    client_id: UUID = user["client_id"]
    # El id se genera aqui y no tras el INSERT: la ruta en Storage lo necesita, y
    # asi el archivo y la fila comparten identificador aunque el INSERT falle.
    document_id = uuid4()
    object_path = build_object_path(client_id, document_id, file.filename or "archivo")

    try:
        await upload_to_storage(object_path, contents, file.content_type)
    except StorageError as exc:
        logger.error("Fallo subiendo %s a Storage: %s", object_path, exc)
        raise AppException(
            status_code=503,
            error_code=STORAGE_UNAVAILABLE,
            message="No se pudo almacenar el archivo, reintentar",
        ) from exc

    async with tenant_session(client_id) as session:
        document = Document(
            id=document_id,
            client_id=client_id,
            title=title or file.filename or "Sin titulo",
            file_url=object_path,
            file_type=ALLOWED_TYPES[file.content_type],
            file_size=len(contents),
            status="pending",
            metadata_={
                **storage_metadata(object_path, file.content_type, len(contents)),
                "uploaded_by": str(user["user_id"]),
            },
        )
        session.add(document)
        await session.flush()
        # created_at y updated_at son server_default: sin refresh llegan a None y
        # DocumentResponse falla. Y en async hay que pedirlos explicitamente, no
        # por carga perezosa al acceder al atributo.
        await session.refresh(document)
        respuesta = DocumentResponse.model_validate(document)

    # Fuera de la transaccion: el worker abre su propia conexion y necesita ver la fila.
    await _enqueue_ingestion(document_id, client_id)

    logger.info("Documento %s subido por tenant %s", document_id, client_id)
    return respuesta


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_role("super_admin", "admin", "supervisor", "agent")),
) -> DocumentListResponse:
    """Lista los documentos del tenant, paginados y opcionalmente filtrados.

    Args:
        status: Filtro por estado (pending, processing, completed, failed).
        page: Pagina, empezando en 1.
        page_size: Tamano de pagina, maximo 100.
        user: Usuario autenticado.

    Returns:
        Pagina de documentos y total de coincidencias.

    Raises:
        AppException: 400 si el status no es uno de los validos.
    """
    if status is not None and status not in VALID_STATUSES:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message=f"Status invalido: {status}. Validos: {', '.join(VALID_STATUSES)}",
        )

    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        filtros = [Document.client_id == client_id]
        if status is not None:
            filtros.append(Document.status == status)

        total = await session.scalar(select(func.count()).select_from(Document).where(*filtros))

        stmt = (
            select(Document)
            .where(*filtros)
            .order_by(Document.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        documentos = (await session.execute(stmt)).scalars().all()

        return DocumentListResponse(
            items=[DocumentResponse.model_validate(d) for d in documentos],
            total=int(total or 0),
            page=page,
            page_size=page_size,
        )


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: UUID,
    user: dict[str, Any] = Depends(require_role("super_admin", "admin", "supervisor", "agent")),
) -> DocumentResponse:
    """Devuelve el detalle de un documento con su conteo real de chunks.

    `documents.chunk_count` lo escribe el pipeline al terminar; aqui se recuenta
    contra `document_chunks` para que el detalle no mienta si la ingesta quedo a
    medias.

    Args:
        document_id: Documento a consultar.
        user: Usuario autenticado.

    Returns:
        El documento, con `chunk_count` recontado.

    Raises:
        AppException: 404 si no existe para este tenant.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        document = await _get_document_or_404(session, document_id)

        chunks = await session.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(
                DocumentChunk.client_id == client_id,
                DocumentChunk.document_id == document_id,
            )
        )

        respuesta = DocumentResponse.model_validate(document)
        respuesta.chunk_count = int(chunks or 0)
        return respuesta


@router.delete("/{document_id}", status_code=200)
async def delete_document(
    document_id: UUID,
    user: dict[str, Any] = Depends(require_role("super_admin", "admin")),
) -> dict[str, str]:
    """Elimina un documento, sus chunks y su archivo en Storage.

    Los chunks se borran explicitamente: `document_chunks` no declara ON DELETE
    CASCADE, asi que sin esto el DELETE fallaria por la foreign key.

    Args:
        document_id: Documento a borrar.
        user: Usuario autenticado (solo admin).

    Returns:
        Confirmacion con el id borrado.

    Raises:
        AppException: 404 si no existe para este tenant.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        document = await _get_document_or_404(session, document_id)
        object_path = document.file_url

        await session.execute(
            sa_delete(DocumentChunk).where(
                DocumentChunk.client_id == client_id,
                DocumentChunk.document_id == document_id,
            )
        )
        await session.delete(document)

    # Storage se toca despues del commit: si la transaccion se revirtiera, el
    # documento seguiria en base y nos habriamos quedado sin su archivo.
    if object_path and not await delete_from_storage(object_path):
        logger.warning(
            "Documento %s borrado en base pero su archivo sigue en Storage: %s",
            document_id,
            object_path,
        )

    logger.info("Documento %s borrado por tenant %s", document_id, client_id)
    return {"id": str(document_id), "status": "deleted"}


@router.post("/{document_id}/reprocess", status_code=202)
async def reprocess_document(
    document_id: UUID,
    user: dict[str, Any] = Depends(require_role("super_admin", "admin")),
) -> dict[str, str]:
    """Reencola la ingesta de un documento.

    Util cuando la ingesta fallo o cambio el modelo de embedding. Los chunks
    previos se borran para no mezclar embeddings de modelos distintos en la misma
    busqueda vectorial.

    Args:
        document_id: Documento a reprocesar.
        user: Usuario autenticado (solo admin).

    Returns:
        Confirmacion con el nuevo status.

    Raises:
        AppException: 404 si no existe; 400 si ya se esta procesando o no tiene
            archivo asociado.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        document = await _get_document_or_404(session, document_id)

        if document.status == "processing":
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message="El documento ya se esta procesando",
            )
        if not document.file_url:
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message="El documento no tiene archivo asociado en Storage",
            )

        await session.execute(
            sa_delete(DocumentChunk).where(
                DocumentChunk.client_id == client_id,
                DocumentChunk.document_id == document_id,
            )
        )
        document.status = "pending"
        document.chunk_count = 0

    await _enqueue_ingestion(document_id, client_id)

    logger.info("Documento %s reencolado para ingesta", document_id)
    return {"id": str(document_id), "status": "pending"}


async def _enqueue_ingestion(document_id: UUID, client_id: UUID) -> None:
    """Encola la ingesta del documento en la cola `documents`.

    `delay()` es I/O sincrono contra Redis: va a threadpool para no bloquear el
    event loop. El import del modulo de tareas es perezoso, asi los tests pueden
    sustituir la tarea por un doble.

    Un fallo al encolar no revierte nada: el documento ya esta guardado y en
    Storage, y queda en `pending`. La via de recuperacion es POST
    /documents/{id}/reprocess, que es justo para lo que existe.

    Args:
        document_id: Documento a procesar.
        client_id: Tenant propietario.
    """
    from app.tasks import document_ingestion

    try:
        await run_in_threadpool(
            document_ingestion.ingest_document.delay, str(document_id), str(client_id)
        )
    except Exception:
        logger.exception(
            "No se pudo encolar la ingesta de %s; queda en pending, reintentar con "
            "POST /documents/%s/reprocess",
            document_id,
            document_id,
        )
