"""Speech-to-Text del canal de voz: frases completas a Whisper (Sprint 13, Dev A).

Whisper no transcribe en streaming (el spec lo reconoce en sus notas): recibe
un archivo y devuelve el texto. La estrategia es la del spec, acumular audio
hasta detectar el final de una frase y mandar esa frase entera, pero con tres
cambios de fondo:

- **El silencio se mide en audio, no en reloj.** El spec arma un temporizador
  con `asyncio.sleep()` que se reinicia con cada trama con voz. Twilio manda
  audio continuo (tramas de 20 ms tambien durante el silencio), asi que contar
  milisegundos de audio silencioso es determinista, no depende de la carga del
  event loop y se puede probar sin esperas reales.
- **La transcripcion no bloquea la recepcion.** Mientras Whisper responde (cientos
  de milisegundos), las tramas siguen llegando por el WebSocket. Las frases se
  encolan y las transcribe una tarea aparte, **en orden**: con una tarea por
  frase, dos frases seguidas podian llegar al agente al reves.
- **Se guarda un poco de audio previo a la voz** (pre-roll). El detector se
  dispara cuando la energia ya subio; sin ese colchon, Whisper recibe la primera
  silaba cortada.

La transcripcion reutiliza `transcribe_audio()` del Sprint 9, con sus mismos
errores: un fallo permanente (audio sin voz) descarta la frase; uno transitorio
tambien, con un warning, porque en una llamada en curso no hay reintento que
valga — el cliente ya siguio hablando.
"""

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Awaitable, Callable

from app.core.config import get_settings
from app.core.metrics import record_tokens
from app.services.transcription import (
    PermanentTranscriptionError,
    TranscriptionError,
    transcribe_audio,
)
from app.services.voice.audio import SAMPLE_RATE, pcm16_to_wav, rms_pcm16

logger = logging.getLogger(__name__)

#: Audio previo a la deteccion de voz que se conserva, para no cortar la primera silaba.
PRE_ROLL_MS = 200

#: Voz minima para considerar que hubo una frase (descarta golpes y clics).
MIN_SPEECH_MS = 250

#: Espera maxima al cerrar la sesion para terminar de transcribir lo pendiente.
CLOSE_TIMEOUT_SECONDS = 10.0

Transcriptor = Callable[[bytes], Awaitable[str | None]]
AlTranscribir = Callable[[str], Awaitable[None]]


def _ms(pcm16: bytes | bytearray) -> float:
    """Duracion de un trozo de PCM de 16 bits a 8 kHz.

    Args:
        pcm16: Audio.

    Returns:
        Milisegundos.
    """
    return len(pcm16) / 2 / SAMPLE_RATE * 1000


class UtteranceSegmenter:
    """Corta el audio continuo de una llamada en frases.

    Sin estado de red ni asincronia: recibe tramas y, cuando una frase termina,
    la devuelve. Toda la logica de "cuando termino de hablar" vive aca y se
    prueba con audio sintetico.

    Attributes:
        speech_threshold_rms: Energia a partir de la cual una trama es voz.
        end_silence_ms: Silencio seguido que cierra una frase.
        max_utterance_ms: Tope de una frase; al alcanzarlo se corta igual.
    """

    def __init__(
        self, speech_threshold_rms: int, end_silence_ms: int, max_utterance_ms: int
    ) -> None:
        """Crea el segmentador.

        Args:
            speech_threshold_rms: Energia minima de voz.
            end_silence_ms: Silencio que cierra una frase.
            max_utterance_ms: Duracion maxima de una frase.
        """
        self.speech_threshold_rms = speech_threshold_rms
        self.end_silence_ms = end_silence_ms
        self.max_utterance_ms = max_utterance_ms
        self._previo: deque[bytes] = deque()
        self._previo_ms = 0.0
        self._frase = bytearray()
        self._hablando = False
        self._voz_ms = 0.0
        self._silencio_ms = 0.0

    def feed(self, pcm16: bytes) -> bytes | None:
        """Procesa una trama.

        Args:
            pcm16: Trama de audio, PCM de 16 bits a 8 kHz.

        Returns:
            El audio de una frase completa si esta trama la cerro, o `None`.
        """
        duracion = _ms(pcm16)
        es_voz = rms_pcm16(pcm16) >= self.speech_threshold_rms

        if not self._hablando:
            if not es_voz:
                self._guardar_previo(pcm16, duracion)
                return None
            self._hablando = True
            self._frase = bytearray(b"".join(self._previo))
            self._previo.clear()
            self._previo_ms = 0.0

        self._frase.extend(pcm16)
        if es_voz:
            self._voz_ms += duracion
            self._silencio_ms = 0.0
        else:
            self._silencio_ms += duracion

        if self._silencio_ms >= self.end_silence_ms or _ms(self._frase) >= self.max_utterance_ms:
            return self._cerrar()
        return None

    def flush(self) -> bytes | None:
        """Cierra la frase en curso, si la hay (fin de la llamada).

        Returns:
            El audio de la frase, o `None` si no habia una con voz suficiente.
        """
        return self._cerrar() if self._hablando else None

    def _guardar_previo(self, pcm16: bytes, duracion: float) -> None:
        """Mantiene los ultimos `PRE_ROLL_MS` de silencio.

        Args:
            pcm16: Trama sin voz.
            duracion: Su duracion.
        """
        self._previo.append(pcm16)
        self._previo_ms += duracion
        while self._previo and self._previo_ms - _ms(self._previo[0]) >= PRE_ROLL_MS:
            self._previo_ms -= _ms(self._previo.popleft())

    def _cerrar(self) -> bytes | None:
        """Termina la frase en curso y reinicia el estado.

        Returns:
            El audio, o `None` si tuvo menos de `MIN_SPEECH_MS` de voz.
        """
        frase, voz = bytes(self._frase), self._voz_ms
        self._frase = bytearray()
        self._hablando = False
        self._voz_ms = 0.0
        self._silencio_ms = 0.0
        return frase if voz >= MIN_SPEECH_MS else None


async def transcribir_con_whisper(pcm16: bytes, client_id: str) -> str | None:
    """Transcribe una frase de la llamada con Whisper.

    Args:
        pcm16: Audio de la frase, PCM de 16 bits a 8 kHz.
        client_id: Tenant al que se imputa el costo.

    Returns:
        El texto, o `None` si no se pudo o no habia voz reconocible.
    """
    settings = get_settings()
    try:
        resultado = await transcribe_audio(
            pcm16_to_wav(pcm16),
            "frase.wav",
            api_key=settings.OPENAI_API_KEY,
            model=settings.WHISPER_MODEL,
            language=settings.WHISPER_LANGUAGE or None,
            timeout_s=settings.WHISPER_TIMEOUT_SECONDS,
        )
    except PermanentTranscriptionError as exc:
        logger.info("Frase de voz descartada: %s", exc)
        return None
    except TranscriptionError:
        logger.warning("Fallo transitorio de Whisper en una llamada; frase perdida", exc_info=True)
        return None

    record_tokens(
        client_id,
        settings.WHISPER_MODEL,
        "voice_transcription",
        0,
        0,
        cost_usd=resultado.duration_seconds / 60 * settings.WHISPER_COST_PER_MINUTE_USD,
    )
    return resultado.text


class STTSession:
    """Transcripcion de una llamada: segmenta, encola y transcribe en orden.

    Attributes:
        call_sid: Llamada a la que pertenece (para los logs).
    """

    def __init__(
        self,
        call_sid: str,
        on_text: AlTranscribir,
        segmenter: UtteranceSegmenter,
        transcriber: Transcriptor,
    ) -> None:
        """Crea la sesion y arranca la tarea que transcribe.

        Debe crearse dentro de un event loop en marcha.

        Args:
            call_sid: Llamada.
            on_text: Se llama con el texto de cada frase, en orden.
            segmenter: Segmentador de frases.
            transcriber: Funcion que convierte una frase en texto
                (`transcribir_con_whisper` con el tenant ya fijado).
        """
        self.call_sid = call_sid
        self._on_text = on_text
        self._segmenter = segmenter
        self._transcriber = transcriber
        self._pendientes: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._tarea = asyncio.create_task(self._transcribir_en_orden())

    def feed(self, pcm16: bytes) -> None:
        """Procesa una trama del cliente; si cierra una frase, la encola.

        Es sincrono a proposito: el bucle que lee el WebSocket no espera a
        Whisper.

        Args:
            pcm16: Trama de audio, PCM de 16 bits a 8 kHz.
        """
        frase = self._segmenter.feed(pcm16)
        if frase is not None:
            self._pendientes.put_nowait(frase)

    async def close(self) -> None:
        """Transcribe lo que quedo pendiente y termina la tarea.

        Si Whisper no termina en `CLOSE_TIMEOUT_SECONDS`, se cancela: la
        llamada ya se colgo y no hay a quien responderle.
        """
        frase = self._segmenter.flush()
        if frase is not None:
            self._pendientes.put_nowait(frase)
        self._pendientes.put_nowait(None)
        try:
            await asyncio.wait_for(asyncio.shield(self._tarea), timeout=CLOSE_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("STT de %s sin terminar al colgar; se cancela", self.call_sid)
            self._tarea.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tarea

    async def _transcribir_en_orden(self) -> None:
        """Consume la cola de frases hasta recibir el `None` de cierre."""
        while True:
            frase = await self._pendientes.get()
            if frase is None:
                return
            try:
                texto = await self._transcriber(frase)
                if texto:
                    await self._on_text(texto)
            except Exception:
                # Una frase que falla no puede tumbar la transcripcion del resto
                # de la llamada.
                logger.exception("Error procesando una frase de %s", self.call_sid)
