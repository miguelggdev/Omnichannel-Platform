"""Tests del procesamiento de audio del canal de voz (Sprint 13, Dev A).

Cubren el codec G.711 (contra `audioop` mientras exista), la deteccion de
frases, la transcripcion en orden, el barge-in y la sintesis.
"""

import asyncio
import io
import math
import warnings
import wave
from array import array
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.voice import audio
from app.services.voice.interruption_handler import InterruptionHandler
from app.services.voice.stt_streaming import MIN_SPEECH_MS, STTSession, UtteranceSegmenter
from app.services.voice.tts_service import TTSError, split_text, synthesize_mulaw

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    try:
        import audioop
    except ImportError:  # Python 3.13+: ya no hay contra que comparar
        audioop = None  # type: ignore[assignment]


def _tono(ms: int, amplitud: int, frecuencia: int = 440) -> bytes:
    """PCM de 16 bits a 8 kHz con un tono puro.

    Args:
        ms: Duracion.
        amplitud: Pico de la onda (0 = silencio).
        frecuencia: Frecuencia del tono en Hz.

    Returns:
        El audio.
    """
    n = audio.SAMPLE_RATE * ms // 1000
    return array(
        "h",
        (
            int(amplitud * math.sin(2 * math.pi * frecuencia * i / audio.SAMPLE_RATE))
            for i in range(n)
        ),
    ).tobytes()


def _tramas(pcm: bytes) -> list[bytes]:
    """Parte PCM de 16 bits en tramas de 20 ms (320 bytes)."""
    return audio.frames(pcm, 320)


VOZ = 3000
SILENCIO = 0


# ─── Codec G.711 ─────────────────────────────────────────────────────────────


@pytest.mark.skipif(audioop is None, reason="audioop no existe desde Python 3.13")
def test_decodificar_mulaw_es_identico_a_audioop() -> None:
    todos = bytes(range(256))
    assert audio.mulaw_to_pcm16(todos) == audioop.ulaw2lin(todos, 2)


@pytest.mark.skipif(audioop is None, reason="audioop no existe desde Python 3.13")
def test_codificar_mulaw_es_identico_a_audioop_en_todo_el_rango() -> None:
    pcm = array("h", range(-32768, 32768)).tobytes()
    assert audio.pcm16_to_mulaw(pcm) == audioop.lin2ulaw(pcm, 2)


def test_ida_y_vuelta_mulaw_conserva_la_forma_de_la_onda() -> None:
    original = _tono(100, 8000)
    vuelta = audio.mulaw_to_pcm16(audio.pcm16_to_mulaw(original))
    a, b = array("h", original), array("h", vuelta)
    # G.711 es con perdida (8 bits por muestra): error relativo acotado.
    assert max(abs(x - y) for x, y in zip(a, b, strict=True)) < 8000 * 0.05


def test_downsample_promedia_grupos_y_descarta_el_resto() -> None:
    pcm = array("h", [3, 6, 9, 30, 60, 90, 7]).tobytes()
    assert array("h", audio.downsample_pcm16(pcm, 3)).tolist() == [6, 60]


def test_downsample_rechaza_un_factor_no_positivo() -> None:
    with pytest.raises(ValueError, match="positivo"):
        audio.downsample_pcm16(b"\x00\x00", 0)


def test_rms_distingue_silencio_de_voz() -> None:
    assert audio.rms_pcm16(_tono(20, SILENCIO)) == 0
    assert audio.rms_pcm16(_tono(20, VOZ)) == pytest.approx(VOZ / math.sqrt(2), rel=0.05)
    assert audio.rms_pcm16(b"") == 0


def test_wav_es_valido_y_conserva_el_audio() -> None:
    pcm = _tono(100, VOZ)
    with wave.open(io.BytesIO(audio.pcm16_to_wav(pcm))) as archivo:
        assert archivo.getframerate() == 8000
        assert archivo.getnchannels() == 1
        assert archivo.getsampwidth() == 2
        assert archivo.readframes(archivo.getnframes()) == pcm


def test_frames_parte_en_tramas_de_twilio() -> None:
    partes = audio.frames(b"x" * 400)
    assert [len(p) for p in partes] == [160, 160, 80]


# ─── Segmentacion de frases ──────────────────────────────────────────────────


def _segmentador(**kwargs: Any) -> UtteranceSegmenter:
    return UtteranceSegmenter(
        speech_threshold_rms=kwargs.get("umbral", 500),
        end_silence_ms=kwargs.get("silencio", 400),
        max_utterance_ms=kwargs.get("maximo", 30_000),
    )


def _alimentar(seg: UtteranceSegmenter, pcm: bytes) -> list[bytes]:
    return [f for t in _tramas(pcm) if (f := seg.feed(t)) is not None]


def test_una_frase_se_cierra_tras_el_silencio_configurado() -> None:
    seg = _segmentador(silencio=400)
    assert _alimentar(seg, _tono(300, SILENCIO) + _tono(600, VOZ)) == []
    # 380 ms de silencio: todavia no.
    assert _alimentar(seg, _tono(380, SILENCIO)) == []
    frases = _alimentar(seg, _tono(40, SILENCIO))
    assert len(frases) == 1


def test_la_frase_incluye_el_audio_previo_a_la_voz() -> None:
    """Sin pre-roll, Whisper recibe la primera silaba cortada."""
    seg = _segmentador(silencio=400)
    previo = _tono(300, SILENCIO)
    voz = _tono(600, VOZ)
    [frase] = _alimentar(seg, previo + voz + _tono(400, SILENCIO))
    # 200 ms de pre-roll + 600 de voz + 400 de silencio de cierre.
    assert len(frase) == (200 + 600 + 400) * 16


def test_un_golpe_corto_no_es_una_frase() -> None:
    seg = _segmentador(silencio=400)
    ruido = _tono(MIN_SPEECH_MS - 60, VOZ)
    assert _alimentar(seg, ruido + _tono(600, SILENCIO)) == []


def test_una_frase_larga_se_corta_en_el_maximo() -> None:
    seg = _segmentador(maximo=1000)
    frases = _alimentar(seg, _tono(2500, VOZ))
    assert len(frases) == 2
    assert all(len(f) == 1000 * 16 for f in frases)


def test_flush_entrega_la_frase_en_curso_al_colgar() -> None:
    seg = _segmentador()
    _alimentar(seg, _tono(500, VOZ))
    frase = seg.flush()
    assert frase is not None
    assert seg.flush() is None


# ─── Sesion de STT ───────────────────────────────────────────────────────────


async def test_las_frases_se_transcriben_en_el_orden_en_que_se_dijeron() -> None:
    """La primera frase tarda mas en Whisper que la segunda: el orden se respeta."""
    recibidos: list[str] = []
    llamadas = 0

    async def transcriptor(pcm: bytes) -> str:
        nonlocal llamadas
        llamadas += 1
        numero = llamadas
        await asyncio.sleep(0.05 if numero == 1 else 0)
        return f"frase {numero}"

    async def al_transcribir(texto: str) -> None:
        recibidos.append(texto)

    sesion = STTSession("CA1", al_transcribir, _segmentador(silencio=100), transcriptor)
    for pcm in (_tono(400, VOZ), _tono(120, SILENCIO), _tono(400, VOZ), _tono(120, SILENCIO)):
        for trama in _tramas(pcm):
            sesion.feed(trama)
    await sesion.close()

    assert recibidos == ["frase 1", "frase 2"]


async def test_una_frase_que_falla_no_corta_la_transcripcion_del_resto() -> None:
    recibidos: list[str] = []
    textos = iter([RuntimeError("boom"), "segunda"])

    async def transcriptor(pcm: bytes) -> str:
        valor = next(textos)
        if isinstance(valor, Exception):
            raise valor
        return valor

    async def al_transcribir(texto: str) -> None:
        recibidos.append(texto)

    sesion = STTSession("CA1", al_transcribir, _segmentador(silencio=100), transcriptor)
    for pcm in (_tono(400, VOZ), _tono(120, SILENCIO), _tono(400, VOZ), _tono(120, SILENCIO)):
        for trama in _tramas(pcm):
            sesion.feed(trama)
    await sesion.close()

    assert recibidos == ["segunda"]


async def test_close_transcribe_la_frase_en_curso() -> None:
    recibidos: list[str] = []

    async def al_transcribir(texto: str) -> None:
        recibidos.append(texto)

    sesion = STTSession("CA1", al_transcribir, _segmentador(), AsyncMock(return_value="adios"))
    for trama in _tramas(_tono(500, VOZ)):
        sesion.feed(trama)
    await sesion.close()
    assert recibidos == ["adios"]


async def test_close_no_espera_para_siempre_a_whisper() -> None:
    async def lento(pcm: bytes) -> str:
        await asyncio.sleep(60)
        return "nunca"

    sesion = STTSession("CA1", AsyncMock(), _segmentador(), lento)
    for trama in _tramas(_tono(500, VOZ)):
        sesion.feed(trama)
    with patch("app.services.voice.stt_streaming.CLOSE_TIMEOUT_SECONDS", 0.05):
        await asyncio.wait_for(sesion.close(), timeout=2)


async def test_transcribir_con_whisper_descarta_audio_sin_voz() -> None:
    from app.services.transcription import PermanentTranscriptionError
    from app.services.voice import stt_streaming

    with patch.object(
        stt_streaming,
        "transcribe_audio",
        AsyncMock(side_effect=PermanentTranscriptionError("sin voz")),
    ):
        assert await stt_streaming.transcribir_con_whisper(_tono(300, VOZ), "t") is None


async def test_transcribir_con_whisper_manda_un_wav() -> None:
    from app.services.voice import stt_streaming

    llamada = AsyncMock(return_value=SimpleNamespace(text="hola", duration_seconds=1.0))
    with patch.object(stt_streaming, "transcribe_audio", llamada):
        assert await stt_streaming.transcribir_con_whisper(_tono(300, VOZ), "t") == "hola"
    wav = llamada.await_args.args[0]
    assert wav.startswith(b"RIFF")
    assert wav[8:12] == b"WAVE"


# ─── Barge-in ────────────────────────────────────────────────────────────────


def test_barge_in_necesita_voz_sostenida() -> None:
    detector = InterruptionHandler(threshold_rms=800, min_speech_ms=200)
    tramas = _tramas(_tono(200, VOZ))
    resultados = [detector.detect(t) for t in tramas]
    assert resultados[:-1] == [False] * (len(tramas) - 1)
    assert resultados[-1] is True


def test_barge_in_ignora_voz_por_debajo_del_umbral() -> None:
    """El eco de la propia respuesta del agente llega mas bajo: no interrumpe."""
    detector = InterruptionHandler(threshold_rms=800, min_speech_ms=200)
    assert not any(detector.detect(t) for t in _tramas(_tono(1000, 900)))


def test_barge_in_un_silencio_reinicia_la_cuenta() -> None:
    detector = InterruptionHandler(threshold_rms=800, min_speech_ms=200)
    pcm = _tono(160, VOZ) + _tono(20, SILENCIO) + _tono(160, VOZ)
    assert not any(detector.detect(t) for t in _tramas(pcm))


def test_dos_llamadas_no_comparten_detector() -> None:
    """En el spec, un solo detector por proceso sumaba la voz de todas las llamadas."""
    a = InterruptionHandler(threshold_rms=800, min_speech_ms=200)
    b = InterruptionHandler(threshold_rms=800, min_speech_ms=200)
    tramas = _tramas(_tono(200, VOZ))
    resultados = []
    for trama in tramas:
        resultados.append(a.detect(trama))
        resultados.append(b.detect(trama))
    assert resultados.count(True) == 2


# ─── TTS ─────────────────────────────────────────────────────────────────────


def test_split_text_respeta_el_limite_y_las_oraciones() -> None:
    texto = "Primera oracion. Segunda oracion! ¿Tercera?"
    assert split_text(texto, limit=20) == ["Primera oracion.", "Segunda oracion!", "¿Tercera?"]
    assert split_text(texto) == [texto]


def test_split_text_corta_una_oracion_mas_larga_que_el_limite() -> None:
    trozos = split_text("a" * 45, limit=20)
    assert trozos == ["a" * 20, "a" * 20, "a" * 5]


def test_split_text_de_un_texto_vacio_no_da_trozos() -> None:
    assert split_text("   ") == []


async def test_synthesize_convierte_24khz_pcm_a_mulaw_8khz() -> None:
    pcm_24k = array("h", [1000] * 2400).tobytes()  # 100 ms a 24 kHz
    cliente = MagicMock()
    cliente.audio.speech.create = AsyncMock(return_value=SimpleNamespace(content=pcm_24k))
    cliente.__aenter__ = AsyncMock(return_value=cliente)
    cliente.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.services.voice.tts_service.AsyncOpenAI", return_value=cliente),
        patch("app.services.voice.tts_service.get_settings") as settings,
    ):
        settings.return_value = SimpleNamespace(
            OPENAI_API_KEY="sk",
            VOICE_TTS_TIMEOUT_SECONDS=5,
            VOICE_TTS_MODEL="tts-1",
            VOICE_TTS_VOICE="nova",
            VOICE_TTS_COST_PER_1K_CHARS_USD=0.015,
        )
        mulaw = await synthesize_mulaw("Hola", "tenant")

    assert len(mulaw) == 800  # 100 ms a 8 kHz, un byte por muestra
    assert mulaw == audio.pcm16_to_mulaw(array("h", [1000] * 800).tobytes())
    assert cliente.audio.speech.create.await_args.kwargs["response_format"] == "pcm"


async def test_synthesize_sin_credencial_falla_claro() -> None:
    with patch("app.services.voice.tts_service.get_settings") as settings:
        settings.return_value = SimpleNamespace(OPENAI_API_KEY="")
        with pytest.raises(TTSError):
            await synthesize_mulaw("Hola", "tenant")
