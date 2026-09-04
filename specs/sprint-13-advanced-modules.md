# Sprint 13 — Canal de Voz & Agente Clínico (Fase 3)

## Objetivo

Implementar un canal de voz para llamadas telefónicas con transcripción automática (STT streaming), síntesis de voz (TTS) y manejo de interrupciones, además de un agente clínico especializado para dictado médico (RIPS — Registro Individual de Prestación de Servicios, normativa colombiana), codificación automática CIE-10/CUPS y cumplimiento de Habeas Data (Ley 1581 de 2012).

## Prerequisitos

- Sprint 12 completado (agentes financiero y marketing funcionales).
- `MessagingProvider` ABC implementada (Sprint 4) con los 5 métodos obligatorios.
- `ProviderFactory` actualizada con todos los proveedores de Fase 2 (Sprint 9).
- Cuenta de Twilio (Voice API) o Vonage (Nexmo) con número telefónico aprovisionado.
- Whisper API key (OpenAI) para transcripción de audio (Sprint 9 ya usa Whisper para mensajes de voz; aquí se extiende a streaming).
- OpenAI TTS API o Google Cloud TTS para síntesis de voz.
- LangGraph grafo funcional con intent routing, sentimiento y nodos de agentes.
- Celery con colas `default`, `bulk` y `notifications` configuradas.
- Familiaridad con la normativa RIPS colombiana (Resolución 3374 de 2000 y actualizaciones) y la Ley 1581 de 2012 (Habeas Data).
- pgcrypto habilitado (Sprint 8) para cifrado de datos clínicos sensibles.

## Archivos a Crear/Modificar

| Archivo | Acción | Descripción |
|---|---|---|
| `app/services/messaging/voice_provider.py` | Crear | VoiceProvider — implementación Twilio/Vonage |
| `app/services/voice/stt_streaming.py` | Crear | Speech-to-Text streaming con WebSocket |
| `app/services/voice/tts_service.py` | Crear | Text-to-Speech service (OpenAI / Google Cloud) |
| `app/services/voice/call_manager.py` | Crear | Gestor del ciclo de vida de llamadas |
| `app/services/voice/interruption_handler.py` | Crear | Manejo de interrupciones (barge-in) |
| `app/services/messaging/factory.py` | Modificar | Registrar VoiceProvider |
| `app/api/v1/voice.py` | Crear | Endpoints de webhook para Twilio/Vonage |
| `app/api/v1/voice_ws.py` | Crear | WebSocket endpoint para audio streaming |
| `app/agents/nodes/clinical.py` | Crear | Nodo agente clínico para el grafo LangGraph |
| `app/agents/tools/clinical_tools.py` | Crear | Tools de dictado médico, RIPS, CIE-10/CUPS |
| `app/agents/nodes/intent_routing.py` | Modificar | Agregar intents clínicos |
| `app/agents/graph.py` | Modificar | Agregar nodos voice_stt, voice_tts, clinical_agent |
| `app/models/call_record.py` | Crear | Modelo SQLAlchemy para registros de llamadas |
| `app/models/clinical_record.py` | Crear | Modelo SQLAlchemy para registros clínicos RIPS |
| `app/schemas/voice.py` | Crear | Schemas Pydantic para canal de voz |
| `app/schemas/clinical.py` | Crear | Schemas Pydantic para registros clínicos |
| `app/tasks/voice_tasks.py` | Crear | Tasks Celery para procesamiento de audio |
| `app/core/habeas_data.py` | Crear | Utilidades de cumplimiento Habeas Data |
| `migrations/versions/xxx_call_records.py` | Crear | Migración para tabla call_records |
| `migrations/versions/xxx_clinical_records.py` | Crear | Migración para tabla clinical_records |
| `tests/unit/test_voice_provider.py` | Crear | Tests unitarios VoiceProvider |
| `tests/unit/test_stt_streaming.py` | Crear | Tests de STT streaming |
| `tests/unit/test_tts_service.py` | Crear | Tests de TTS |
| `tests/unit/test_clinical_agent.py` | Crear | Tests del agente clínico |
| `tests/unit/test_habeas_data.py` | Crear | Tests de cumplimiento Habeas Data |
| `tests/integration/test_voice_flow.py` | Crear | Test de flujo completo de llamada |

## Tareas Detalladas

### 1. VoiceProvider — `app/services/messaging/voice_provider.py`

**1.1 Estructura de la clase**

```python
# app/services/messaging/voice_provider.py
from app.services.messaging.base import MessagingProvider, ChannelConstraints, NormalizedMessage
from app.core.config import settings
from enum import Enum
import httpx
import hmac
import hashlib
from urllib.parse import urlencode

class VoiceBackend(str, Enum):
    TWILIO = "twilio"
    VONAGE = "vonage"

class VoiceProvider(MessagingProvider):
    """
    Proveedor de mensajería para canal de voz (llamadas telefónicas).
    Soporta Twilio y Vonage como backends intercambiables.
    """

    def __init__(self, provider_config: dict):
        self.backend = VoiceBackend(provider_config.get("backend", "twilio"))
        self.account_sid = provider_config.get("account_sid")  # Twilio
        self.auth_token = provider_config.get("auth_token")    # Twilio
        self.api_key = provider_config.get("api_key")          # Vonage
        self.api_secret = provider_config.get("api_secret")    # Vonage
        self.phone_number = provider_config.get("phone_number")
        self.webhook_url = provider_config.get("webhook_url")
        self.ws_url = provider_config.get("ws_url")  # URL WebSocket para audio streaming

    async def parse_webhook(self, payload: dict, headers: dict) -> NormalizedMessage:
        """
        Parsear webhook de llamada entrante.
        Twilio: StatusCallback / <Gather> result / recording
        Vonage: Event webhook / Input webhook
        """
        if self.backend == VoiceBackend.TWILIO:
            return await self._parse_twilio_webhook(payload, headers)
        return await self._parse_vonage_webhook(payload, headers)

    async def _parse_twilio_webhook(self, payload: dict, headers: dict) -> NormalizedMessage:
        """Parsear webhook de Twilio Voice."""
        call_sid = payload.get("CallSid")
        caller = payload.get("From", "")     # +57XXXXXXXXXX
        called = payload.get("To", "")
        call_status = payload.get("CallStatus")  # ringing, in-progress, completed, busy, failed, no-answer
        direction = payload.get("Direction", "inbound")  # inbound, outbound-api, outbound-dial

        # Si es resultado de <Gather> (DTMF o speech)
        speech_result = payload.get("SpeechResult")
        digits = payload.get("Digits")

        content = speech_result or digits or f"[Llamada {call_status}]"

        return NormalizedMessage(
            sender_id=caller,
            message_id=call_sid,
            content=content,
            channel="voice",
            timestamp=datetime.utcnow(),
            metadata={
                "call_sid": call_sid,
                "call_status": call_status,
                "direction": direction,
                "caller": caller,
                "called": called,
                "speech_result": speech_result,
                "digits": digits,
                "confidence": payload.get("Confidence"),
                "recording_url": payload.get("RecordingUrl"),
                "recording_duration": payload.get("RecordingDuration"),
            },
        )

    async def _parse_vonage_webhook(self, payload: dict, headers: dict) -> NormalizedMessage:
        """Parsear webhook de Vonage Voice."""
        conversation_uuid = payload.get("conversation_uuid")
        caller = payload.get("from", "")
        status = payload.get("status")

        # Para input speech
        speech = payload.get("speech", {})
        speech_results = speech.get("results", [])
        content = speech_results[0].get("text", "") if speech_results else f"[Llamada {status}]"

        # Para DTMF
        dtmf = payload.get("dtmf", {})
        if dtmf.get("digits"):
            content = dtmf["digits"]

        return NormalizedMessage(
            sender_id=caller,
            message_id=conversation_uuid or payload.get("uuid", ""),
            content=content,
            channel="voice",
            timestamp=datetime.utcnow(),
            metadata={
                "conversation_uuid": conversation_uuid,
                "status": status,
                "direction": payload.get("direction", "inbound"),
                "caller": caller,
                "speech_confidence": speech_results[0].get("confidence") if speech_results else None,
            },
        )

    async def validate_signature(self, payload: bytes, headers: dict, secret: str) -> bool:
        """
        Validar firma del webhook.
        Twilio: X-Twilio-Signature — HMAC-SHA1 sobre URL + params sorted.
        Vonage: JWT signature en Authorization header.
        """
        if self.backend == VoiceBackend.TWILIO:
            return self._validate_twilio_signature(payload, headers, secret)
        return self._validate_vonage_signature(payload, headers, secret)

    def _validate_twilio_signature(self, payload: bytes, headers: dict, secret: str) -> bool:
        """
        Verificar X-Twilio-Signature.
        Twilio firma: HMAC-SHA1(auth_token, URL + parámetros POST ordenados).
        """
        signature = headers.get("x-twilio-signature", "")
        # Reconstruir la cadena firmada: URL completa + parámetros POST ordenados
        url = headers.get("x-original-url", self.webhook_url)
        import urllib.parse
        params = urllib.parse.parse_qs(payload.decode("utf-8"))
        # Ordenar parámetros y concatenar al URL
        sorted_params = sorted(params.items())
        data = url + "".join(f"{k}{v[0]}" for k, v in sorted_params)

        expected = hmac.new(
            secret.encode("utf-8"),
            data.encode("utf-8"),
            hashlib.sha1,
        ).digest()

        import base64
        expected_b64 = base64.b64encode(expected).decode("utf-8")
        return hmac.compare_digest(expected_b64, signature)

    def _validate_vonage_signature(self, payload: bytes, headers: dict, secret: str) -> bool:
        """
        Verificar JWT de Vonage.
        Vonage envía un JWT firmado en Authorization header. Verificar con api_secret.
        """
        auth_header = headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return False

        token = auth_header[7:]
        try:
            import jwt
            decoded = jwt.decode(token, secret, algorithms=["HS256"])
            return True
        except jwt.InvalidTokenError:
            return False

    async def send_message(self, recipient_id: str, content: str, **kwargs) -> dict:
        """
        Iniciar una llamada saliente o reproducir audio en una llamada activa.
        recipient_id: número telefónico destino (+57XXXXXXXXXX)
        content: texto para TTS o URL de audio pregrabado
        """
        if self.backend == VoiceBackend.TWILIO:
            return await self._twilio_send(recipient_id, content, **kwargs)
        return await self._vonage_send(recipient_id, content, **kwargs)

    async def _twilio_send(self, recipient_id: str, content: str, **kwargs) -> dict:
        """Enviar via Twilio Voice — iniciar llamada o hablar en llamada activa."""
        call_sid = kwargs.get("call_sid")

        if call_sid:
            # Modificar llamada en curso — inyectar TTS
            twiml = f'<Response><Say voice="Polly.Mia" language="es-CO">{content}</Say></Response>'
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Calls/{call_sid}.json",
                    auth=(self.account_sid, self.auth_token),
                    data={"Twiml": twiml},
                    timeout=10.0,
                )
                return {"success": response.status_code == 200, "call_sid": call_sid}
        else:
            # Iniciar nueva llamada
            twiml_url = kwargs.get("twiml_url", f"{self.webhook_url}/voice/twiml")
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Calls.json",
                    auth=(self.account_sid, self.auth_token),
                    data={
                        "To": recipient_id,
                        "From": self.phone_number,
                        "Url": twiml_url,
                        "StatusCallback": f"{self.webhook_url}/voice/status",
                        "StatusCallbackEvent": "initiated ringing answered completed",
                        "Record": kwargs.get("record", "false"),
                    },
                    timeout=10.0,
                )
                data = response.json()
                return {
                    "success": response.status_code == 201,
                    "call_sid": data.get("sid"),
                    "provider_message_id": data.get("sid"),
                }

    async def _vonage_send(self, recipient_id: str, content: str, **kwargs) -> dict:
        """Enviar via Vonage Voice — iniciar llamada o hablar en llamada activa."""
        conversation_uuid = kwargs.get("conversation_uuid")

        if conversation_uuid:
            # Hablar en llamada activa via NCCO action
            async with httpx.AsyncClient() as client:
                response = await client.put(
                    f"https://api.nexmo.com/v1/calls/{conversation_uuid}",
                    headers={"Authorization": f"Bearer {self._generate_vonage_jwt()}"},
                    json={
                        "action": "talk",
                        "text": content,
                        "voice_name": "Conchita",  # Voz en español
                        "language": "es-CO",
                    },
                    timeout=10.0,
                )
                return {"success": response.status_code == 200, "conversation_uuid": conversation_uuid}
        else:
            # Iniciar nueva llamada
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://api.nexmo.com/v1/calls",
                    headers={"Authorization": f"Bearer {self._generate_vonage_jwt()}"},
                    json={
                        "to": [{"type": "phone", "number": recipient_id}],
                        "from": {"type": "phone", "number": self.phone_number},
                        "answer_url": [f"{self.webhook_url}/voice/answer"],
                        "event_url": [f"{self.webhook_url}/voice/events"],
                    },
                    timeout=10.0,
                )
                data = response.json()
                return {
                    "success": response.status_code == 201,
                    "conversation_uuid": data.get("conversation_uuid"),
                    "provider_message_id": data.get("uuid"),
                }

    async def send_template(self, recipient_id: str, template_name: str, params: dict) -> dict:
        """
        No aplica directamente a voz. Se puede usar para iniciar
        una llamada con un script predeterminado (IVR template).
        """
        script = await self._get_ivr_script(template_name, params)
        return await self.send_message(recipient_id, script, **params)

    def get_channel_constraints(self) -> ChannelConstraints:
        """Restricciones del canal de voz."""
        return ChannelConstraints(
            max_text_length=None,         # Sin límite de texto (TTS)
            supports_media=False,         # No envía imágenes/videos
            supports_audio=True,          # Audio bidireccional (es el canal nativo)
            supports_buttons=False,       # No hay botones, solo DTMF
            supports_templates=True,      # Scripts IVR como templates
            supports_location=False,
            max_buttons=0,
            media_types=[],
            rate_limit_per_second=10,     # Máximo 10 llamadas simultáneas
            notes="Canal de voz via Twilio/Vonage. Soporta STT streaming, TTS y DTMF.",
        )
```

### 2. STT Streaming — `app/services/voice/stt_streaming.py`

**2.1 Speech-to-Text en tiempo real**

```python
# app/services/voice/stt_streaming.py
import asyncio
import json
from datetime import datetime
from uuid import UUID
from loguru import logger
from openai import AsyncOpenAI

class STTStreamingService:
    """
    Servicio de Speech-to-Text en streaming.
    Recibe audio chunks via WebSocket y produce transcripción parcial/final.
    Usa Whisper API para transcripción final y un modelo de streaming para parciales.
    """

    def __init__(self):
        self.openai_client = AsyncOpenAI()
        self.active_sessions: dict[str, STTSession] = {}

    async def create_session(
        self,
        call_id: str,
        client_id: UUID,
        language: str = "es",
        on_partial: callable = None,
        on_final: callable = None,
    ) -> "STTSession":
        """Crear sesión de transcripción streaming."""
        session = STTSession(
            call_id=call_id,
            client_id=client_id,
            language=language,
            on_partial=on_partial,
            on_final=on_final,
            openai_client=self.openai_client,
        )
        self.active_sessions[call_id] = session
        logger.info(f"Sesión STT creada para llamada {call_id}")
        return session

    async def close_session(self, call_id: str):
        """Cerrar sesión de transcripción."""
        session = self.active_sessions.pop(call_id, None)
        if session:
            await session.finalize()
            logger.info(f"Sesión STT cerrada para llamada {call_id}")

    def get_session(self, call_id: str) -> "STTSession | None":
        return self.active_sessions.get(call_id)


class STTSession:
    """
    Sesión individual de transcripción streaming.
    Acumula audio chunks, detecta silencios y produce transcripciones.
    """

    SILENCE_THRESHOLD_MS = 1500       # Silencio para considerar fin de utterance
    MAX_UTTERANCE_DURATION_S = 30     # Máximo de un utterance continuo
    AUDIO_SAMPLE_RATE = 8000          # 8kHz para llamadas telefónicas (G.711)
    AUDIO_FORMAT = "pcm"              # PCM sin compresión

    def __init__(
        self,
        call_id: str,
        client_id: UUID,
        language: str,
        on_partial: callable,
        on_final: callable,
        openai_client: AsyncOpenAI,
    ):
        self.call_id = call_id
        self.client_id = client_id
        self.language = language
        self.on_partial = on_partial
        self.on_final = on_final
        self.openai_client = openai_client
        self.audio_buffer = bytearray()
        self.last_audio_time = datetime.utcnow()
        self.utterances: list[dict] = []
        self.is_speaking = False
        self._silence_task: asyncio.Task | None = None

    async def process_audio_chunk(self, chunk: bytes):
        """
        Procesar un chunk de audio entrante.
        Detecta actividad de voz (VAD simple) y acumula para transcripción.
        """
        self.audio_buffer.extend(chunk)
        self.last_audio_time = datetime.utcnow()

        # VAD simple: si la energía RMS supera un umbral, hay voz
        rms = self._calculate_rms(chunk)
        if rms > 500:  # Umbral de actividad de voz (ajustable)
            if not self.is_speaking:
                self.is_speaking = True
                logger.debug(f"Voz detectada en llamada {self.call_id}")

            # Cancelar timer de silencio previo
            if self._silence_task:
                self._silence_task.cancel()

            # Iniciar nuevo timer de silencio
            self._silence_task = asyncio.create_task(
                self._silence_timeout()
            )
        elif self.is_speaking:
            # Audio recibido pero bajo umbral — posible silencio
            pass

    async def _silence_timeout(self):
        """Esperar silencio y producir transcripción."""
        try:
            await asyncio.sleep(self.SILENCE_THRESHOLD_MS / 1000)
            # Si llegamos aquí, hubo suficiente silencio
            self.is_speaking = False
            await self._transcribe_buffer()
        except asyncio.CancelledError:
            pass  # Silencio interrumpido por más audio

    async def _transcribe_buffer(self):
        """
        Transcribir el buffer de audio acumulado usando Whisper API.
        Produce transcripción final y limpia el buffer.
        """
        if len(self.audio_buffer) < 3200:  # Menos de 200ms de audio
            self.audio_buffer.clear()
            return

        audio_data = bytes(self.audio_buffer)
        self.audio_buffer.clear()

        try:
            # Convertir PCM a WAV en memoria para Whisper
            wav_data = self._pcm_to_wav(audio_data)

            # Enviar a Whisper API
            import io
            audio_file = io.BytesIO(wav_data)
            audio_file.name = "audio.wav"

            transcript = await self.openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language=self.language,
                response_format="verbose_json",
                timestamp_granularities=["word"],
            )

            text = transcript.text.strip()

            if text:
                utterance = {
                    "text": text,
                    "timestamp": datetime.utcnow().isoformat(),
                    "confidence": getattr(transcript, "avg_logprob", None),
                    "words": getattr(transcript, "words", []),
                    "language": transcript.language,
                }
                self.utterances.append(utterance)

                # Callback de transcripción final
                if self.on_final:
                    await self.on_final(self.call_id, utterance)

                logger.info(
                    f"Transcripción: '{text}'",
                    extra={"call_id": self.call_id, "language": transcript.language},
                )

        except Exception as e:
            logger.error(f"Error en transcripción: {e}", extra={"call_id": self.call_id})

    async def finalize(self) -> list[dict]:
        """Finalizar sesión: transcribir buffer restante y retornar todas las utterances."""
        if self._silence_task:
            self._silence_task.cancel()

        if len(self.audio_buffer) >= 3200:
            await self._transcribe_buffer()

        return self.utterances

    def _calculate_rms(self, chunk: bytes) -> float:
        """Calcular RMS (Root Mean Square) del chunk de audio como indicador de energía."""
        import struct
        samples = struct.unpack(f"<{len(chunk) // 2}h", chunk)
        if not samples:
            return 0
        return (sum(s * s for s in samples) / len(samples)) ** 0.5

    def _pcm_to_wav(self, pcm_data: bytes) -> bytes:
        """Convertir PCM crudo a formato WAV."""
        import struct
        import io
        wav = io.BytesIO()
        # WAV header
        num_channels = 1
        sample_width = 2  # 16-bit
        data_size = len(pcm_data)
        wav.write(b"RIFF")
        wav.write(struct.pack("<I", 36 + data_size))
        wav.write(b"WAVE")
        wav.write(b"fmt ")
        wav.write(struct.pack("<I", 16))             # chunk size
        wav.write(struct.pack("<H", 1))              # PCM format
        wav.write(struct.pack("<H", num_channels))
        wav.write(struct.pack("<I", self.AUDIO_SAMPLE_RATE))
        wav.write(struct.pack("<I", self.AUDIO_SAMPLE_RATE * num_channels * sample_width))
        wav.write(struct.pack("<H", num_channels * sample_width))
        wav.write(struct.pack("<H", sample_width * 8))
        wav.write(b"data")
        wav.write(struct.pack("<I", data_size))
        wav.write(pcm_data)
        return wav.getvalue()
```

### 3. TTS Service — `app/services/voice/tts_service.py`

```python
# app/services/voice/tts_service.py
from enum import Enum
from openai import AsyncOpenAI
from loguru import logger
import httpx

class TTSProvider(str, Enum):
    OPENAI = "openai"
    GOOGLE = "google"

class TTSService:
    """
    Servicio de Text-to-Speech para el canal de voz.
    Soporta OpenAI TTS y Google Cloud TTS como backends.
    """

    # Voces recomendadas para español latinoamericano
    OPENAI_VOICES = {
        "female": "nova",      # Voz femenina natural
        "male": "onyx",        # Voz masculina profunda
        "neutral": "shimmer",  # Voz neutral
    }

    GOOGLE_VOICES = {
        "female": "es-US-Neural2-A",
        "male": "es-US-Neural2-B",
        "neutral": "es-US-Neural2-C",
    }

    def __init__(self, provider: TTSProvider = TTSProvider.OPENAI):
        self.provider = provider
        self.openai_client = AsyncOpenAI()

    async def synthesize(
        self,
        text: str,
        voice_gender: str = "female",
        speed: float = 1.0,
        output_format: str = "pcm",
    ) -> bytes:
        """
        Sintetizar texto a audio.
        Retorna bytes de audio en el formato especificado.
        """
        if self.provider == TTSProvider.OPENAI:
            return await self._openai_tts(text, voice_gender, speed, output_format)
        return await self._google_tts(text, voice_gender, speed, output_format)

    async def _openai_tts(
        self,
        text: str,
        voice_gender: str,
        speed: float,
        output_format: str,
    ) -> bytes:
        """Sintetizar usando OpenAI TTS API."""
        voice = self.OPENAI_VOICES.get(voice_gender, "nova")

        response = await self.openai_client.audio.speech.create(
            model="tts-1",           # tts-1 para baja latencia, tts-1-hd para calidad
            voice=voice,
            input=text,
            speed=speed,
            response_format=output_format,  # "pcm" para streaming, "mp3" para grabación
        )

        audio_data = response.content
        logger.debug(f"TTS generado: {len(audio_data)} bytes, voz={voice}")
        return audio_data

    async def _google_tts(
        self,
        text: str,
        voice_gender: str,
        speed: float,
        output_format: str,
    ) -> bytes:
        """Sintetizar usando Google Cloud TTS API."""
        from google.cloud import texttospeech_v1 as tts

        client = tts.TextToSpeechAsyncClient()
        voice_name = self.GOOGLE_VOICES.get(voice_gender, "es-US-Neural2-A")

        response = await client.synthesize_speech(
            input=tts.SynthesisInput(text=text),
            voice=tts.VoiceSelectionParams(
                language_code="es-CO",
                name=voice_name,
            ),
            audio_config=tts.AudioConfig(
                audio_encoding=tts.AudioEncoding.LINEAR16 if output_format == "pcm"
                else tts.AudioEncoding.MP3,
                speaking_rate=speed,
                sample_rate_hertz=8000,  # 8kHz para telefonía
            ),
        )

        return response.audio_content

    async def synthesize_streaming(
        self,
        text: str,
        voice_gender: str = "female",
        chunk_size: int = 4096,
    ):
        """
        Sintetizar y emitir audio en chunks para streaming.
        Permite enviar audio al caller mientras se genera.
        """
        audio_data = await self.synthesize(text, voice_gender, output_format="pcm")

        # Emitir en chunks para streaming
        for i in range(0, len(audio_data), chunk_size):
            yield audio_data[i:i + chunk_size]
```

### 4. Gestor de Llamadas — `app/services/voice/call_manager.py`

```python
# app/services/voice/call_manager.py
from uuid import UUID, uuid4
from datetime import datetime
from loguru import logger
from app.services.voice.stt_streaming import STTStreamingService
from app.services.voice.tts_service import TTSService
from app.services.voice.interruption_handler import InterruptionHandler

class CallState:
    """Estado de una llamada activa."""
    def __init__(self, call_id: str, client_id: UUID, contact_id: UUID, channel_config: dict):
        self.call_id = call_id
        self.client_id = client_id
        self.contact_id = contact_id
        self.channel_config = channel_config
        self.started_at = datetime.utcnow()
        self.status = "ringing"   # ringing, connected, on_hold, transferring, completed, failed
        self.direction = "inbound"
        self.transcript: list[dict] = []
        self.current_tts_task = None
        self.is_ai_speaking = False
        self.conversation_id: UUID | None = None

class CallManager:
    """
    Gestor del ciclo de vida de llamadas.
    Coordina STT, TTS, interrupciones y el pipeline del agente conversacional.
    """

    def __init__(self):
        self.stt_service = STTStreamingService()
        self.tts_service = TTSService()
        self.interruption_handler = InterruptionHandler()
        self.active_calls: dict[str, CallState] = {}

    async def handle_incoming_call(
        self,
        call_id: str,
        client_id: UUID,
        caller_number: str,
        channel_config: dict,
    ) -> CallState:
        """
        Manejar una llamada entrante.
        1. Crear estado de llamada
        2. Crear sesión STT
        3. Enviar saludo TTS
        4. Conectar con el pipeline del agente
        """
        # Buscar o crear contacto por número telefónico
        contact = await self._resolve_contact(client_id, caller_number)

        call_state = CallState(
            call_id=call_id,
            client_id=client_id,
            contact_id=contact.id,
            channel_config=channel_config,
        )
        call_state.status = "connected"
        self.active_calls[call_id] = call_state

        # Crear sesión STT con callbacks
        await self.stt_service.create_session(
            call_id=call_id,
            client_id=client_id,
            language=channel_config.get("language", "es"),
            on_partial=self._on_partial_transcript,
            on_final=self._on_final_transcript,
        )

        logger.info(
            f"Llamada entrante conectada",
            extra={"call_id": call_id, "caller": caller_number},
        )

        return call_state

    async def handle_audio_chunk(self, call_id: str, audio_chunk: bytes):
        """
        Procesar chunk de audio recibido del caller.
        Si el agente está hablando y detecta voz, manejar interrupción (barge-in).
        """
        call_state = self.active_calls.get(call_id)
        if not call_state:
            return

        # Verificar interrupción (barge-in): el caller habla mientras el agente habla
        if call_state.is_ai_speaking:
            is_interruption = self.interruption_handler.detect_interruption(audio_chunk)
            if is_interruption:
                await self._handle_barge_in(call_state)

        # Enviar al STT
        stt_session = self.stt_service.get_session(call_id)
        if stt_session:
            await stt_session.process_audio_chunk(audio_chunk)

    async def _handle_barge_in(self, call_state: CallState):
        """
        Manejar interrupción del caller (barge-in).
        Detener TTS actual y escuchar al caller.
        """
        logger.info(f"Barge-in detectado en llamada {call_state.call_id}")

        # Cancelar TTS en curso
        if call_state.current_tts_task:
            call_state.current_tts_task.cancel()
            call_state.current_tts_task = None

        call_state.is_ai_speaking = False

        # Emitir silencio para detener el audio saliente
        # (La implementación depende del backend Twilio/Vonage)

    async def _on_partial_transcript(self, call_id: str, partial: dict):
        """Callback para transcripción parcial (indicador de que el caller habla)."""
        pass  # Se puede usar para UI en tiempo real

    async def _on_final_transcript(self, call_id: str, utterance: dict):
        """
        Callback para transcripción final de un utterance.
        Envía el texto transcrito al pipeline del agente conversacional.
        """
        call_state = self.active_calls.get(call_id)
        if not call_state:
            return

        text = utterance["text"]
        call_state.transcript.append({
            "role": "caller",
            "text": text,
            "timestamp": utterance["timestamp"],
        })

        # Procesar con el pipeline del agente (mismo flujo que mensajes de texto)
        from app.services.conversation_pipeline import process_incoming_message

        response = await process_incoming_message(
            client_id=call_state.client_id,
            contact_id=call_state.contact_id,
            channel="voice",
            content=text,
            metadata={"call_id": call_id},
        )

        # Convertir respuesta a audio TTS y enviar al caller
        if response:
            call_state.transcript.append({
                "role": "agent",
                "text": response,
                "timestamp": datetime.utcnow().isoformat(),
            })
            await self._speak_response(call_state, response)

    async def _speak_response(self, call_state: CallState, text: str):
        """Sintetizar respuesta y enviar audio al caller."""
        call_state.is_ai_speaking = True
        voice_gender = call_state.channel_config.get("voice_gender", "female")

        try:
            audio_data = await self.tts_service.synthesize(
                text=text,
                voice_gender=voice_gender,
                output_format="pcm",
            )
            # Enviar audio al caller via el provider
            # (Implementación depende de si es WebSocket directo o Twilio/Vonage media stream)
            await self._send_audio_to_caller(call_state, audio_data)
        except Exception as e:
            logger.error(f"Error en TTS: {e}", extra={"call_id": call_state.call_id})
        finally:
            call_state.is_ai_speaking = False

    async def end_call(self, call_id: str, reason: str = "completed"):
        """
        Finalizar una llamada.
        1. Cerrar sesión STT
        2. Guardar registro de llamada en DB
        3. Guardar transcripción completa
        """
        call_state = self.active_calls.pop(call_id, None)
        if not call_state:
            return

        # Cerrar STT
        utterances = await self.stt_service.close_session(call_id)

        call_state.status = reason

        # Guardar en DB
        from app.models.call_record import CallRecord
        call_record = CallRecord(
            client_id=call_state.client_id,
            contact_id=call_state.contact_id,
            conversation_id=call_state.conversation_id,
            call_id=call_id,
            direction=call_state.direction,
            status=reason,
            started_at=call_state.started_at,
            ended_at=datetime.utcnow(),
            duration_seconds=int((datetime.utcnow() - call_state.started_at).total_seconds()),
            transcript=call_state.transcript,
        )
        # Guardar call_record en DB
        # ...

        logger.info(
            f"Llamada finalizada",
            extra={"call_id": call_id, "duration": call_record.duration_seconds, "reason": reason},
        )

    async def _resolve_contact(self, client_id: UUID, phone_number: str):
        """Buscar contacto por número telefónico o crear uno nuevo."""
        from app.services.contact_service import ContactService
        contact_service = ContactService()
        return await contact_service.get_or_create_by_identifier(
            client_id=client_id,
            channel="voice",
            identifier_value=phone_number,
        )

    async def _send_audio_to_caller(self, call_state: CallState, audio_data: bytes):
        """Enviar audio al caller según el backend."""
        # Implementación depende del mecanismo de streaming:
        # - Twilio Media Streams: WebSocket bidireccional
        # - Vonage: WebSocket o NCCO actions
        pass
```

### 5. Manejo de Interrupciones — `app/services/voice/interruption_handler.py`

```python
# app/services/voice/interruption_handler.py
import struct

class InterruptionHandler:
    """
    Detector de interrupciones (barge-in).
    Detecta cuando el caller comienza a hablar mientras el agente está hablando.
    Usa VAD (Voice Activity Detection) simple basado en energía RMS.
    """

    BARGE_IN_THRESHOLD_RMS = 800       # Umbral de energía para considerar voz
    BARGE_IN_MIN_DURATION_MS = 200     # Duración mínima de voz para confirmar interrupción
    BARGE_IN_CONSECUTIVE_CHUNKS = 3    # Chunks consecutivos con voz para confirmar

    def __init__(self):
        self._consecutive_voice_chunks = 0

    def detect_interruption(self, audio_chunk: bytes) -> bool:
        """
        Detectar si hay una interrupción del caller.
        Retorna True si el caller está hablando (barge-in).
        """
        rms = self._calculate_rms(audio_chunk)

        if rms > self.BARGE_IN_THRESHOLD_RMS:
            self._consecutive_voice_chunks += 1
            if self._consecutive_voice_chunks >= self.BARGE_IN_CONSECUTIVE_CHUNKS:
                self._consecutive_voice_chunks = 0
                return True
        else:
            self._consecutive_voice_chunks = 0

        return False

    def reset(self):
        """Resetear estado del detector."""
        self._consecutive_voice_chunks = 0

    def _calculate_rms(self, chunk: bytes) -> float:
        """Calcular RMS del chunk de audio."""
        if len(chunk) < 2:
            return 0
        samples = struct.unpack(f"<{len(chunk) // 2}h", chunk)
        if not samples:
            return 0
        return (sum(s * s for s in samples) / len(samples)) ** 0.5
```

### 6. Endpoints de Voz — `app/api/v1/voice.py`

```python
# app/api/v1/voice.py
from fastapi import APIRouter, Request, Response, Depends, HTTPException
from app.services.voice.call_manager import CallManager

router = APIRouter(prefix="/api/v1/voice", tags=["Voice"])

call_manager = CallManager()

@router.post("/webhook/twilio")
async def twilio_voice_webhook(request: Request):
    """
    Webhook de Twilio Voice.
    Recibe notificaciones de llamada entrante y genera respuesta TwiML.
    """
    form_data = await request.form()
    payload = dict(form_data)
    headers = dict(request.headers)

    call_sid = payload.get("CallSid")
    call_status = payload.get("CallStatus")

    if call_status == "ringing":
        # Llamada entrante — responder con TwiML
        # Conectar WebSocket para audio streaming
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Mia" language="es-CO">
        Bienvenido. ¿En qué puedo ayudarle?
    </Say>
    <Connect>
        <Stream url="wss://{request.url.hostname}/api/v1/voice/ws/stream/{call_sid}" />
    </Connect>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    elif call_status == "completed":
        await call_manager.end_call(call_sid, reason="completed")
        return {"status": "ok"}

    elif call_status in ("busy", "failed", "no-answer"):
        await call_manager.end_call(call_sid, reason=call_status)
        return {"status": "ok"}

    return {"status": "ok"}

@router.post("/webhook/twilio/status")
async def twilio_status_callback(request: Request):
    """Callback de estado de llamadas Twilio."""
    form_data = await request.form()
    payload = dict(form_data)
    call_sid = payload.get("CallSid")
    status = payload.get("CallStatus")

    # Actualizar estado de llamada
    # ...
    return {"status": "ok"}

@router.post("/webhook/vonage/answer")
async def vonage_answer_webhook(request: Request):
    """
    Webhook de respuesta de Vonage.
    Retorna NCCO (Nexmo Call Control Object) con instrucciones.
    """
    payload = await request.json()

    ncco = [
        {
            "action": "talk",
            "text": "Bienvenido. ¿En qué puedo ayudarle?",
            "voiceName": "Conchita",
            "language": "es-CO",
        },
        {
            "action": "connect",
            "endpoint": [{
                "type": "websocket",
                "uri": f"wss://{request.url.hostname}/api/v1/voice/ws/stream/{payload.get('uuid', '')}",
                "content-type": "audio/l16;rate=8000",
            }],
        },
    ]

    return ncco

@router.post("/webhook/vonage/events")
async def vonage_events_webhook(request: Request):
    """Webhook de eventos de Vonage (status updates)."""
    payload = await request.json()
    status = payload.get("status")
    uuid = payload.get("uuid")

    if status == "completed":
        await call_manager.end_call(uuid, reason="completed")

    return {"status": "ok"}

@router.post("/outbound")
async def initiate_outbound_call(
    phone_number: str,
    script: str = None,
    current_user=Depends(get_current_user),
):
    """Iniciar una llamada saliente."""
    # Validar formato de número
    if not phone_number.startswith("+"):
        raise HTTPException(400, "El número debe incluir código de país (ej: +573001234567)")

    channel_config = await get_active_channel_config(
        current_user.client_id, "voice"
    )
    if not channel_config:
        raise HTTPException(400, "Canal de voz no configurado para este tenant")

    provider = ProviderFactory.get_provider("voice", channel_config.provider_config)
    result = await provider.send_message(
        recipient_id=phone_number,
        content=script or "Hola, le llamamos para...",
    )

    return result
```

### 7. WebSocket para Audio Streaming — `app/api/v1/voice_ws.py`

```python
# app/api/v1/voice_ws.py
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.services.voice.call_manager import CallManager
import json
import base64
from loguru import logger

router = APIRouter()

call_manager = CallManager()

@router.websocket("/api/v1/voice/ws/stream/{call_id}")
async def voice_stream_websocket(websocket: WebSocket, call_id: str):
    """
    WebSocket para audio streaming bidireccional.
    Twilio Media Streams / Vonage WebSocket envían audio aquí.
    """
    await websocket.accept()
    logger.info(f"WebSocket conectado para llamada {call_id}")

    try:
        # Determinar el formato basado en el primer mensaje
        is_twilio = False

        async for message in websocket.iter_text():
            data = json.loads(message)

            # Twilio Media Streams envía eventos JSON
            event_type = data.get("event")

            if event_type == "connected":
                # Twilio: conexión establecida
                is_twilio = True
                logger.info(f"Twilio Media Stream conectado: {call_id}")

            elif event_type == "start":
                # Twilio: inicio de stream con metadata
                stream_sid = data.get("streamSid")
                metadata = data.get("start", {})
                client_id = metadata.get("customParameters", {}).get("client_id")

                # Crear sesión de llamada
                await call_manager.handle_incoming_call(
                    call_id=call_id,
                    client_id=client_id,
                    caller_number=metadata.get("caller", ""),
                    channel_config={"language": "es"},
                )

            elif event_type == "media":
                # Twilio: chunk de audio en base64 (mulaw 8kHz)
                audio_payload = data.get("media", {}).get("payload", "")
                audio_bytes = base64.b64decode(audio_payload)

                # Convertir de mulaw a PCM si es necesario
                audio_pcm = mulaw_to_pcm(audio_bytes)
                await call_manager.handle_audio_chunk(call_id, audio_pcm)

            elif event_type == "stop":
                # Twilio: stream finalizado
                logger.info(f"Twilio Media Stream finalizado: {call_id}")
                await call_manager.end_call(call_id)
                break

            elif not event_type:
                # Vonage: audio crudo (sin wrapping JSON de eventos)
                # Vonage envía audio PCM directamente via WebSocket binario
                pass

    except WebSocketDisconnect:
        logger.info(f"WebSocket desconectado: {call_id}")
        await call_manager.end_call(call_id, reason="disconnected")
    except Exception as e:
        logger.error(f"Error en WebSocket de voz: {e}", extra={"call_id": call_id})
        await call_manager.end_call(call_id, reason="error")


def mulaw_to_pcm(mulaw_data: bytes) -> bytes:
    """
    Convertir audio mu-law (Twilio) a PCM lineal 16-bit.
    Twilio Media Streams usa mu-law encoding a 8kHz.
    """
    import audioop
    return audioop.ulaw2lin(mulaw_data, 2)  # 2 bytes = 16-bit PCM
```

### 8. Modelos de Datos

**8.1 Modelo CallRecord — `app/models/call_record.py`**

```python
# app/models/call_record.py
from sqlalchemy import Column, String, Integer, DateTime
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.models.base import Base, TimestampMixin
import uuid

class CallRecord(Base, TimestampMixin):
    __tablename__ = "call_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    contact_id = Column(UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id"))
    call_id = Column(String(200), nullable=False, unique=True)  # SID de Twilio o UUID de Vonage
    direction = Column(String(20), nullable=False)   # inbound, outbound
    status = Column(String(20), nullable=False)      # completed, busy, failed, no-answer, cancelled
    phone_from = Column(String(20))
    phone_to = Column(String(20))
    started_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime)
    duration_seconds = Column(Integer, default=0)
    transcript = Column(JSONB, default=[])            # Transcripción completa [{role, text, timestamp}]
    recording_url = Column(String(2048))              # URL de grabación (si habilitada)
    recording_duration = Column(Integer)              # Duración de grabación en segundos
    metadata = Column(JSONB, default={})              # Datos adicionales del provider
```

**8.2 Modelo ClinicalRecord — `app/models/clinical_record.py`**

```python
# app/models/clinical_record.py
from sqlalchemy import Column, String, Integer, DateTime, Date, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.models.base import Base, TimestampMixin
from app.core.encryption import EncryptedString  # Sprint 8
import uuid

class ClinicalRecord(Base, TimestampMixin):
    """
    Registro clínico RIPS (Registro Individual de Prestación de Servicios).
    Datos sensibles cifrados con pgcrypto (EncryptedString del Sprint 8).
    Cumple con Habeas Data (Ley 1581 de 2012).
    """
    __tablename__ = "clinical_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    contact_id = Column(UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id"))
    call_record_id = Column(UUID(as_uuid=True), ForeignKey("call_records.id"))

    # Datos del paciente (cifrados)
    patient_document_type = Column(String(5))    # CC, TI, CE, PA, RC, etc.
    patient_document_number = Column(EncryptedString, nullable=False)  # Cifrado
    patient_name = Column(EncryptedString)                              # Cifrado

    # Datos del servicio médico
    service_date = Column(Date, nullable=False)
    service_type = Column(String(50))        # consulta, procedimiento, urgencia, hospitalización
    specialty = Column(String(100))          # Especialidad médica
    provider_code = Column(String(20))       # Código del prestador

    # Codificación CIE-10 / CUPS
    diagnosis_codes = Column(JSONB, default=[])     # [{code: "J06.9", description: "...", type: "principal|relacionado"}]
    procedure_codes = Column(JSONB, default=[])     # [{code: "890201", description: "...", laterality: "..."}]
    diagnosis_type = Column(String(20))              # confirmado, presuntivo, impresión

    # Dictado médico
    raw_transcription = Column(EncryptedString)     # Transcripción cruda del audio (cifrada)
    structured_notes = Column(JSONB, default={})    # {subjective, objective, assessment, plan} (SOAP)
    medical_entities = Column(JSONB, default=[])    # Entidades médicas extraídas

    # RIPS específico
    rips_type = Column(String(5))             # AC (consulta), AP (procedimiento), AU (urgencia), AH (hospitalización)
    purpose_code = Column(String(5))          # Finalidad de la consulta (01-10)
    external_cause = Column(String(5))        # Causa externa
    discharge_status = Column(String(5))      # Estado de salida

    # Consentimiento y Habeas Data
    consent_given = Column(DateTime)           # Fecha/hora del consentimiento informado
    consent_type = Column(String(50))          # verbal, digital, written
    data_processing_authorized = Column(DateTime)  # Autorización tratamiento de datos

    # Estado
    status = Column(String(20), default="draft")  # draft, reviewed, signed, submitted
    reviewed_by = Column(UUID(as_uuid=True))       # Médico que revisó
    signed_at = Column(DateTime)
```

**8.3 Migraciones**

```sql
-- call_records
CREATE TABLE call_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL REFERENCES clients(id),
    contact_id UUID NOT NULL REFERENCES contacts(id),
    conversation_id UUID REFERENCES conversations(id),
    call_id VARCHAR(200) NOT NULL UNIQUE,
    direction VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL,
    phone_from VARCHAR(20),
    phone_to VARCHAR(20),
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    duration_seconds INTEGER DEFAULT 0,
    transcript JSONB DEFAULT '[]',
    recording_url VARCHAR(2048),
    recording_duration INTEGER,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE call_records ENABLE ROW LEVEL SECURITY;
CREATE POLICY call_records_isolation ON call_records
    USING (client_id = current_setting('app.current_client_id')::UUID);

CREATE INDEX idx_call_records_client_date ON call_records (client_id, started_at DESC);
CREATE INDEX idx_call_records_contact ON call_records (contact_id, started_at DESC);

-- clinical_records
CREATE TABLE clinical_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL REFERENCES clients(id),
    contact_id UUID NOT NULL REFERENCES contacts(id),
    conversation_id UUID REFERENCES conversations(id),
    call_record_id UUID REFERENCES call_records(id),
    patient_document_type VARCHAR(5),
    patient_document_number BYTEA NOT NULL,
    patient_name BYTEA,
    service_date DATE NOT NULL,
    service_type VARCHAR(50),
    specialty VARCHAR(100),
    provider_code VARCHAR(20),
    diagnosis_codes JSONB DEFAULT '[]',
    procedure_codes JSONB DEFAULT '[]',
    diagnosis_type VARCHAR(20),
    raw_transcription BYTEA,
    structured_notes JSONB DEFAULT '{}',
    medical_entities JSONB DEFAULT '[]',
    rips_type VARCHAR(5),
    purpose_code VARCHAR(5),
    external_cause VARCHAR(5),
    discharge_status VARCHAR(5),
    consent_given TIMESTAMPTZ,
    consent_type VARCHAR(50),
    data_processing_authorized TIMESTAMPTZ,
    status VARCHAR(20) DEFAULT 'draft',
    reviewed_by UUID REFERENCES users(id),
    signed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE clinical_records ENABLE ROW LEVEL SECURITY;
CREATE POLICY clinical_records_isolation ON clinical_records
    USING (client_id = current_setting('app.current_client_id')::UUID);

-- Políticas RLS adicionales: solo usuarios con rol 'medical' pueden leer registros clínicos
CREATE POLICY clinical_records_medical_access ON clinical_records
    FOR SELECT
    USING (
        client_id = current_setting('app.current_client_id')::UUID
        AND current_setting('app.current_user_role') IN ('medical', 'admin', 'super_admin')
    );

CREATE INDEX idx_clinical_records_client ON clinical_records (client_id, service_date DESC);
CREATE INDEX idx_clinical_records_patient ON clinical_records (client_id, patient_document_type, patient_document_number);

-- Trigger de auditoría para registros clínicos (obligatorio por Habeas Data)
-- Reutiliza el trigger de auditoría del Sprint 8
CREATE TRIGGER audit_clinical_records
    AFTER INSERT OR UPDATE OR DELETE ON clinical_records
    FOR EACH ROW EXECUTE FUNCTION audit_trigger_fn();
```

### 9. Agente Clínico — `app/agents/nodes/clinical.py`

**9.1 Definición del nodo**

```python
# app/agents/nodes/clinical.py
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage
from app.agents.state import ConversationState
from app.agents.tools.clinical_tools import (
    extract_medical_entities,
    code_cie10,
    code_cups,
    create_rips_record,
    search_cie10,
    search_cups,
    get_patient_history,
)

CLINICAL_SYSTEM_PROMPT = """Eres un agente clínico especializado en documentación médica colombiana.

Tu función es:
1. Escuchar dictados médicos (transcritos a texto por STT)
2. Extraer entidades médicas del dictado (síntomas, diagnósticos, procedimientos, medicamentos)
3. Codificar diagnósticos según CIE-10 (Clasificación Internacional de Enfermedades, 10a revisión)
4. Codificar procedimientos según CUPS (Clasificación Única de Procedimientos en Salud)
5. Generar registros RIPS estructurados (Resolución 3374 de 2000)
6. Estructurar notas clínicas en formato SOAP (Subjetivo, Objetivo, Evaluación, Plan)

Reglas ESTRICTAS:
- NUNCA sugieras diagnósticos. Solo codifica lo que el profesional de salud dicte explícitamente.
- Siempre solicita confirmación del profesional antes de guardar un registro.
- Los datos del paciente son CONFIDENCIALES (Ley 1581 de 2012, Habeas Data).
- Requiere consentimiento informado del paciente antes de crear registro.
- Si hay ambigüedad en un código CIE-10 o CUPS, presenta opciones al profesional.
- Registra siempre quién dictó y quién revisó.
- Los campos cifrados (documento, nombre) se manejan automáticamente por el sistema.

Formato RIPS requerido:
- AC (Consulta): tipo consulta, finalidad, causa externa, diagnósticos (principal + 3 relacionados)
- AP (Procedimiento): código CUPS, ámbito, finalidad, personal que realiza
- AU (Urgencia): causa externa, diagnóstico ingreso/egreso, estado salida
- AH (Hospitalización): diagnóstico ingreso, causa externa, días estancia

Estado actual: {conversation_context}
"""

async def clinical_agent_node(state: ConversationState) -> ConversationState:
    """Nodo del agente clínico en el grafo LangGraph."""

    agent_config = state.get("agent_configs", {}).get("clinical")
    if not agent_config:
        state["response"] = (
            "Lo siento, el módulo clínico no está habilitado para este tenant. "
            "Contacte al administrador."
        )
        state["next_node"] = "respond"
        return state

    # Verificar que el usuario tiene rol médico
    user_role = state.get("user_role")
    if user_role not in ("medical", "admin", "super_admin"):
        state["response"] = (
            "El módulo clínico requiere un usuario con rol médico autorizado."
        )
        state["next_node"] = "respond"
        return state

    # Verificar consentimiento Habeas Data
    habeas_data_status = state.get("habeas_data_consent")
    if not habeas_data_status:
        state["response"] = (
            "Antes de registrar datos clínicos, se requiere la autorización de tratamiento "
            "de datos personales del paciente (Ley 1581 de 2012). "
            "¿El paciente ha otorgado su consentimiento informado?"
        )
        state["next_node"] = "respond"
        state["awaiting_consent"] = True
        return state

    context = build_clinical_context(state)

    llm = ChatOpenAI(
        model=agent_config.get("model", "gpt-4o"),
        temperature=0.0,  # Cero creatividad en datos médicos
    )

    llm_with_tools = llm.bind_tools([
        extract_medical_entities,
        code_cie10,
        code_cups,
        create_rips_record,
        search_cie10,
        search_cups,
        get_patient_history,
    ])

    messages = [
        SystemMessage(content=CLINICAL_SYSTEM_PROMPT.format(
            conversation_context=context
        )),
        *state.get("messages", []),
    ]

    response = await run_agent_with_tools(llm_with_tools, messages, max_iterations=8)

    state["response"] = response.content
    state["next_node"] = "respond"
    state["tokens_used"] = state.get("tokens_used", 0) + response.usage_metadata.get("total_tokens", 0)

    return state


def build_clinical_context(state: ConversationState) -> str:
    """Construir contexto clínico para el prompt."""
    parts = []

    contact = state.get("contact")
    if contact:
        parts.append(f"Paciente: {contact.get('first_name', '')} {contact.get('last_name', '')}")

    clinical_draft = state.get("clinical_draft")
    if clinical_draft:
        parts.append(f"Registro en curso: {json.dumps(clinical_draft, ensure_ascii=False)}")

    # Historial de la sesión de dictado
    dictation_transcript = state.get("dictation_transcript")
    if dictation_transcript:
        parts.append(f"Transcripción del dictado actual:\n{dictation_transcript}")

    return "\n".join(parts) or "Sin contexto clínico previo."
```

**9.2 Tools Clínicos — `app/agents/tools/clinical_tools.py`**

```python
# app/agents/tools/clinical_tools.py
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from typing import Optional
import json

# Base de datos de códigos CIE-10 y CUPS (subset más frecuente en memoria,
# resto consultado de base de datos)
CIE10_COMMON = {
    "J06.9": "Infección aguda de las vías respiratorias superiores, no especificada",
    "J18.9": "Neumonía, organismo no especificado",
    "I10": "Hipertensión esencial (primaria)",
    "E11.9": "Diabetes mellitus tipo 2 sin complicaciones",
    "M54.5": "Dolor en la región lumbar",
    "K29.7": "Gastritis, no especificada",
    "N39.0": "Infección de vías urinarias, sitio no especificado",
    "A09": "Diarrea y gastroenteritis de presunto origen infeccioso",
    "R51": "Cefalea",
    "J45.9": "Asma, no especificada",
}

CUPS_COMMON = {
    "890201": "Consulta de primera vez por medicina general",
    "890301": "Consulta de control o de seguimiento por medicina general",
    "890401": "Consulta de primera vez por medicina especializada",
    "903841": "Hemograma IV (hemoglobina, hematocrito, recuento de eritrocitos)",
    "903856": "Glicemia en suero u otro fluido diferente a orina",
    "903868": "Parcial de orina (incluye sedimento)",
    "871121": "Radiografía de tórax (PA o AP)",
    "906919": "PCR cuantitativa",
}


@tool
async def extract_medical_entities(text: str) -> dict:
    """
    Extraer entidades médicas de texto transcrito de dictado.
    Identifica: síntomas, diagnósticos, medicamentos, procedimientos, signos vitales.

    Args:
        text: Texto transcrito del dictado médico
    """
    from openai import AsyncOpenAI
    client = AsyncOpenAI()

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": """Extrae entidades médicas del siguiente dictado médico.
Retorna un JSON con las categorías: symptoms, diagnoses, medications, procedures, vital_signs, allergies.
Cada entidad debe tener: text (como fue dicho), normalized (término médico estándar), negated (bool, si fue negado).
No inventes — solo extrae lo mencionado explícitamente.""",
            },
            {"role": "user", "content": text},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
    )

    entities = json.loads(response.choices[0].message.content)
    return entities


@tool
async def code_cie10(diagnosis: str, context: str = "") -> dict:
    """
    Codificar un diagnóstico en CIE-10.
    Retorna los códigos más probables con descripciones.

    Args:
        diagnosis: Diagnóstico a codificar (texto libre)
        context: Contexto clínico adicional para desambiguación
    """
    # 1. Buscar en códigos comunes (fast path)
    for code, description in CIE10_COMMON.items():
        if diagnosis.lower() in description.lower():
            return {
                "matches": [{"code": code, "description": description, "confidence": "high"}],
                "source": "common_codes",
            }

    # 2. Buscar en base de datos completa
    matches = await search_cie10_db(diagnosis)

    if not matches:
        # 3. Usar LLM como último recurso
        matches = await llm_cie10_lookup(diagnosis, context)

    return {
        "matches": matches[:5],  # Top 5 resultados
        "source": "database" if matches else "llm_assisted",
        "note": "Verifique el código seleccionado contra el listado oficial CIE-10 de la OMS.",
    }


@tool
async def code_cups(procedure: str) -> dict:
    """
    Codificar un procedimiento en CUPS (Clasificación Única de Procedimientos en Salud).

    Args:
        procedure: Procedimiento a codificar (texto libre)
    """
    # Búsqueda similar a CIE-10
    for code, description in CUPS_COMMON.items():
        if procedure.lower() in description.lower():
            return {
                "matches": [{"code": code, "description": description, "confidence": "high"}],
            }

    matches = await search_cups_db(procedure)

    return {
        "matches": matches[:5],
        "note": "Verifique el código CUPS contra la normativa vigente del Ministerio de Salud.",
    }


@tool
async def search_cie10(query: str, category: str = None) -> dict:
    """
    Buscar códigos CIE-10 por texto libre o categoría.

    Args:
        query: Texto de búsqueda
        category: Categoría CIE-10 (A00-B99, C00-D48, etc.) para filtrar
    """
    # Buscar en base de datos de CIE-10 con búsqueda full-text
    results = await search_cie10_db(query, category=category)
    return {"results": results[:10], "total": len(results)}


@tool
async def search_cups(query: str, group: str = None) -> dict:
    """
    Buscar códigos CUPS por texto libre o grupo.

    Args:
        query: Texto de búsqueda
        group: Grupo CUPS (ej: "89" para consultas, "87" para radiología)
    """
    results = await search_cups_db(query, group=group)
    return {"results": results[:10], "total": len(results)}


@tool
async def create_rips_record(
    patient_document_type: str,
    patient_document_number: str,
    service_date: str,
    service_type: str,
    rips_type: str,
    diagnosis_codes: list[dict],
    procedure_codes: list[dict] = None,
    purpose_code: str = "01",
    external_cause: str = None,
    notes: dict = None,
) -> dict:
    """
    Crear un registro RIPS (Registro Individual de Prestación de Servicios).
    REQUIERE confirmación del profesional antes de guardar.

    Args:
        patient_document_type: Tipo documento (CC, TI, CE, PA, RC)
        patient_document_number: Número de documento
        service_date: Fecha del servicio (YYYY-MM-DD)
        service_type: Tipo de servicio (consulta, procedimiento, urgencia, hospitalización)
        rips_type: Tipo RIPS (AC, AP, AU, AH)
        diagnosis_codes: [{code, description, type: principal|relacionado}]
        procedure_codes: [{code, description, laterality?}]
        purpose_code: Código de finalidad (01-10)
        external_cause: Código de causa externa
        notes: Notas SOAP {subjective, objective, assessment, plan}
    """
    # Validar tipo de documento
    valid_doc_types = ["CC", "TI", "CE", "PA", "RC", "MS", "AS", "NU"]
    if patient_document_type not in valid_doc_types:
        return {"error": f"Tipo de documento inválido. Válidos: {valid_doc_types}"}

    # Validar que hay al menos un diagnóstico principal
    principal = [d for d in diagnosis_codes if d.get("type") == "principal"]
    if not principal:
        return {"error": "Se requiere al menos un diagnóstico principal."}

    # Validar tipo RIPS
    valid_rips = ["AC", "AP", "AU", "AH"]
    if rips_type not in valid_rips:
        return {"error": f"Tipo RIPS inválido. Válidos: {valid_rips}"}

    # Crear registro (draft — requiere revisión)
    record = {
        "patient_document_type": patient_document_type,
        "patient_document_number": patient_document_number,
        "service_date": service_date,
        "service_type": service_type,
        "rips_type": rips_type,
        "diagnosis_codes": diagnosis_codes,
        "procedure_codes": procedure_codes or [],
        "purpose_code": purpose_code,
        "external_cause": external_cause,
        "structured_notes": notes or {},
        "status": "draft",
    }

    # Guardar en DB (datos sensibles cifrados automáticamente por EncryptedString)
    # ...

    return {
        "success": True,
        "record_id": "...",
        "status": "draft",
        "message": "Registro RIPS creado en borrador. Requiere revisión y firma del profesional.",
        "summary": {
            "patient": f"{patient_document_type} {patient_document_number[:4]}****",
            "date": service_date,
            "type": rips_type,
            "main_diagnosis": principal[0]["code"] if principal else "N/A",
            "procedures": len(procedure_codes or []),
        },
    }


@tool
async def get_patient_history(
    patient_document_type: str,
    patient_document_number: str,
    limit: int = 10,
) -> dict:
    """
    Obtener historial clínico del paciente (registros RIPS anteriores).
    Solo accesible por usuarios con rol médico.

    Args:
        patient_document_type: Tipo de documento
        patient_document_number: Número de documento
        limit: Máximo de registros a retornar
    """
    # Buscar registros del paciente (cifrados, se descifran en lectura)
    # ...
    return {
        "patient": f"{patient_document_type} ****",
        "records": [],
        "total_count": 0,
        "note": "El historial clínico es confidencial (Ley 1581 de 2012). "
                "Solo accesible por personal médico autorizado.",
    }
```

### 10. Habeas Data — `app/core/habeas_data.py`

```python
# app/core/habeas_data.py
"""
Utilidades de cumplimiento de Habeas Data (Ley 1581 de 2012 — Colombia).
Aplica al tratamiento de datos personales sensibles, especialmente datos de salud.
"""
from datetime import datetime
from uuid import UUID
from loguru import logger

class HabeasDataCompliance:
    """
    Verificaciones y utilidades para cumplimiento de Habeas Data.

    Principios clave (Art. 4, Ley 1581 de 2012):
    - Finalidad: Datos tratados con propósito legítimo
    - Libertad: Tratamiento requiere consentimiento previo
    - Veracidad: Datos deben ser veraces y actualizados
    - Transparencia: Titular informado sobre tratamiento
    - Acceso y circulación restringida: Solo acceso autorizado
    - Seguridad: Medidas técnicas para proteger datos
    - Confidencialidad: Reserva de la información
    """

    SENSITIVE_DATA_CATEGORIES = [
        "health",           # Datos de salud
        "biometric",        # Datos biométricos
        "sexual",           # Orientación sexual
        "racial",           # Origen racial o étnico
        "political",        # Opiniones políticas
        "religious",        # Convicciones religiosas
        "union_membership", # Pertenencia a sindicatos
    ]

    @staticmethod
    async def verify_consent(
        db,
        contact_id: UUID,
        data_category: str,
    ) -> dict:
        """
        Verificar que existe consentimiento para tratar datos sensibles.
        Art. 6 — Los datos sensibles solo pueden tratarse con consentimiento explícito.
        """
        from app.models.clinical_record import ClinicalRecord

        # Buscar consentimiento activo
        result = await db.execute(
            select(ClinicalRecord)
            .where(
                ClinicalRecord.contact_id == contact_id,
                ClinicalRecord.data_processing_authorized.isnot(None),
            )
            .order_by(ClinicalRecord.data_processing_authorized.desc())
            .limit(1)
        )
        record = result.scalar_one_or_none()

        if record and record.data_processing_authorized:
            return {
                "has_consent": True,
                "consent_date": record.data_processing_authorized.isoformat(),
                "consent_type": record.consent_type,
            }

        return {
            "has_consent": False,
            "required_action": (
                "Se requiere autorización del titular para el tratamiento de datos "
                f"personales sensibles (categoría: {data_category}). "
                "Ley 1581 de 2012, Art. 6."
            ),
        }

    @staticmethod
    async def register_consent(
        db,
        contact_id: UUID,
        client_id: UUID,
        consent_type: str,  # verbal, digital, written
        authorized_by: UUID,
    ) -> dict:
        """Registrar consentimiento de tratamiento de datos."""
        # Actualizar registros del contacto
        # ...
        logger.info(
            "Consentimiento Habeas Data registrado",
            extra={
                "contact_id": str(contact_id),
                "consent_type": consent_type,
                "authorized_by": str(authorized_by),
            },
        )
        return {
            "registered": True,
            "timestamp": datetime.utcnow().isoformat(),
            "consent_type": consent_type,
        }

    @staticmethod
    async def export_patient_data(db, contact_id: UUID, client_id: UUID) -> dict:
        """
        Exportar todos los datos de un paciente (derecho de acceso, Art. 12).
        Similar al endpoint RGPD del Sprint 8 pero específico para datos clínicos.
        """
        from app.models.clinical_record import ClinicalRecord
        from app.models.call_record import CallRecord

        clinical_records = await db.execute(
            select(ClinicalRecord)
            .where(
                ClinicalRecord.client_id == client_id,
                ClinicalRecord.contact_id == contact_id,
            )
            .order_by(ClinicalRecord.service_date.desc())
        )

        call_records = await db.execute(
            select(CallRecord)
            .where(
                CallRecord.client_id == client_id,
                CallRecord.contact_id == contact_id,
            )
            .order_by(CallRecord.started_at.desc())
        )

        return {
            "contact_id": str(contact_id),
            "export_date": datetime.utcnow().isoformat(),
            "clinical_records": [
                {
                    "id": str(r.id),
                    "service_date": r.service_date.isoformat(),
                    "service_type": r.service_type,
                    "diagnosis_codes": r.diagnosis_codes,
                    "procedure_codes": r.procedure_codes,
                    "status": r.status,
                }
                for r in clinical_records.scalars()
            ],
            "call_records": [
                {
                    "id": str(r.id),
                    "date": r.started_at.isoformat(),
                    "duration": r.duration_seconds,
                    "direction": r.direction,
                }
                for r in call_records.scalars()
            ],
            "legal_basis": "Ley 1581 de 2012, Art. 12 — Derecho de acceso",
        }

    @staticmethod
    async def anonymize_patient_data(db, contact_id: UUID, client_id: UUID) -> dict:
        """
        Anonimizar datos de un paciente (derecho de supresión, Art. 12, literal e).
        Los registros clínicos se retienen con datos anonimizados por razones epidemiológicas.
        """
        from app.models.clinical_record import ClinicalRecord

        # Anonimizar — reemplazar datos identificables pero conservar datos epidemiológicos
        await db.execute(
            update(ClinicalRecord)
            .where(
                ClinicalRecord.client_id == client_id,
                ClinicalRecord.contact_id == contact_id,
            )
            .values(
                patient_document_number=encrypt("ANONIMIZADO"),
                patient_name=encrypt("ANONIMIZADO"),
                raw_transcription=None,
            )
        )
        await db.commit()

        logger.warning(
            "Datos de paciente anonimizados (Habeas Data)",
            extra={"contact_id": str(contact_id)},
        )

        return {
            "anonymized": True,
            "contact_id": str(contact_id),
            "legal_basis": "Ley 1581 de 2012, Art. 12, literal e — Derecho de supresión",
            "note": "Datos epidemiológicos conservados de forma anónima.",
        }
```

### 11. Actualizar Intent Routing e Integración en Grafo

```python
# En app/agents/nodes/intent_routing.py (agregar intents clínicos)

INTENTS = [
    # Existentes
    "general", "faq", "appointment", "handoff", "greeting", "farewell",
    "financial", "marketing", "invoice_request",
    # Nuevos Sprint 13
    "clinical",           # "dictado médico", "registrar consulta", "RIPS"
    "clinical_dictation", # "transcribir dictado", "nota clínica"
    "clinical_coding",    # "codificar diagnóstico", "buscar CIE-10"
]

def route_by_intent(state: ConversationState) -> str:
    intent = state.get("intent")
    agent_configs = state.get("agent_configs", {})

    # Routing clínico (solo si habilitado y rol médico)
    if intent in ("clinical", "clinical_dictation", "clinical_coding"):
        if "clinical" in agent_configs:
            return "clinical_agent"
        return "rag_query"

    # ... routing existente (financial, marketing, etc.)
```

```python
# En app/agents/graph.py (agregar nodos de voz y clínico)

from app.agents.nodes.clinical import clinical_agent_node

# Agregar nodo al grafo
graph.add_node("clinical_agent", clinical_agent_node)

# Edges condicionales
graph.add_conditional_edges(
    "intent_routing",
    route_by_intent,
    {
        # ... edges existentes
        "clinical_agent": "clinical_agent",
    },
)
graph.add_edge("clinical_agent", "respond")
```

### 12. Actualizar ProviderFactory

```python
# En app/services/messaging/factory.py
from app.services.messaging.voice_provider import VoiceProvider

class ProviderFactory:
    _providers = {
        "whatsapp": WhatsAppProvider,
        "telegram": TelegramProvider,
        "meta": MetaProvider,          # Instagram DM + Facebook Messenger
        "webchat": WebchatProvider,
        "email": EmailProvider,
        "voice": VoiceProvider,        # Nuevo Sprint 13
    }
```

### 13. Tests

**13.1 Tests VoiceProvider — `tests/unit/test_voice_provider.py`**

```python
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.services.messaging.voice_provider import VoiceProvider, VoiceBackend

class TestVoiceProvider:
    @pytest.fixture
    def twilio_provider(self):
        return VoiceProvider({
            "backend": "twilio",
            "account_sid": "ACtest123",
            "auth_token": "test_token",
            "phone_number": "+573001234567",
            "webhook_url": "https://example.com",
        })

    @pytest.fixture
    def vonage_provider(self):
        return VoiceProvider({
            "backend": "vonage",
            "api_key": "test_key",
            "api_secret": "test_secret",
            "phone_number": "+573001234567",
            "webhook_url": "https://example.com",
        })

    def test_channel_constraints(self, twilio_provider):
        """Verificar restricciones del canal de voz."""
        constraints = twilio_provider.get_channel_constraints()
        assert constraints.supports_audio is True
        assert constraints.supports_media is False
        assert constraints.supports_buttons is False

    @pytest.mark.asyncio
    async def test_parse_twilio_webhook_ringing(self, twilio_provider):
        """Parsear webhook de llamada entrante Twilio."""
        payload = {
            "CallSid": "CA123",
            "From": "+573001234567",
            "To": "+573009876543",
            "CallStatus": "ringing",
            "Direction": "inbound",
        }
        result = await twilio_provider.parse_webhook(payload, {})
        assert result.sender_id == "+573001234567"
        assert result.message_id == "CA123"
        assert result.channel == "voice"

    @pytest.mark.asyncio
    async def test_parse_twilio_webhook_speech_result(self, twilio_provider):
        """Parsear resultado de <Gather> con reconocimiento de voz."""
        payload = {
            "CallSid": "CA123",
            "From": "+573001234567",
            "CallStatus": "in-progress",
            "SpeechResult": "Necesito agendar una cita para mañana",
            "Confidence": "0.92",
        }
        result = await twilio_provider.parse_webhook(payload, {})
        assert "agendar una cita" in result.content

    @pytest.mark.asyncio
    async def test_validate_twilio_signature(self, twilio_provider):
        """Verificar firma de webhook Twilio."""
        # La verificación real requiere datos firmados correctos
        result = await twilio_provider.validate_signature(
            b"CallSid=CA123&From=%2B573001234567",
            {"x-twilio-signature": "invalid"},
            "test_token",
        )
        assert result is False  # Firma inválida

    @pytest.mark.asyncio
    async def test_initiate_outbound_call_twilio(self, twilio_provider):
        """Iniciar llamada saliente via Twilio."""
        with patch("httpx.AsyncClient") as mock_client:
            mock_response = MagicMock(
                status_code=201,
                json=MagicMock(return_value={"sid": "CA456"})
            )
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.post = AsyncMock(return_value=mock_response)

            result = await twilio_provider.send_message("+573009876543", "Hola, le llamamos de...")
            assert result["success"] is True
            assert result["call_sid"] == "CA456"
```

**13.2 Tests STT — `tests/unit/test_stt_streaming.py`**

```python
import pytest
import struct
from app.services.voice.stt_streaming import STTSession

class TestSTTStreaming:
    def test_rms_calculation_silence(self):
        """Audio silencioso tiene RMS bajo."""
        session = STTSession.__new__(STTSession)
        silence = b"\x00" * 320  # 10ms de silencio a 16kHz
        rms = session._calculate_rms(silence)
        assert rms < 100

    def test_rms_calculation_voice(self):
        """Audio con voz tiene RMS alto."""
        session = STTSession.__new__(STTSession)
        # Generar onda sinusoidal simple
        import math
        samples = [int(10000 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(160)]
        voice = struct.pack(f"<{len(samples)}h", *samples)
        rms = session._calculate_rms(voice)
        assert rms > 1000

    def test_pcm_to_wav(self):
        """Conversión PCM a WAV genera header correcto."""
        session = STTSession.__new__(STTSession)
        session.AUDIO_SAMPLE_RATE = 8000
        pcm_data = b"\x00" * 16000  # 1 segundo de silencio
        wav_data = session._pcm_to_wav(pcm_data)
        assert wav_data[:4] == b"RIFF"
        assert wav_data[8:12] == b"WAVE"
```

**13.3 Tests Agente Clínico — `tests/unit/test_clinical_agent.py`**

```python
import pytest
from unittest.mock import AsyncMock, patch
from app.agents.nodes.clinical import clinical_agent_node
from app.agents.tools.clinical_tools import code_cie10, code_cups

class TestClinicalAgent:
    @pytest.mark.asyncio
    async def test_clinical_node_disabled(self):
        """Si agente clínico no está habilitado, retorna mensaje de error."""
        state = {"agent_configs": {}, "last_user_message": "Dictado médico"}
        result = await clinical_agent_node(state)
        assert "no está habilitado" in result["response"]

    @pytest.mark.asyncio
    async def test_clinical_node_requires_medical_role(self):
        """Solo usuarios con rol médico pueden usar el agente clínico."""
        state = {
            "agent_configs": {"clinical": {"model": "gpt-4o"}},
            "user_role": "agent",  # No tiene rol médico
        }
        result = await clinical_agent_node(state)
        assert "rol médico" in result["response"]

    @pytest.mark.asyncio
    async def test_clinical_node_requires_consent(self):
        """Se requiere consentimiento Habeas Data antes de registrar datos."""
        state = {
            "agent_configs": {"clinical": {"model": "gpt-4o"}},
            "user_role": "medical",
            "habeas_data_consent": None,
        }
        result = await clinical_agent_node(state)
        assert "consentimiento" in result["response"].lower() or "autorización" in result["response"].lower()
        assert result.get("awaiting_consent") is True

    @pytest.mark.asyncio
    async def test_code_cie10_common_diagnosis(self):
        """Codificar diagnóstico común retorna código correcto."""
        result = await code_cie10.ainvoke({
            "diagnosis": "Hipertensión esencial"
        })
        assert any(m["code"] == "I10" for m in result["matches"])

    @pytest.mark.asyncio
    async def test_code_cups_common_procedure(self):
        """Codificar procedimiento común retorna código correcto."""
        result = await code_cups.ainvoke({
            "procedure": "Consulta de primera vez por medicina general"
        })
        assert any(m["code"] == "890201" for m in result["matches"])

    @pytest.mark.asyncio
    async def test_create_rips_requires_principal_diagnosis(self):
        """RIPS requiere al menos un diagnóstico principal."""
        result = await create_rips_record.ainvoke({
            "patient_document_type": "CC",
            "patient_document_number": "1234567890",
            "service_date": "2025-01-15",
            "service_type": "consulta",
            "rips_type": "AC",
            "diagnosis_codes": [
                {"code": "J06.9", "description": "IRAS", "type": "relacionado"}
            ],
        })
        assert "error" in result
        assert "principal" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_create_rips_valid(self):
        """RIPS válido crea registro en borrador."""
        result = await create_rips_record.ainvoke({
            "patient_document_type": "CC",
            "patient_document_number": "1234567890",
            "service_date": "2025-01-15",
            "service_type": "consulta",
            "rips_type": "AC",
            "diagnosis_codes": [
                {"code": "I10", "description": "Hipertensión esencial", "type": "principal"}
            ],
            "purpose_code": "01",
        })
        assert result["success"] is True
        assert result["status"] == "draft"
```

**13.4 Tests Habeas Data — `tests/unit/test_habeas_data.py`**

```python
import pytest
from app.core.habeas_data import HabeasDataCompliance

class TestHabeasDataCompliance:
    @pytest.mark.asyncio
    async def test_verify_consent_missing(self, db, test_contact):
        """Sin consentimiento retorna has_consent=False."""
        result = await HabeasDataCompliance.verify_consent(
            db, test_contact.id, "health"
        )
        assert result["has_consent"] is False
        assert "Ley 1581" in result["required_action"]

    @pytest.mark.asyncio
    async def test_verify_consent_exists(self, db, test_contact_with_consent):
        """Con consentimiento registrado retorna has_consent=True."""
        result = await HabeasDataCompliance.verify_consent(
            db, test_contact_with_consent.id, "health"
        )
        assert result["has_consent"] is True

    @pytest.mark.asyncio
    async def test_export_patient_data(self, db, test_contact, test_clinical_records):
        """Exportar datos del paciente incluye registros clínicos y llamadas."""
        result = await HabeasDataCompliance.export_patient_data(
            db, test_contact.id, test_contact.client_id
        )
        assert "clinical_records" in result
        assert "call_records" in result
        assert "Ley 1581" in result["legal_basis"]

    @pytest.mark.asyncio
    async def test_anonymize_patient_data(self, db, test_contact, test_clinical_records):
        """Anonimizar datos reemplaza datos identificables pero conserva epidemiológicos."""
        result = await HabeasDataCompliance.anonymize_patient_data(
            db, test_contact.id, test_contact.client_id
        )
        assert result["anonymized"] is True
```

**13.5 Test de flujo integrado — `tests/integration/test_voice_flow.py`**

```python
@pytest.mark.integration
class TestVoiceFlow:
    async def test_incoming_call_full_flow(self, db, test_tenant, test_voice_config):
        """Flujo completo: llamada entrante → STT → agente → TTS → fin."""
        # 1. Simular webhook de llamada entrante
        # 2. Verificar que se creó sesión STT
        # 3. Enviar chunks de audio
        # 4. Verificar transcripción
        # 5. Verificar respuesta del agente
        # 6. Verificar TTS generado
        # 7. Finalizar llamada
        # 8. Verificar call_record guardado con transcripción
        pass

    async def test_barge_in_detection(self, db, test_tenant):
        """Interrupción del caller detiene TTS y escucha."""
        # 1. Simular que el agente está hablando (TTS activo)
        # 2. Enviar chunks de audio con voz del caller
        # 3. Verificar que se cancela el TTS
        # 4. Verificar que se activa STT
        pass

    async def test_clinical_dictation_flow(self, db, test_tenant, test_medical_user):
        """Flujo de dictado médico: voz → transcripción → entidades → RIPS."""
        # 1. Simular dictado médico transcrito
        # 2. Verificar extracción de entidades
        # 3. Verificar codificación CIE-10
        # 4. Verificar creación de registro RIPS
        # 5. Verificar que datos sensibles están cifrados
        pass
```

## Criterios de Aceptación

| # | Criterio | Verificación |
|---|---|---|
| 1 | Llamada entrante procesada correctamente | Webhook Twilio/Vonage → CallManager → sesión STT creada |
| 2 | STT streaming transcribe audio | Chunks de audio → transcripción parcial/final via Whisper |
| 3 | TTS sintetiza respuesta del agente | Texto del agente → audio PCM → enviado al caller |
| 4 | Barge-in funciona (interrupción) | Caller habla durante TTS → TTS se detiene → STT activo |
| 5 | Registro de llamada guardado | call_records con transcripción completa y duración |
| 6 | VoiceProvider implementa 5 métodos ABC | parse_webhook, validate_signature, send_message, send_template, get_channel_constraints |
| 7 | Agente clínico extrae entidades médicas | Dictado → síntomas, diagnósticos, medicamentos identificados |
| 8 | Codificación CIE-10 funcional | "Hipertensión esencial" → I10 |
| 9 | Codificación CUPS funcional | "Consulta primera vez medicina general" → 890201 |
| 10 | RIPS generado correctamente | Registro con diagnóstico principal, tipo RIPS, finalidad |
| 11 | Consentimiento Habeas Data requerido | Sin consentimiento → solicitud antes de registrar datos |
| 12 | Datos sensibles cifrados | patient_document_number, patient_name, raw_transcription → BYTEA cifrado |
| 13 | Auditoría en registros clínicos | INSERT/UPDATE/DELETE → audit_logs (trigger PostgreSQL) |
| 14 | Solo rol médico accede a datos clínicos | RLS policy adicional en clinical_records |
| 15 | Exportación de datos del paciente | Endpoint que retorna todos los datos clínicos y llamadas |

## Notas Técnicas

- **VoiceProvider dual-backend**: Se implementa Twilio como backend principal y Vonage como alternativa. La selección se hace via `provider_config.backend`. Ambos exponen la misma interfaz ABC, por lo que el cambio es transparente para el resto del sistema.
- **STT con Whisper, no streaming nativo**: Whisper API no soporta streaming verdadero (solo procesamiento por lotes). La estrategia es acumular audio hasta detectar silencio (VAD), enviar el utterance completo a Whisper y retornar la transcripción. La latencia es aceptable (~500ms por utterance) para conversaciones de voz naturales.
- **Audio 8kHz PCM**: Las llamadas telefónicas operan a 8kHz (calidad telefónica estándar). Twilio usa mu-law encoding que requiere conversión a PCM. Vonage puede enviar PCM directamente.
- **Barge-in simple**: La detección de interrupciones usa VAD basado en energía RMS. Para producción, considerar modelos de VAD más sofisticados como Silero-VAD (pero requiere GPU o latencia adicional).
- **Datos clínicos con EncryptedString**: Los campos `patient_document_number`, `patient_name` y `raw_transcription` usan el tipo `EncryptedString` del Sprint 8 (pgcrypto). Los datos se almacenan como BYTEA cifrado en PostgreSQL.
- **RIPS — solo Colombia**: La estructura RIPS es específica de Colombia (Resolución 3374 de 2000). Para otros países, crear implementaciones alternativas de los tools clínicos manteniendo la misma interfaz.
- **CIE-10 y CUPS como datos de referencia**: Los códigos CIE-10 y CUPS más frecuentes se cargan en memoria para búsqueda rápida. La base de datos completa se carga en una tabla de referencia (no por tenant — son catálogos universales). Se recomienda indexar con full-text search (tsvector) para búsqueda eficiente.
- **Habeas Data NO es RGPD**: Aunque similares en espíritu, la Ley 1581 de 2012 tiene requisitos específicos colombianos. El Sprint 8 implementó RGPD (europeo); este sprint agrega las particularidades de Habeas Data para datos de salud.
- **temperature=0.0 en agente clínico**: El LLM del agente clínico usa temperatura cero. NUNCA generar datos médicos creativos — solo estructurar y codificar lo que el profesional dicta explícitamente.

## Dependencias

| Dependencia | Versión | Propósito |
|---|---|---|
| twilio | >=9.0.0 | SDK de Twilio Voice (alternativa a httpx directo) |
| vonage (nexmo) | >=3.0.0 | SDK de Vonage Voice (alternativa a httpx directo) |
| openai | >=1.30.0 | Whisper API (STT) y TTS API |
| google-cloud-texttospeech | >=2.16.0 | Google Cloud TTS (backend alternativo) |
| PyJWT | >=2.8.0 | Verificación JWT de Vonage |
| audioop-lts | >=0.2.0 | Conversión mu-law a PCM (audioop deprecated en Python 3.13) |
| sentence-transformers | (existente) | Reutilizado del Sprint 12 |
