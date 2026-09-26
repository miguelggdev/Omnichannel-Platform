"""Una llamada en curso: audio de ida y vuelta con el agente (Sprint 13, Dev A).

`CallSession` es el estado y la logica de una llamada mientras dura su stream
de audio. El WebSocket (`app/api/v1/voice_ws.py`) solo lee eventos de Twilio y
se los pasa; aca se decide que hacer con ellos:

- **Audio del cliente** (`media`): se decodifica, se mira si interrumpe al
  agente (barge-in) y se pasa al STT, que devuelve frases transcritas.
- **Cada frase** se normaliza con `TwilioVoiceProvider.parse_webhook()` y se
  encola en `process_incoming_message`, igual que un WhatsApp: el contacto, la
  conversacion, el grafo y el CRM no saben que fue una llamada.
- **Las respuestas** llegan por el canal de Redis del llamante (las publica
  `TwilioVoiceProvider.send_message()` desde el worker), se sintetizan y se
  mandan a Twilio como tramas `media`, seguidas de una `mark` para saber cuando
  termino de sonar.
- **Barge-in:** si el cliente habla mientras suena una respuesta, se cancela
  la sintesis en curso y se manda `clear`, que vacia el audio que Twilio tenia
  en cola. Es la pieza que el spec deja como "la implementacion depende del
  backend".
- **Al colgar**, se guarda la transcripcion (`voice_tasks.save_call_record`).

Desviaciones sobre el spec (§4)
-------------------------------
El `CallManager` del spec es un singleton por proceso con un diccionario de
llamadas, llama a `app.services.conversation_pipeline.process_incoming_message`
y a `ContactService.get_or_create_by_identifier`, que no existen, y ejecuta el
agente dentro del proceso de la API. Aca cada conexion tiene su `CallSession` y
el agente corre donde corre para todos los canales: en los workers de Celery.
"""

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from functools import partial
from typing import Any

from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.services.dedup import get_redis, mark_if_new, release_mark
from app.services.messaging.voice_provider import TwilioVoiceProvider, canal_de_salida
from app.services.voice.audio import frames, mulaw_to_pcm16
from app.services.voice.interruption_handler import InterruptionHandler
from app.services.voice.stream_token import StreamClaims
from app.services.voice.stt_streaming import (
    STTSession,
    UtteranceSegmenter,
    transcribir_con_whisper,
)
from app.services.voice.tts_service import synthesize_mulaw

logger = logging.getLogger(__name__)

#: Canal con el que se deduplican las frases (el de `NormalizedMessage.channel`).
DEDUP_CHANNEL = "voice"

#: Turnos de la transcripcion que se guardan como maximo en `call_records`.
MAX_TRANSCRIPT_TURNS = 500

Enviar = Callable[[dict[str, Any]], Awaitable[None]]
Sintetizar = Callable[[str, str], Awaitable[bytes]]
Encolar = Callable[..., Any]


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encolar_mensaje(normalized: dict[str, Any]) -> None:
    """Encola una frase en el procesador de mensajes (import perezoso).

    Args:
        normalized: `NormalizedMessage` serializado.
    """
    from app.tasks.webhook_processor import process_incoming_message

    process_incoming_message.delay(
        provider="twilio", channel="voice", normalized_message=normalized
    )


def _encolar_registro(client_id: str, call_sid: str, datos: dict[str, Any]) -> None:
    """Encola la persistencia de un evento de la llamada (import perezoso).

    Args:
        client_id: Tenant.
        call_sid: Llamada.
        datos: Columnas de `call_records` que aporta el evento.
    """
    from app.tasks.voice_tasks import save_call_record

    save_call_record.delay(client_id=client_id, call_sid=call_sid, datos=datos)


class CallSession:
    """Una llamada con su stream de audio abierto.

    Attributes:
        claims: Datos de la llamada, del token del stream.
        stream_sid: `streamSid` de Twilio, obligatorio en cada evento saliente.
        transcript: Turnos de la llamada, `[{role, text, timestamp}]`.
    """

    def __init__(
        self,
        claims: StreamClaims,
        stream_sid: str,
        send: Enviar,
        *,
        synthesize: Sintetizar | None = None,
        transcriber: Callable[[bytes], Awaitable[str | None]] | None = None,
        enqueue_message: Encolar | None = None,
        enqueue_record: Encolar | None = None,
    ) -> None:
        """Prepara la sesion; `start()` la pone en marcha.

        Args:
            claims: Datos verificados de la llamada.
            stream_sid: `streamSid` del evento `start`.
            send: Envia un evento JSON a Twilio por el WebSocket.
            synthesize: Texto -> audio mu-law; por defecto OpenAI TTS.
            transcriber: Frase -> texto; por defecto Whisper.
            enqueue_message: Encola una frase normalizada; por defecto en
                `process_incoming_message`.
            enqueue_record: Encola un evento de `call_records`; por defecto en
                `save_call_record`.
        """
        settings = get_settings()
        self.claims = claims
        self.stream_sid = stream_sid
        self.transcript: list[dict[str, Any]] = []
        self._send = send
        self._synthesize = synthesize or synthesize_mulaw
        self._enqueue_message = enqueue_message or _encolar_mensaje
        self._enqueue_record = enqueue_record or _encolar_registro
        self._transcriber = transcriber or partial(
            transcribir_con_whisper, client_id=claims.client_id
        )
        self._settings = settings
        self._interrupcion = InterruptionHandler(
            settings.VOICE_BARGE_IN_RMS_THRESHOLD, settings.VOICE_BARGE_IN_MIN_MS
        )
        self._stt: STTSession | None = None
        self._pubsub: Any = None
        self._escucha: asyncio.Task[None] | None = None
        self._hablando: asyncio.Task[None] | None = None
        self._marcas_pendientes: set[str] = set()
        self._frases = 0
        self._envio = asyncio.Lock()

    # ─── Ciclo de vida ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Se suscribe a las respuestas del llamante y arranca el STT.

        El saludo ya lo dijo Twilio (`<Say>` del TwiML); se anota en la
        transcripcion para que quede completa.
        """
        self._stt = STTSession(
            self.claims.call_sid,
            self._al_transcribir,
            UtteranceSegmenter(
                self._settings.VOICE_SPEECH_RMS_THRESHOLD,
                self._settings.VOICE_END_OF_SPEECH_SILENCE_MS,
                self._settings.VOICE_MAX_UTTERANCE_SECONDS * 1000,
            ),
            self._transcriber,
        )
        self._pubsub = get_redis().pubsub()
        await self._pubsub.subscribe(
            canal_de_salida(self.claims.client_id, self.claims.contact_phone)
        )
        self._escucha = asyncio.create_task(self._escuchar_respuestas())
        self._anotar("agent", self._saludo())

    async def close(self, reason: str) -> None:
        """Termina la llamada: transcribe lo pendiente y guarda el registro.

        Args:
            reason: Motivo, para los logs (`stop`, `disconnected`, `timeout`...).
        """
        for tarea in (self._escucha, self._hablando):
            if tarea is not None:
                tarea.cancel()
        await asyncio.gather(
            *(t for t in (self._escucha, self._hablando) if t is not None),
            return_exceptions=True,
        )
        if self._stt is not None:
            # Lo que el cliente dijo justo antes de colgar tambien se encola:
            # queda en la conversacion aunque ya no haya a quien responder.
            await self._stt.close()
        if self._pubsub is not None:
            try:
                await self._pubsub.unsubscribe()
                await self._pubsub.close()
            except Exception:
                logger.warning("Voz: no se pudo cerrar la suscripcion de Redis", exc_info=True)

        await self._guardar(
            {
                "ended_at": _ahora(),
                "transcript": self.transcript[-MAX_TRANSCRIPT_TURNS:],
            }
        )
        logger.info(
            "Llamada %s terminada (%s): %d turnos",
            self.claims.call_sid,
            reason,
            len(self.transcript),
        )

    # ─── Eventos de Twilio ───────────────────────────────────────────────────

    async def on_media(self, payload_b64: str) -> None:
        """Procesa una trama de audio del cliente.

        Args:
            payload_b64: Audio mu-law en base64, tal como lo manda Twilio.
        """
        try:
            pcm = mulaw_to_pcm16(base64.b64decode(payload_b64, validate=True))
        except ValueError:
            logger.warning("Voz: trama de audio no valida en %s", self.claims.call_sid)
            return
        if self.agente_hablando and self._interrupcion.detect(pcm):
            await self.interrumpir()
        if self._stt is not None:
            self._stt.feed(pcm)

    def on_mark(self, name: str) -> None:
        """Twilio termino de reproducir el audio hasta esta marca.

        Args:
            name: Nombre de la marca.
        """
        self._marcas_pendientes.discard(name)
        if not self.agente_hablando:
            self._interrupcion.reset()

    @property
    def agente_hablando(self) -> bool:
        """Si hay una respuesta sintetizandose o sonando en la llamada."""
        return bool(self._marcas_pendientes) or (
            self._hablando is not None and not self._hablando.done()
        )

    async def interrumpir(self) -> None:
        """Calla al agente: el cliente empezo a hablar (barge-in)."""
        logger.info("Barge-in en la llamada %s", self.claims.call_sid)
        if self._hablando is not None:
            self._hablando.cancel()
        self._marcas_pendientes.clear()
        self._interrupcion.reset()
        # `clear` vacia el audio que Twilio ya tenia en cola para reproducir.
        await self._enviar({"event": "clear", "streamSid": self.stream_sid})

    # ─── Entrada: frases del cliente ─────────────────────────────────────────

    async def _al_transcribir(self, texto: str) -> None:
        """Encola una frase transcrita como mensaje entrante del canal de voz.

        Args:
            texto: Lo que dijo el cliente.
        """
        self._frases += 1
        self._anotar("caller", texto)
        normalized = await TwilioVoiceProvider().parse_webhook(
            {
                "CallSid": self.claims.call_sid,
                "From": self.claims.phone_from,
                "To": self.claims.phone_to,
                "Direction": "outbound-api" if self.claims.direction == "outbound" else "inbound",
                "SpeechResult": texto,
                "UtteranceIndex": self._frases,
            }
        )
        externo = normalized.external_message_id
        if not await mark_if_new(DEDUP_CHANNEL, externo):
            return
        try:
            await run_in_threadpool(self._enqueue_message, normalized.model_dump(mode="json"))
        except Exception:
            await release_mark(DEDUP_CHANNEL, externo)
            logger.exception("Voz: no se pudo encolar una frase de %s", self.claims.call_sid)

    # ─── Salida: respuestas del agente ───────────────────────────────────────

    async def _escuchar_respuestas(self) -> None:
        """Dice, en orden, cada respuesta publicada para el llamante."""
        async for mensaje in self._pubsub.listen():
            if mensaje.get("type") != "message":
                continue
            try:
                frame = json.loads(mensaje["data"])
            except (TypeError, ValueError):
                logger.warning("Voz: frame no valido en el canal de salida")
                continue
            texto = str(frame.get("text") or "").strip()
            if frame.get("type") != "say" or not texto:
                continue
            self._anotar("agent", texto)
            self._hablando = asyncio.create_task(self._decir(texto, str(frame.get("message_id"))))
            # `wait` y no `await`: si un barge-in cancela esta respuesta, la
            # escucha tiene que seguir para la proxima.
            await asyncio.wait({self._hablando})

    async def _decir(self, texto: str, message_id: str) -> None:
        """Sintetiza una respuesta y la manda a Twilio.

        Args:
            texto: Respuesta del agente.
            message_id: Id del mensaje, usado como nombre de la marca.
        """
        try:
            audio = await self._synthesize(texto, self.claims.client_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Voz: no se pudo sintetizar una respuesta en %s", self.claims.call_sid)
            return
        self._marcas_pendientes.add(message_id)
        for trama in frames(audio):
            await self._enviar(
                {
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": base64.b64encode(trama).decode("ascii")},
                }
            )
        await self._enviar(
            {"event": "mark", "streamSid": self.stream_sid, "mark": {"name": message_id}}
        )

    # ─── Utilidades ──────────────────────────────────────────────────────────

    async def _enviar(self, evento: dict[str, Any]) -> None:
        """Manda un evento a Twilio, sin intercalar con otro envio.

        Args:
            evento: Evento JSON de Media Streams.
        """
        async with self._envio:
            await self._send(evento)

    def _anotar(self, rol: str, texto: str) -> None:
        """Agrega un turno a la transcripcion.

        Args:
            rol: `caller` o `agent`.
            texto: Lo dicho.
        """
        self.transcript.append({"role": rol, "text": texto, "timestamp": _ahora()})

    def _saludo(self) -> str:
        """Saludo que dijo Twilio al contestar.

        Returns:
            El de entrantes o el de salientes, segun la direccion.
        """
        if self.claims.direction == "outbound":
            return self._settings.VOICE_OUTBOUND_WELCOME_MESSAGE
        return self._settings.VOICE_WELCOME_MESSAGE

    async def _guardar(self, datos: dict[str, Any]) -> None:
        """Encola un evento de `call_records`; si falla, lo registra y sigue.

        Args:
            datos: Columnas que aporta el evento.
        """
        try:
            await run_in_threadpool(
                self._enqueue_record, self.claims.client_id, self.claims.call_sid, datos
            )
        except Exception:
            logger.exception("Voz: no se pudo encolar el registro de %s", self.claims.call_sid)
