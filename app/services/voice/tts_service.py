"""Text-to-Speech del canal de voz con OpenAI TTS (Sprint 13, Dev A).

Convierte la respuesta del agente en audio listo para Twilio Media Streams:
mu-law a 8 kHz. OpenAI devuelve PCM de 16 bits a 24 kHz con
`response_format="pcm"`; se remuestrea (24 -> 8 kHz, factor 3) y se codifica
aca (`audio.py`).

Desviaciones sobre el spec (§3)
-------------------------------
- **Sin backend de Google Cloud TTS.** Seria una dependencia nueva
  (`google-cloud-texttospeech`) con sus propias credenciales para una segunda
  opcion que nadie pidio: el proyecto ya habla con OpenAI. La interfaz es una
  sola funcion, asi que sumarlo despues no toca a quien la usa.
- **Textos largos en trozos.** La API acepta hasta 4096 caracteres por
  llamada; una respuesta mas larga (poco probable por voz, pero posible) daba
  un 400 y el cliente se quedaba sin respuesta. Se parte por oraciones.
- **Voz y modelo por configuracion** (`VOICE_TTS_VOICE`, `VOICE_TTS_MODEL`), no
  por un diccionario de "genero" en el codigo.
"""

import logging
import re

import openai
from openai import AsyncOpenAI

from app.core.config import get_settings
from app.core.metrics import record_tokens
from app.services.voice.audio import (
    OPENAI_TTS_SAMPLE_RATE,
    SAMPLE_RATE,
    downsample_pcm16,
    pcm16_to_mulaw,
)

logger = logging.getLogger(__name__)

#: Tope de caracteres por llamada a la API de OpenAI TTS (limite documentado: 4096).
MAX_CHARS_PER_REQUEST = 4000

_FIN_DE_ORACION = re.compile(r"(?<=[.!?¿¡;:])\s+")


class TTSError(RuntimeError):
    """No se pudo sintetizar la respuesta."""


def split_text(text: str, limit: int = MAX_CHARS_PER_REQUEST) -> list[str]:
    """Parte un texto en trozos de hasta `limit` caracteres, por oraciones.

    Una oracion que por si sola supera el limite se corta en seco: es mejor
    una pausa rara que no responder.

    Args:
        text: Texto a sintetizar.
        limit: Caracteres maximos por trozo.

    Returns:
        Los trozos, sin vacios.
    """
    trozos: list[str] = []
    actual = ""
    for oracion in _FIN_DE_ORACION.split(text.strip()):
        while len(oracion) > limit:
            if actual:
                trozos.append(actual)
                actual = ""
            trozos.append(oracion[:limit])
            oracion = oracion[limit:]
        candidato = f"{actual} {oracion}".strip() if actual else oracion
        if len(candidato) > limit:
            trozos.append(actual)
            actual = oracion
        else:
            actual = candidato
    if actual:
        trozos.append(actual)
    return [t for t in trozos if t.strip()]


async def synthesize_mulaw(text: str, client_id: str) -> bytes:
    """Sintetiza un texto como audio mu-law a 8 kHz.

    Args:
        text: Respuesta del agente.
        client_id: Tenant al que se imputa el costo.

    Returns:
        El audio completo, listo para partir en tramas de Twilio.

    Raises:
        TTSError: Si falta la credencial o la API fallo.
    """
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise TTSError("OPENAI_API_KEY no configurada")

    factor = OPENAI_TTS_SAMPLE_RATE // SAMPLE_RATE
    audio = bytearray()
    async with AsyncOpenAI(
        api_key=settings.OPENAI_API_KEY,
        timeout=settings.VOICE_TTS_TIMEOUT_SECONDS,
        max_retries=1,
    ) as cliente:
        for trozo in split_text(text):
            try:
                respuesta = await cliente.audio.speech.create(
                    model=settings.VOICE_TTS_MODEL,
                    voice=settings.VOICE_TTS_VOICE,
                    input=trozo,
                    response_format="pcm",
                )
            except openai.APIError as exc:
                raise TTSError(f"Fallo de OpenAI TTS: {type(exc).__name__}") from exc
            audio.extend(pcm16_to_mulaw(downsample_pcm16(respuesta.content, factor)))

    record_tokens(
        client_id,
        settings.VOICE_TTS_MODEL,
        "voice_synthesis",
        0,
        0,
        cost_usd=len(text) / 1000 * settings.VOICE_TTS_COST_PER_1K_CHARS_USD,
    )
    return bytes(audio)
