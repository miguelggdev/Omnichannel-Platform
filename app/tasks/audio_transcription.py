"""Worker de Celery que transcribe los audios entrantes antes de pasarlos a la IA.

Cola: `media` (el nombre empieza por `app.tasks.media_`, asi que el
`task_routes` de `celery_config.py` la enruta sola; worker `celery-media`).
Va aparte de `ai_inference` porque una transcripcion puede tardar hasta 100 s
y esa cola corre con concurrency 2: dos notas de voz largas bloquearian las
respuestas del LLM de todos los tenants. Se ejecuta entre la persistencia del
mensaje (`webhook_processor`) y el grafo (`ai_processor`):

1. Resuelve la URL del medio (en Telegram hay que pedirle a `getFile` una URL de
   descarga; la de WhatsApp/Instagram/Facebook ya viene en el mensaje).
2. Descarga el audio y lo transcribe con Whisper (`app/services/transcription.py`).
3. Guarda el texto en `messages.content` (junto con la duracion en `metadata`).
4. Encola `process_ai_response` con el texto ya puesto en `message_data["text"]`.

Idempotencia: si la tarea se reintenta despues de haber guardado la transcripcion
(p. ej. fallo al encolar la IA), no se vuelve a llamar a Whisper: se reutiliza el
texto de `messages.content`.

Fallo: ante un error permanente (audio demasiado grande, formato que Whisper no
lee, audio sin voz, credencial invalida) o con los reintentos agotados, la
conversacion se escala a un humano con el motivo `transcription_failed` — hay una
persona esperando y el bot no puede leer lo que dijo. Si mientras tanto un humano
ya tomo la conversacion, no se le pisa el estado.
"""

import logging
from typing import Any
from uuid import UUID

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import run_isolated, tenant_session
from app.core.metrics import record_tokens
from app.models.conversation import Conversation
from app.models.message import Message
from app.services.transcription import (
    PermanentTranscriptionError,
    Transcription,
    download_media,
    inferir_nombre_audio,
    transcribe_audio,
)

logger = logging.getLogger(__name__)

#: Motivo con el que se escala cuando no se pudo transcribir.
TRANSCRIPTION_FAILED_REASON = "transcription_failed"

#: Estados en los que un humano ya tiene la conversacion (igual que
#: `webhook_processor.HUMAN_OWNED_STATUSES`; duplicado para no importar el modulo
#: del webhook desde aqui y crear un ciclo).
HUMAN_OWNED_STATUSES = ("human_active", "waiting_human")

RETRY_COUNTDOWN_SECONDS = 10


async def resolve_media_url(media_url: str, channel: str) -> str:
    """Convierte el `media_url` del mensaje en una URL de descarga.

    Args:
        media_url: `NormalizedMessage.media_url`.
        channel: Canal de origen.

    Returns:
        URL descargable. Para Telegram (`telegram-file:<file_id>`) se resuelve con
        `getFile`; el resto de canales ya trae la URL.

    Raises:
        PermanentTranscriptionError: Si el canal de Telegram no esta configurado.
    """
    from app.agents.nodes._tenant import ChannelNotConfiguredError, get_channel_config
    from app.services.messaging.telegram import TelegramProvider

    file_id = TelegramProvider.file_id_de(media_url)
    if file_id is None:
        return media_url

    try:
        _, config = get_channel_config(channel)
    except ChannelNotConfiguredError as exc:
        raise PermanentTranscriptionError(str(exc)) from exc
    # La URL devuelta lleva el token del bot: no se registra en ningun log.
    return await TelegramProvider().get_file_url(file_id, config)


async def _leer_transcripcion_previa(client_id: UUID, message_id: UUID) -> str | None:
    """Lee el texto ya guardado del mensaje, si una ejecucion anterior lo dejo.

    Args:
        client_id: Tenant propietario.
        message_id: Mensaje de audio.

    Returns:
        El contenido existente, o `None` si aun no hay transcripcion.

    Raises:
        PermanentTranscriptionError: Si el mensaje no existe en el tenant.
    """
    async with tenant_session(client_id) as session:
        stmt = select(Message.content).where(
            Message.id == message_id, Message.client_id == client_id
        )
        fila = (await session.execute(stmt)).one_or_none()
    if fila is None:
        raise PermanentTranscriptionError(f"El mensaje {message_id} no existe en el tenant")
    contenido = fila[0]
    return contenido if contenido and contenido.strip() else None


async def _estado_conversacion(client_id: UUID, conversation_id: UUID) -> str | None:
    """Lee el estado actual de la conversacion.

    Args:
        client_id: Tenant propietario.
        conversation_id: Conversacion.

    Returns:
        El `status`, o `None` si la conversacion no existe en el tenant.
    """
    async with tenant_session(client_id) as session:
        stmt = select(Conversation.status).where(
            Conversation.id == conversation_id, Conversation.client_id == client_id
        )
        return (await session.execute(stmt)).scalar_one_or_none()


async def _guardar_transcripcion(
    client_id: UUID, message_id: UUID, resultado: Transcription, model: str
) -> None:
    """Guarda el texto transcrito y sus metadatos en el mensaje.

    Args:
        client_id: Tenant propietario.
        message_id: Mensaje de audio.
        resultado: Transcripcion obtenida.
        model: Modelo con el que se obtuvo.

    Raises:
        PermanentTranscriptionError: Si el mensaje no existe en el tenant.
    """
    async with tenant_session(client_id) as session:
        stmt = select(Message).where(Message.id == message_id, Message.client_id == client_id)
        mensaje = (await session.execute(stmt)).scalar_one_or_none()
        if mensaje is None:
            raise PermanentTranscriptionError(f"El mensaje {message_id} no existe en el tenant")
        mensaje.content = resultado.text
        # Se reasigna un dict nuevo: SQLAlchemy no detecta mutaciones in situ del JSONB.
        mensaje.metadata_ = {
            **(mensaje.metadata_ or {}),
            "transcription": {
                "model": model,
                "duration_seconds": resultado.duration_seconds,
            },
        }


async def _transcribir(
    client_id: str,
    conversation_id: str,
    channel: str,
    message_id: str,
    media_url: str,
) -> tuple[str, str | None]:
    """Obtiene y guarda la transcripcion del audio de un mensaje.

    Args:
        client_id: Tenant propietario.
        conversation_id: Conversacion del mensaje.
        channel: Canal de origen.
        message_id: Mensaje de audio ya persistido.
        media_url: URL (o referencia de canal) del audio.

    Returns:
        Tupla `(texto, estado_de_la_conversacion)`.

    Raises:
        PermanentTranscriptionError: Fallo que reintentar no arregla.
        TranscriptionError: Fallo transitorio.
    """
    tenant = UUID(client_id)
    mensaje_id = UUID(message_id)
    settings = get_settings()

    texto = await _leer_transcripcion_previa(tenant, mensaje_id)
    if texto is None:
        url = await resolve_media_url(media_url, channel)
        medio = await download_media(
            url,
            max_bytes=settings.WHISPER_MAX_AUDIO_BYTES,
            timeout_s=settings.MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
        )
        resultado = await transcribe_audio(
            medio.content,
            inferir_nombre_audio(medio.content_type, medio.url_path),
            api_key=settings.OPENAI_API_KEY,
            model=settings.WHISPER_MODEL,
            language=settings.WHISPER_LANGUAGE or None,
            timeout_s=settings.WHISPER_TIMEOUT_SECONDS,
        )
        await _guardar_transcripcion(tenant, mensaje_id, resultado, settings.WHISPER_MODEL)
        record_tokens(
            client_id,
            settings.WHISPER_MODEL,
            "transcription",
            0,
            0,
            cost_usd=resultado.duration_seconds / 60 * settings.WHISPER_COST_PER_MINUTE_USD,
        )
        texto = resultado.text
        logger.info(
            "Audio transcrito en %s: %.1fs, %d caracteres",
            conversation_id,
            resultado.duration_seconds,
            len(texto),
        )

    return texto, await _estado_conversacion(tenant, UUID(conversation_id))


async def _escalar_si_corresponde(
    client_id: str, conversation_id: str, contact_id: str, channel: str
) -> str:
    """Escala a un humano salvo que ya tenga la conversacion.

    Args:
        client_id: Tenant propietario.
        conversation_id: Conversacion afectada.
        contact_id: Contacto que espera respuesta.
        channel: Canal de origen.

    Returns:
        `"handoff"` si se escalo, `"human_owned"` si un humano ya la tenia.
    """
    from app.tasks.ai_processor import _emergency_handoff

    estado = await _estado_conversacion(UUID(client_id), UUID(conversation_id))
    if estado in HUMAN_OWNED_STATUSES:
        return "human_owned"
    await _emergency_handoff(
        client_id, conversation_id, contact_id, channel, TRANSCRIPTION_FAILED_REASON
    )
    return "handoff"


@shared_task(
    name="app.tasks.media_transcribe_audio",
    bind=True,
    max_retries=2,
    acks_late=True,
    queue="media",
    time_limit=120,
    soft_time_limit=100,
)
def transcribe_audio_message(
    self: Any,
    client_id: str,
    conversation_id: str,
    contact_id: str,
    channel: str,
    message_id: str,
    message_data: dict[str, Any],
) -> dict[str, str]:
    """Transcribe el audio de un mensaje y lo encola para la IA.

    Args:
        self: Instancia de la tarea (bind=True), para los reintentos.
        client_id: Tenant propietario.
        conversation_id: Conversacion en curso.
        contact_id: Contacto que escribio.
        channel: Canal de origen.
        message_id: Mensaje de audio ya persistido.
        message_data: `NormalizedMessage` serializado (sin texto).

    Returns:
        `{"status": ...}`: `transcribed`, `handoff` o `human_owned`.

    Raises:
        Retry: Reintento (2 como maximo) ante un fallo transitorio.
    """
    media_url = message_data.get("media_url")
    try:
        if not media_url:
            raise PermanentTranscriptionError("El mensaje de audio no trae media_url")
        texto, estado = run_isolated(
            _transcribir(client_id, conversation_id, channel, message_id, media_url)
        )
    except (PermanentTranscriptionError, SoftTimeLimitExceeded) as exc:
        # Ninguno mejora reintentando: el audio no va a encogerse ni a tener voz.
        logger.error("Audio no transcribible en %s: %s", conversation_id, exc)
        return {
            "status": run_isolated(
                _escalar_si_corresponde(client_id, conversation_id, contact_id, channel)
            )
        }
    except Exception as exc:
        if self.request.retries < self.max_retries:
            logger.warning(
                "Fallo la transcripcion en %s (intento %s/%s): %s",
                conversation_id,
                self.request.retries + 1,
                self.max_retries,
                exc,
            )
            raise self.retry(exc=exc, countdown=RETRY_COUNTDOWN_SECONDS) from exc

        logger.critical(
            "Transcripcion agotada tras %s intentos en %s: %s",
            self.max_retries,
            conversation_id,
            exc,
        )
        return {
            "status": run_isolated(
                _escalar_si_corresponde(client_id, conversation_id, contact_id, channel)
            )
        }

    if estado in HUMAN_OWNED_STATUSES:
        # Un humano tomo la conversacion mientras se transcribia: el texto queda
        # guardado para que lo lea, pero el bot no responde.
        logger.info("Conversacion %s en manos de un humano; no se encola IA", conversation_id)
        return {"status": "human_owned"}

    from app.tasks.ai_processor import process_ai_response

    process_ai_response.delay(
        client_id=client_id,
        conversation_id=conversation_id,
        contact_id=contact_id,
        channel=channel,
        message_data={**message_data, "text": texto},
    )
    return {"status": "transcribed"}
