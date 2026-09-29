"""Utilidades de audio del canal de voz (Sprint 13, Dev A).

Twilio Media Streams manda y recibe **mu-law (G.711) a 8 kHz, mono**, en
tramas de 20 ms (160 bytes). El resto de la plataforma trabaja con PCM lineal
de 16 bits: Whisper recibe WAV y OpenAI TTS devuelve PCM a 24 kHz. Este modulo
es la frontera entre los dos mundos.

Por que no `audioop`
--------------------
El spec convierte con `audioop.ulaw2lin()`. `audioop` esta deprecado desde
Python 3.11 y **se elimino en 3.13** (PEP 594): el dia que la imagen suba de
version, el canal de voz dejaria de arrancar. El codec G.711 es una tabla de
256 entradas para decodificar y una funcion por tramos para codificar; se
implementa aca, sin dependencias, y los tests lo comparan contra `audioop`
mientras siga disponible.

Todo es sincrono y de CPU: una trama de 20 ms son 160 muestras, microsegundos
de trabajo. Lo unico mas pesado es codificar una respuesta entera de TTS
(decenas de miles de muestras), que sigue estando en el orden de milisegundos.
"""

from __future__ import annotations

import io
import math
import wave
from array import array
from sys import byteorder

#: Frecuencia de muestreo de la telefonia (G.711).
SAMPLE_RATE = 8000

#: Frecuencia del PCM que devuelve OpenAI TTS con `response_format="pcm"`.
OPENAI_TTS_SAMPLE_RATE = 24000

#: Bytes de una trama de 20 ms en mu-law (1 byte por muestra).
FRAME_BYTES_MULAW = 160

#: Duracion de una trama de Twilio.
FRAME_MS = 20

_BIAS = 0x84
_BIAS_14 = 0x21
_CLIP_14 = 8159
_FIN_DE_SEGMENTO = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)


def _mulaw_a_lineal(byte: int) -> int:
    """Decodifica una muestra mu-law (G.711).

    Args:
        byte: Muestra codificada (0-255).

    Returns:
        Muestra PCM de 16 bits con signo.
    """
    valor = ~byte & 0xFF
    signo = valor & 0x80
    exponente = (valor >> 4) & 0x07
    mantisa = valor & 0x0F
    muestra = (((mantisa << 3) + _BIAS) << exponente) - _BIAS
    return -muestra if signo else muestra


def _lineal_a_mulaw(muestra: int) -> int:
    """Codifica una muestra PCM de 16 bits en mu-law (G.711).

    Variante de 14 bits de la implementacion de referencia de Sun (la misma
    que usa `audioop`): la muestra se desplaza con signo antes de tomar el
    valor absoluto. Con la variante de 16 bits, unas pocas muestras negativas
    en el borde de cada segmento caen en el codigo vecino; las dos son G.711
    valido, pero asi el resultado es identico al de la referencia.

    Args:
        muestra: Muestra con signo, entre -32768 y 32767.

    Returns:
        Byte mu-law.
    """
    valor = muestra >> 2
    if valor < 0:
        valor, mascara = -valor, 0x7F
    else:
        mascara = 0xFF
    valor = min(valor, _CLIP_14) + _BIAS_14
    for segmento, tope in enumerate(_FIN_DE_SEGMENTO):
        if valor <= tope:
            return ((segmento << 4) | ((valor >> (segmento + 1)) & 0x0F)) ^ mascara
    return 0x7F ^ mascara


# Tablas precalculadas al importar (256 y 65536 entradas, ~10 ms).
_DECODIFICAR: tuple[int, ...] = tuple(_mulaw_a_lineal(b) for b in range(256))
_CODIFICAR: bytes = bytes(_lineal_a_mulaw(m - 65536 if m >= 32768 else m) for m in range(65536))


def _muestras(pcm16: bytes) -> array[int]:
    """Interpreta PCM de 16 bits little-endian como muestras con signo.

    Args:
        pcm16: Audio PCM. Un byte suelto al final se descarta.

    Returns:
        Las muestras.
    """
    muestras = array("h")
    muestras.frombytes(pcm16[: len(pcm16) - len(pcm16) % 2])
    if byteorder == "big":
        muestras.byteswap()
    return muestras


def _a_bytes(muestras: array[int]) -> bytes:
    """Serializa muestras a PCM de 16 bits little-endian.

    Args:
        muestras: Muestras con signo.

    Returns:
        El PCM.
    """
    if byteorder == "big":
        muestras = array("h", muestras)
        muestras.byteswap()
    return muestras.tobytes()


def mulaw_to_pcm16(mulaw: bytes) -> bytes:
    """Decodifica mu-law a PCM lineal de 16 bits little-endian.

    Args:
        mulaw: Audio mu-law, una muestra por byte.

    Returns:
        PCM de 16 bits, dos bytes por muestra.
    """
    return _a_bytes(array("h", (_DECODIFICAR[b] for b in mulaw)))


def pcm16_to_mulaw(pcm16: bytes) -> bytes:
    """Codifica PCM lineal de 16 bits little-endian en mu-law.

    Args:
        pcm16: Audio PCM.

    Returns:
        Audio mu-law, una muestra por byte.
    """
    return bytes(_CODIFICAR[m & 0xFFFF] for m in _muestras(pcm16))


def downsample_pcm16(pcm16: bytes, factor: int) -> bytes:
    """Reduce la frecuencia de muestreo por un factor entero.

    Promedia cada grupo de `factor` muestras en vez de quedarse con una de cada
    `factor`: el promedio es un filtro paso-bajo elemental que atenua lo que
    esta por encima de la nueva frecuencia de Nyquist. Diezmar sin filtrar
    pliega esas frecuencias (aliasing) y la voz sintetizada suena metalica por
    telefono.

    Args:
        pcm16: Audio PCM de 16 bits.
        factor: Cuantas muestras de entrada dan una de salida (3 para 24 -> 8 kHz).

    Returns:
        El audio remuestreado. Las muestras que no completan un grupo al final
        se descartan.

    Raises:
        ValueError: Si el factor no es positivo.
    """
    if factor < 1:
        raise ValueError("El factor de remuestreo tiene que ser positivo")
    if factor == 1:
        return pcm16
    muestras = _muestras(pcm16)
    completas = len(muestras) - len(muestras) % factor
    salida = array(
        "h",
        (sum(muestras[i : i + factor]) // factor for i in range(0, completas, factor)),
    )
    return _a_bytes(salida)


def rms_pcm16(pcm16: bytes) -> float:
    """Energia RMS de un trozo de audio: el indicador de voz del canal.

    Args:
        pcm16: Audio PCM de 16 bits.

    Returns:
        La raiz del cuadrado medio de las muestras; 0 si no hay muestras.
    """
    muestras = _muestras(pcm16)
    if not muestras:
        return 0.0
    return math.sqrt(sum(m * m for m in muestras) / len(muestras))


def pcm16_to_wav(pcm16: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Envuelve PCM de 16 bits mono en un WAV, el formato que acepta Whisper.

    Args:
        pcm16: Audio PCM.
        sample_rate: Frecuencia de muestreo del audio.

    Returns:
        El archivo WAV completo.
    """
    salida = io.BytesIO()
    with wave.open(salida, "wb") as archivo:
        archivo.setnchannels(1)
        archivo.setsampwidth(2)
        archivo.setframerate(sample_rate)
        archivo.writeframes(pcm16)
    return salida.getvalue()


def frames(audio: bytes, size: int = FRAME_BYTES_MULAW) -> list[bytes]:
    """Parte un audio en tramas del tamano que espera Twilio.

    Args:
        audio: Audio mu-law.
        size: Bytes por trama.

    Returns:
        Las tramas; la ultima puede ser mas corta.
    """
    return [audio[i : i + size] for i in range(0, len(audio), size)]
