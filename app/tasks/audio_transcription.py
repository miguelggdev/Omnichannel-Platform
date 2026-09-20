"""Transcripcion de notas de voz con la API de Whisper.

Cola: `media`. El nombre de la tarea empieza por `app.tasks.media_` para que el
`task_routes` de `celery_config.py` la enrute sola.

Por que una cola propia y no `documents`, que es la otra que hace I/O pesado
contra OpenAI: ahi viven la ingesta y el OCR de documentos, que tardan minutos y
corren con concurrencia 2. Detras de uno de esos, una nota de voz de tres
segundos esperaria su turno con el contacto mirando la pantalla (ADR-058).

Flujo:

1. `webhook_processor` guarda el mensaje de audio y, en vez de encolar la IA,
   encola esta tarea.
2. Se resuelve la URL de descarga del audio (en Telegram hay que pedirla con
   `getFile`; los demas canales la traen en el payload).
3. Se descarga con tope de tamano y se manda a Whisper.
4. Se actualiza `messages.content` con la transcripcion — `media_url` se
   conserva: el audio original sigue siendo la fuente.
5. Se encola la IA con el texto ya puesto en el `NormalizedMessage`, que es de
   donde lo leen los nodos del grafo (`state["message"]["text"]`).

Si la transcripcion falla definitivamente, la IA se encola igual con un texto
que dice que la nota de voz no se pudo transcribir: el contacto escribio y se
merece una respuesta, aunque sea para pedirle que lo repita por escrito.
"""

import logging
from typing import Any
from uuid import UUID

import httpx
from celery import shared_task
from openai import AsyncOpenAI
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import run_isolated, tenant_session
from app.models.message import Message
from app.schemas.message import MessageTypeEnum, NormalizedMessage

logger = logging.getLogger(__name__)

#: Backoff de los reintentos: 5s, 25s, 125s (igual que `webhook_processor`).
RETRY_BASE_DELAY_SECONDS = 5
RETRY_BACKOFF_FACTOR = 5

#: Texto con el que sigue el pipeline cuando la transcripcion no fue posible.
TEXTO_SIN_TRANSCRIBIR = "[el contacto envio una nota de voz que no se pudo transcribir]"

#: Timeout de la descarga del audio y de la llamada a Whisper.
_TIMEOUT_DESCARGA = 30.0
_TIMEOUT_WHISPER = 120.0

#: Nombre con el que se sube el audio a la API (Whisper lo usa para el formato).
_NOMBRE_ARCHIVO = "audio.ogg"
_MIME_POR_DEFECTO = "audio/ogg"


class AudioDemasiadoGrandeError(RuntimeError):
    """El audio supera `WHISPER_MAX_AUDIO_BYTES`.

    No es reintentable: descargarlo otra vez da el mismo tamano.
    """


async def resolver_url(client_id: UUID, channel: str, media_url: str) -> str:
    """Convierte el `media_url` del mensaje en una URL descargable.

    Telegram no manda URLs en el webhook: manda un `file_id` que hay que
    canjear con `getFile` (`telegram-file:<file_id>`, ver
    `app/services/messaging/telegram.py`). El resto de canales trae una URL
    directa.

    Args:
        client_id: Tenant dueno del mensaje.
        channel: Canal de origen.
        media_url: Valor guardado en el mensaje.

    Returns:
        URL de descarga. **En Telegram lleva el token del bot**: no se loguea ni
        se guarda en ningun lado.
    """
    from app.agents.nodes._tenant import get_channel_config
    from app.services.messaging.telegram import TelegramProvider

    file_id = TelegramProvider.file_id_de(media_url)
    if file_id is None:
        return media_url

    _provider_name, channel_config = get_channel_config(channel, client_id)
    return await TelegramProvider().get_file_url(file_id, channel_config)


async def descargar_audio(url: str, limite: int) -> bytes:
    """Descarga el audio sin pasarse del tope de tamano.

    Se lee en trozos y se corta en cuanto se supera el limite: mirar solo el
    `Content-Length` no sirve, porque un servidor puede no declararlo o mentir.

    Args:
        url: URL de descarga.
        limite: Maximo de bytes aceptados.

    Returns:
        El audio completo.

    Raises:
        AudioDemasiadoGrandeError: Si el audio supera `limite`.
        httpx.HTTPError: Si la descarga falla.
    """
    trozos: list[bytes] = []
    total = 0
    async with (
        httpx.AsyncClient(timeout=_TIMEOUT_DESCARGA, follow_redirects=True) as cliente,
        cliente.stream("GET", url) as respuesta,
    ):
        respuesta.raise_for_status()
        async for trozo in respuesta.aiter_bytes():
            total += len(trozo)
            if total > limite:
                raise AudioDemasiadoGrandeError(
                    f"El audio supera el maximo de {limite} bytes y no se transcribe"
                )
            trozos.append(trozo)
    return b"".join(trozos)


async def transcribir(audio: bytes) -> str:
    """Manda el audio a Whisper y devuelve el texto.

    Args:
        audio: Bytes del audio.

    Returns:
        La transcripcion, sin espacios al borde.

    Raises:
        RuntimeError: Si no hay `OPENAI_API_KEY` configurada.
    """
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("No hay OPENAI_API_KEY: no se puede transcribir")

    cliente = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, timeout=_TIMEOUT_WHISPER)
    # `response_format` se deja en el JSON simple a proposito: `verbose_json`
    # daria el idioma detectado, pero solo lo soporta `whisper-1` y el modelo es
    # configurable.
    extra: dict[str, Any] = {}
    if settings.WHISPER_LANGUAGE:
        extra["language"] = settings.WHISPER_LANGUAGE

    respuesta = await cliente.audio.transcriptions.create(
        model=settings.WHISPER_MODEL,
        file=(_NOMBRE_ARCHIVO, audio, _MIME_POR_DEFECTO),
        **extra,
    )
    return str(respuesta.text or "").strip()


async def guardar_transcripcion(
    client_id: UUID, conversation_id: UUID, external_message_id: str, texto: str
) -> None:
    """Escribe la transcripcion en el mensaje ya guardado.

    Args:
        client_id: Tenant dueno del mensaje.
        conversation_id: Conversacion a la que pertenece.
        external_message_id: Id del mensaje en el proveedor de origen.
        texto: Transcripcion.
    """
    settings = get_settings()
    async with tenant_session(client_id) as session:
        mensaje = (
            await session.execute(
                select(Message).where(
                    Message.client_id == client_id,
                    Message.conversation_id == conversation_id,
                    Message.external_message_id == external_message_id,
                )
            )
        ).scalar_one_or_none()
        if mensaje is None:
            logger.warning(
                "No se encontro el mensaje a transcribir en la conversacion %s", conversation_id
            )
            return

        mensaje.content = texto
        # Reasignacion completa y no mutacion in situ: SQLAlchemy no detecta los
        # cambios dentro de un JSONB mutado en sitio y el UPDATE no saldria.
        mensaje.metadata_ = {
            **(mensaje.metadata_ or {}),
            "original_type": "audio",
            "transcription_model": settings.WHISPER_MODEL,
            "transcription_language": settings.WHISPER_LANGUAGE or "auto",
        }


def _encolar_ia(
    client_id: str,
    conversation_id: str,
    contact_id: str,
    channel: str,
    message_data: dict[str, Any],
) -> None:
    """Encola el grafo de agentes con el mensaje ya transcrito.

    Mismo criterio que `webhook_processor._enqueue_ai_processing`: nada de lo que
    pase aqui propaga, porque el mensaje ya esta guardado y marcado como
    deduplicado; reintentar la tarea entera no lo volveria a encolar.

    Args:
        client_id: Tenant propietario.
        conversation_id: Conversacion a procesar.
        contact_id: Contacto que escribio.
        channel: Canal de origen.
        message_data: `NormalizedMessage` serializado, con `text` ya puesto.
    """
    try:
        from app.tasks.ai_processor import process_ai_response

        process_ai_response.delay(
            client_id=client_id,
            conversation_id=conversation_id,
            contact_id=contact_id,
            channel=channel,
            message_data=message_data,
        )
    except Exception:
        logger.critical(
            "No se pudo encolar la IA tras transcribir; el mensaje quedo sin respuesta "
            "automatica: conversation_id=%s",
            conversation_id,
            exc_info=True,
        )


async def _transcribir_mensaje(
    client_id: UUID, conversation_id: UUID, message_data: dict[str, Any]
) -> str:
    """Descarga, transcribe y guarda. Devuelve el texto obtenido.

    Args:
        client_id: Tenant propietario.
        conversation_id: Conversacion del mensaje.
        message_data: `NormalizedMessage` serializado del audio.

    Returns:
        La transcripcion.

    Raises:
        ValueError: Si el mensaje no trae `media_url` o Whisper devuelve vacio.
    """
    normalizado = NormalizedMessage(**message_data)
    if not normalizado.media_url:
        raise ValueError("El mensaje de audio no trae media_url")

    url = await resolver_url(client_id, normalizado.channel.value, normalizado.media_url)
    audio = await descargar_audio(url, get_settings().WHISPER_MAX_AUDIO_BYTES)
    texto = (await transcribir(audio)).strip()
    if not texto:
        raise ValueError("Whisper devolvio una transcripcion vacia")

    await guardar_transcripcion(client_id, conversation_id, normalizado.external_message_id, texto)
    return texto


@shared_task(
    name="app.tasks.media_transcribe_audio",
    bind=True,
    max_retries=3,
    acks_late=True,
    queue="media",
)
def transcribe_audio(
    self: Any,
    client_id: str,
    conversation_id: str,
    contact_id: str,
    channel: str,
    message_data: dict[str, Any],
) -> dict[str, str]:
    """Transcribe la nota de voz de un mensaje entrante y sigue el pipeline.

    Args:
        self: Instancia de la tarea (bind=True), para los reintentos.
        client_id: Tenant propietario.
        conversation_id: Conversacion del mensaje.
        contact_id: Contacto que escribio.
        channel: Canal de origen.
        message_data: `NormalizedMessage` serializado del audio.

    Returns:
        `{"status": "transcribed"}` o `{"status": "failed"}` si se agotaron los
        reintentos (en cuyo caso la IA se encola igual, con un aviso).

    Raises:
        Retry: Reintento con backoff exponencial (5s, 25s, 125s).
    """
    try:
        texto = run_isolated(
            _transcribir_mensaje(UUID(client_id), UUID(conversation_id), message_data)
        )
    except AudioDemasiadoGrandeError as exc:
        # Reintentar no cambia el tamano del archivo.
        logger.warning("Audio no transcrito: %s", exc)
        _encolar_ia(
            client_id,
            conversation_id,
            contact_id,
            channel,
            {**message_data, "text": TEXTO_SIN_TRANSCRIBIR},
        )
        return {"status": "failed"}
    except Exception as exc:
        if self.request.retries < self.max_retries:
            countdown = RETRY_BASE_DELAY_SECONDS * (RETRY_BACKOFF_FACTOR**self.request.retries)
            logger.warning(
                "Fallo transcribiendo audio (intento %s/%s, reintento en %ss): %s",
                self.request.retries + 1,
                self.max_retries,
                countdown,
                exc,
            )
            raise self.retry(exc=exc, countdown=countdown) from exc

        logger.error(
            "Audio sin transcribir tras %s intentos; se responde igual",
            self.max_retries,
            exc_info=True,
        )
        _encolar_ia(
            client_id,
            conversation_id,
            contact_id,
            channel,
            {**message_data, "text": TEXTO_SIN_TRANSCRIBIR},
        )
        return {"status": "failed"}

    _encolar_ia(
        client_id,
        conversation_id,
        contact_id,
        channel,
        {**message_data, "text": texto, "media_type": MessageTypeEnum.audio.value},
    )
    return {"status": "transcribed"}
