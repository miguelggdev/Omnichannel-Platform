"""Cliente de Supabase Storage para los archivos de la base de conocimiento.

Supabase Cloud provee el Storage (ADR-020), asi que se habla con su API REST via
httpx; no hay contenedor propio ni SDK sincrono de por medio.

Aislamiento por tenant
----------------------
Todos los objetos viven en un unico bucket privado (`SUPABASE_STORAGE_BUCKET`) y la
separacion es por prefijo de ruta: `{client_id}/{document_id}/{filename}`. El bucket
es privado, asi que el `file_url` que se guarda en `documents.file_url` es la **ruta
del objeto**, no una URL publica: para leerlo hay que pasar por
`download_from_storage()`, que autentica con la service key.

`build_object_path()` es el unico sitio donde se decide esa ruta. Si alguna vez se
pasa a un bucket por tenant, se cambia aqui.

Este modulo no esta asignado a ningun rol en la Matriz §6: lo necesitan tanto el
endpoint de subida (Dev B) como el pipeline de ingesta (Dev A), asi que vive fuera
de ambos.
"""

import logging
import re
from typing import Any
from uuid import UUID

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Timeout generoso: son archivos de hasta 50 MB contra un servicio externo.
STORAGE_TIMEOUT_SECONDS = 60.0

# Caracteres admitidos en el nombre de objeto. Todo lo demas se sustituye por "_"
# para no depender de como sanitiza Supabase ni arriesgar un path traversal.
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


class StorageError(RuntimeError):
    """Fallo hablando con Supabase Storage.

    Se traduce a AppException en el endpoint; nunca se propaga al cliente tal cual.
    """


def sanitize_filename(filename: str) -> str:
    """Normaliza el nombre de archivo para usarlo como parte de la ruta del objeto.

    Args:
        filename: Nombre original subido por el usuario.

    Returns:
        Nombre sin separadores de ruta ni caracteres raros. Nunca vacio.
    """
    # basename manual: el cliente puede mandar "..\\..\\etc\\passwd" o rutas POSIX.
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    limpio = _SAFE_FILENAME.sub("_", base).strip("._")
    return limpio[:200] or "archivo"


def build_object_path(client_id: UUID, document_id: UUID, filename: str) -> str:
    """Construye la ruta del objeto dentro del bucket.

    Args:
        client_id: Tenant propietario — es el prefijo que aisla sus archivos.
        document_id: Documento al que pertenece el archivo.
        filename: Nombre original del archivo.

    Returns:
        Ruta `{client_id}/{document_id}/{filename_sanitizado}`.
    """
    return f"{client_id}/{document_id}/{sanitize_filename(filename)}"


def _storage_endpoint(object_path: str) -> str:
    """Devuelve la URL del objeto en la API de Storage.

    Args:
        object_path: Ruta del objeto dentro del bucket.

    Returns:
        URL absoluta contra `SUPABASE_URL`.

    Raises:
        StorageError: Si `SUPABASE_URL` no esta configurada.
    """
    settings = get_settings()
    if not settings.SUPABASE_URL:
        raise StorageError("SUPABASE_URL no configurada")
    base = settings.SUPABASE_URL.rstrip("/")
    return f"{base}/storage/v1/object/{settings.SUPABASE_STORAGE_BUCKET}/{object_path}"


def _auth_headers() -> dict[str, str]:
    """Cabeceras de autenticacion contra Supabase Storage.

    Returns:
        Authorization con la service key. Nunca se loguea ni se devuelve al cliente.

    Raises:
        StorageError: Si `SUPABASE_SECRET_KEY` no esta configurada.
    """
    settings = get_settings()
    if not settings.SUPABASE_SECRET_KEY:
        raise StorageError("SUPABASE_SECRET_KEY no configurada")
    return {"Authorization": f"Bearer {settings.SUPABASE_SECRET_KEY}"}


async def upload_to_storage(
    object_path: str,
    content: bytes,
    content_type: str,
) -> str:
    """Sube un archivo al bucket del knowledge base.

    Args:
        object_path: Ruta destino, tal como la devuelve `build_object_path()`.
        content: Bytes del archivo.
        content_type: MIME type declarado por el cliente.

    Returns:
        La misma `object_path`, que es lo que se guarda en `documents.file_url`.

    Raises:
        StorageError: Si Supabase responde con error o no se puede contactar.
    """
    headers = {**_auth_headers(), "Content-Type": content_type}

    try:
        async with httpx.AsyncClient(timeout=STORAGE_TIMEOUT_SECONDS) as client:
            response = await client.post(
                _storage_endpoint(object_path), content=content, headers=headers
            )
    except httpx.HTTPError as exc:
        raise StorageError(f"No se pudo contactar con Storage: {exc}") from exc

    if response.status_code >= 400:
        # El cuerpo puede traer detalles del proveedor: se registra, no se expone.
        logger.error(
            "Storage rechazo la subida de %s: %s %s",
            object_path,
            response.status_code,
            response.text[:500],
        )
        raise StorageError(f"Storage devolvio {response.status_code} al subir el archivo")

    return object_path


async def download_from_storage(object_path: str) -> bytes:
    """Descarga un archivo del bucket.

    La usa el pipeline de ingesta (Dev A) para recuperar el archivo en el worker.

    Args:
        object_path: Ruta del objeto, tal como se guardo en `documents.file_url`.

    Returns:
        Bytes del archivo.

    Raises:
        StorageError: Si el objeto no existe o Storage responde con error.
    """
    try:
        async with httpx.AsyncClient(timeout=STORAGE_TIMEOUT_SECONDS) as client:
            response = await client.get(_storage_endpoint(object_path), headers=_auth_headers())
    except httpx.HTTPError as exc:
        raise StorageError(f"No se pudo contactar con Storage: {exc}") from exc

    if response.status_code >= 400:
        logger.error("Storage rechazo la descarga de %s: %s", object_path, response.status_code)
        raise StorageError(f"Storage devolvio {response.status_code} al descargar el archivo")

    return response.content


async def delete_from_storage(object_path: str) -> bool:
    """Borra un archivo del bucket.

    No levanta si el objeto ya no existe: borrar el registro de la base cuando el
    archivo ya se fue no es un error, y el endpoint de borrado no debe fallar por eso.

    Args:
        object_path: Ruta del objeto a borrar.

    Returns:
        True si Storage confirmo el borrado, False si no se pudo (queda en el log).
    """
    try:
        async with httpx.AsyncClient(timeout=STORAGE_TIMEOUT_SECONDS) as client:
            response = await client.delete(_storage_endpoint(object_path), headers=_auth_headers())
    except (httpx.HTTPError, StorageError) as exc:
        logger.warning("No se pudo borrar %s de Storage: %s", object_path, exc)
        return False

    if response.status_code >= 400:
        logger.warning("Storage devolvio %s al borrar %s", response.status_code, object_path)
        return False

    return True


def storage_metadata(object_path: str, content_type: str, size: int) -> dict[str, Any]:
    """Metadatos del archivo para guardar en `documents.metadata`.

    Args:
        object_path: Ruta del objeto en el bucket.
        content_type: MIME type original.
        size: Tamano en bytes.

    Returns:
        Dict serializable a JSONB.
    """
    return {
        "storage_bucket": get_settings().SUPABASE_STORAGE_BUCKET,
        "storage_path": object_path,
        "content_type": content_type,
        "size_bytes": size,
    }
