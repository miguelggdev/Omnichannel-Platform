"""Transcripcion de audios entrantes con la API de Whisper de OpenAI.

Dos piezas, ambas sin estado y sin acceso a la base de datos:

- `download_media()` baja el audio desde la URL que entrego el canal. Esa URL la
  controla un tercero (el proveedor, o quien haya falsificado un webhook), asi que
  se trata como entrada no confiable: solo `https`, solo hosts que resuelven a IPs
  publicas (nada de `169.254.169.254` ni de la red interna), redirecciones
  revalidadas salto a salto y un tope de bytes que se aplica mientras se descarga,
  no despues.
- `transcribe_audio()` llama a Whisper y devuelve el texto y la duracion.

Los errores se clasifican en dos: `TranscriptionError` (transitorio, reintentar
puede arreglarlo) y `PermanentTranscriptionError` (reintentar no lo arregla: audio
demasiado grande, formato no soportado, credencial invalida, audio sin voz). La
tarea de Celery decide con esa distincion si reintenta o escala a un humano.

Limitacion conocida: la IP se valida al resolver el host y `httpx` vuelve a
resolverlo al conectar, asi que un DNS que cambie entre las dos consultas (DNS
rebinding) no queda cubierto. Los proveedores sirven los medios desde CDNs
conocidos y la superficie es la de un webhook ya autenticado; cerrarlo del todo
exige fijar la IP en el transporte de httpx.
"""

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import openai
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

#: Redirecciones que se siguen como maximo al bajar un medio.
MAX_REDIRECTS = 3

_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})

#: Extensiones que acepta la API de Whisper.
_WHISPER_EXTENSIONS = frozenset(
    {"flac", "m4a", "mp3", "mp4", "mpeg", "mpga", "oga", "ogg", "wav", "webm"}
)

#: Content-Type -> extension. Whisper decide el formato por la extension del
#: nombre del archivo, asi que hay que dar una valida aunque la URL no la traiga.
_MIME_EXTENSIONS: dict[str, str] = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "audio/flac": "flac",
    "video/mp4": "mp4",
    "video/webm": "webm",
}


class TranscriptionError(RuntimeError):
    """Fallo transitorio de la transcripcion: reintentar puede arreglarlo."""


class PermanentTranscriptionError(TranscriptionError):
    """Fallo que reintentar no arregla; la conversacion debe escalarse."""


@dataclass(frozen=True)
class DownloadedMedia:
    """Medio descargado.

    Attributes:
        content: Bytes del archivo.
        content_type: Content-Type que declaro el servidor, sin parametros.
        url_path: Ruta de la URL final, para inferir la extension.
    """

    content: bytes
    content_type: str
    url_path: str


@dataclass(frozen=True)
class Transcription:
    """Resultado de una transcripcion.

    Attributes:
        text: Texto transcrito, sin espacios en los extremos.
        duration_seconds: Duracion del audio segun Whisper (0.0 si no la informo).
    """

    text: str
    duration_seconds: float


async def _resolver_host(host: str, port: int) -> list[str]:
    """Resuelve un host a sus direcciones IP.

    Es un punto de sustitucion para los tests: resolver DNS real no es
    determinista ni deseable en una suite unitaria.

    Args:
        host: Nombre de host o IP literal.
        port: Puerto (solo para `getaddrinfo`).

    Returns:
        Direcciones IP, como texto.

    Raises:
        TranscriptionError: Si el host no resuelve.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise TranscriptionError(f"No se pudo resolver el host {host!r}") from exc
    return [str(info[4][0]) for info in infos]


async def _validar_url(url: str) -> None:
    """Rechaza una URL que no sea https o que apunte a una red no publica.

    Args:
        url: URL a validar.

    Raises:
        PermanentTranscriptionError: Si el esquema no es https, no hay host, o
            alguna de las IPs a las que resuelve no es publica.
        TranscriptionError: Si el host no resuelve (puede ser transitorio).
    """
    partes = urlsplit(url)
    if partes.scheme != "https" or not partes.hostname:
        raise PermanentTranscriptionError("La URL del medio debe ser https")

    puerto = partes.port or 443
    direcciones = await _resolver_host(partes.hostname, puerto)
    if not direcciones:
        raise TranscriptionError(f"El host {partes.hostname!r} no devolvio direcciones")
    for direccion in direcciones:
        # Se exige que TODAS sean publicas: si una resuelve a la red interna, el
        # cliente podria terminar conectando a ella.
        if not ipaddress.ip_address(direccion.split("%")[0]).is_global:
            raise PermanentTranscriptionError("La URL del medio apunta a una red no publica")


async def download_media(
    url: str,
    *,
    max_bytes: int,
    timeout_s: float = 30.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> DownloadedMedia:
    """Descarga un medio con las defensas de una URL no confiable.

    Args:
        url: URL del medio.
        max_bytes: Tamano maximo aceptado. Se comprueba contra `Content-Length` y
            de nuevo mientras se lee el cuerpo.
        timeout_s: Timeout por operacion de red, en segundos.
        transport: Transporte de httpx (para los tests).

    Returns:
        El medio descargado.

    Raises:
        PermanentTranscriptionError: URL no permitida, respuesta 4xx (salvo 429),
            demasiadas redirecciones o archivo por encima de `max_bytes`.
        TranscriptionError: Fallo transitorio (red, 5xx, 429).
    """
    actual = url
    async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as cliente:
        for _ in range(MAX_REDIRECTS + 1):
            await _validar_url(actual)
            try:
                async with cliente.stream("GET", actual, follow_redirects=False) as respuesta:
                    if respuesta.status_code in _REDIRECT_STATUS:
                        destino = respuesta.headers.get("location")
                        if not destino:
                            raise PermanentTranscriptionError("Redireccion sin `Location`")
                        actual = urljoin(actual, destino)
                        continue

                    if respuesta.status_code >= 400:
                        # Sin `raise_for_status`: su mensaje incluye la URL, que en
                        # Telegram lleva el token del bot.
                        error = (
                            TranscriptionError
                            if respuesta.status_code >= 500 or respuesta.status_code == 429
                            else PermanentTranscriptionError
                        )
                        raise error(f"El medio respondio HTTP {respuesta.status_code}")

                    declarado = respuesta.headers.get("content-length", "")
                    if declarado.isdigit() and int(declarado) > max_bytes:
                        raise PermanentTranscriptionError(
                            f"El audio pesa {declarado} bytes; el maximo es {max_bytes}"
                        )

                    cuerpo = bytearray()
                    async for trozo in respuesta.aiter_bytes():
                        cuerpo.extend(trozo)
                        if len(cuerpo) > max_bytes:
                            raise PermanentTranscriptionError(
                                f"El audio supera el maximo de {max_bytes} bytes"
                            )

                    tipo = respuesta.headers.get("content-type", "").split(";")[0].strip().lower()
                    return DownloadedMedia(
                        content=bytes(cuerpo),
                        content_type=tipo,
                        url_path=urlsplit(actual).path,
                    )
            except httpx.HTTPError as exc:
                # Se descarta el mensaje de httpx (puede traer la URL) y se deja
                # solo el tipo de error.
                raise TranscriptionError(
                    f"Fallo de red al bajar el medio: {type(exc).__name__}"
                ) from None

    raise PermanentTranscriptionError(f"Mas de {MAX_REDIRECTS} redirecciones al bajar el medio")


def inferir_nombre_audio(content_type: str, url_path: str) -> str:
    """Elige un nombre de archivo con una extension que Whisper acepte.

    Args:
        content_type: Content-Type del medio, sin parametros.
        url_path: Ruta de la URL de descarga.

    Returns:
        Nombre del estilo `audio.ogg`.

    Raises:
        PermanentTranscriptionError: Si ni el Content-Type ni la URL indican un
            formato que Whisper soporte (p. ej. `audio/amr`).
    """
    extension = _MIME_EXTENSIONS.get(content_type)
    if extension is None:
        candidata = url_path.rsplit(".", 1)[-1].lower() if "." in url_path else ""
        if candidata in _WHISPER_EXTENSIONS:
            extension = candidata
    if extension is None:
        raise PermanentTranscriptionError(
            f"Formato de audio no soportado por Whisper: {content_type or 'desconocido'}"
        )
    return f"audio.{extension}"


async def transcribe_audio(
    audio: bytes,
    filename: str,
    *,
    api_key: str,
    model: str,
    language: str | None = None,
    timeout_s: float = 60.0,
) -> Transcription:
    """Transcribe un audio con Whisper.

    Args:
        audio: Bytes del audio.
        filename: Nombre con extension valida (`inferir_nombre_audio`).
        api_key: `OPENAI_API_KEY`.
        model: Modelo de transcripcion (`whisper-1`).
        language: Codigo ISO-639-1 si se quiere forzar; `None` deja que Whisper
            detecte el idioma (la plataforma atiende 6).
        timeout_s: Timeout de la llamada, en segundos.

    Returns:
        Texto y duracion.

    Raises:
        PermanentTranscriptionError: Sin credencial, credencial rechazada, audio
            que Whisper no puede leer o audio sin voz.
        TranscriptionError: Fallo transitorio (limite de tasa, red, 5xx).
    """
    if not api_key:
        raise PermanentTranscriptionError("OPENAI_API_KEY no configurada")

    # Solo se manda `language` si se fijo: un valor vacio no es "autodetectar".
    opciones: dict[str, Any] = {"language": language} if language else {}

    # `max_retries=0`: los reintentos los decide la tarea de Celery, que ademas
    # sabe escalar a un humano cuando se agotan.
    async with AsyncOpenAI(api_key=api_key, timeout=timeout_s, max_retries=0) as cliente:
        try:
            respuesta = await cliente.audio.transcriptions.create(
                model=model,
                file=(filename, audio),
                response_format="verbose_json",
                **opciones,
            )
        except (
            openai.AuthenticationError,
            openai.PermissionDeniedError,
            openai.BadRequestError,
            openai.UnprocessableEntityError,
        ) as exc:
            raise PermanentTranscriptionError(f"Whisper rechazo el audio: {exc.message}") from exc
        except openai.APIError as exc:
            raise TranscriptionError(f"Fallo de Whisper: {type(exc).__name__}") from exc

    texto = (getattr(respuesta, "text", "") or "").strip()
    if not texto:
        raise PermanentTranscriptionError("Whisper no reconocio voz en el audio")
    duracion = getattr(respuesta, "duration", None)
    return Transcription(text=texto, duration_seconds=float(duracion or 0.0))
