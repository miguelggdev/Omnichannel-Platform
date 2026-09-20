"""Tests del servicio de transcripcion (Sprint 9): descarga protegida y Whisper.

La descarga trata la URL como entrada no confiable; estos tests fijan las
defensas (https, IPs publicas, redirecciones revalidadas, tope de bytes) y la
clasificacion de errores en transitorios y permanentes.
"""

from typing import Any, ClassVar

import httpx
import openai
import pytest

from app.services import transcription as tr
from app.services.transcription import (
    PermanentTranscriptionError,
    TranscriptionError,
    download_media,
    inferir_nombre_audio,
    transcribe_audio,
)

PUBLICA = "8.8.8.8"


@pytest.fixture(autouse=True)
def dns_publico(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Resuelve todo host a una IP publica, salvo los que el test defina.

    Args:
        monkeypatch: Fixture de pytest.

    Returns:
        Tabla `host -> IPs` que el test puede ampliar.
    """
    tabla: dict[str, list[str]] = {}

    async def _resolver(host: str, port: int) -> list[str]:
        return tabla.get(host, [PUBLICA])

    monkeypatch.setattr(tr, "_resolver_host", _resolver)
    return tabla


def _transporte(handler: Any) -> httpx.MockTransport:
    """Envuelve un handler sincrono en un transporte de httpx."""
    return httpx.MockTransport(handler)


class TestUrlsNoConfiables:
    """Solo https y solo hacia redes publicas."""

    async def test_http_plano_se_rechaza(self) -> None:
        with pytest.raises(PermanentTranscriptionError, match="https"):
            await download_media("http://cdn.example.com/a.ogg", max_bytes=100)

    @pytest.mark.parametrize(
        "ip", ["127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254", "::1", "fd00::1"]
    )
    async def test_ips_no_publicas_se_rechazan(
        self, dns_publico: dict[str, list[str]], ip: str
    ) -> None:
        """El metadata service de la nube (169.254.169.254) es el objetivo clasico."""
        dns_publico["interno.example.com"] = [ip]

        with pytest.raises(PermanentTranscriptionError, match="no publica"):
            await download_media("https://interno.example.com/a.ogg", max_bytes=100)

    async def test_basta_una_ip_no_publica_entre_varias(
        self, dns_publico: dict[str, list[str]]
    ) -> None:
        dns_publico["mixto.example.com"] = [PUBLICA, "10.0.0.1"]

        with pytest.raises(PermanentTranscriptionError):
            await download_media("https://mixto.example.com/a.ogg", max_bytes=100)

    async def test_no_se_conecta_si_la_url_es_invalida(
        self, dns_publico: dict[str, list[str]]
    ) -> None:
        dns_publico["interno.example.com"] = ["127.0.0.1"]
        llamadas: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            llamadas.append(str(request.url))
            return httpx.Response(200, content=b"x")

        with pytest.raises(PermanentTranscriptionError):
            await download_media(
                "https://interno.example.com/a.ogg", max_bytes=100, transport=_transporte(handler)
            )

        assert llamadas == []

    async def test_una_redireccion_hacia_la_red_interna_se_rechaza(
        self, dns_publico: dict[str, list[str]]
    ) -> None:
        """Cada salto se revalida: un CDN legitimo no puede rebotar a 169.254.x."""
        dns_publico["metadata.internal"] = ["169.254.169.254"]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "cdn.example.com":
                return httpx.Response(302, headers={"location": "https://metadata.internal/x"})
            return httpx.Response(200, content=b"secreto")

        with pytest.raises(PermanentTranscriptionError, match="no publica"):
            await download_media(
                "https://cdn.example.com/a.ogg", max_bytes=100, transport=_transporte(handler)
            )

    async def test_demasiadas_redirecciones(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://cdn.example.com/otra"})

        with pytest.raises(PermanentTranscriptionError, match="redirecciones"):
            await download_media(
                "https://cdn.example.com/a.ogg", max_bytes=100, transport=_transporte(handler)
            )

    async def test_una_redireccion_valida_se_sigue(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/a.ogg":
                return httpx.Response(302, headers={"location": "/final.ogg"})
            return httpx.Response(200, content=b"audio", headers={"content-type": "audio/ogg"})

        medio = await download_media(
            "https://cdn.example.com/a.ogg", max_bytes=100, transport=_transporte(handler)
        )

        assert medio.content == b"audio"
        assert medio.url_path == "/final.ogg"


class TestDescarga:
    """Tope de tamano y clasificacion de errores HTTP."""

    async def test_devuelve_bytes_y_content_type_sin_parametros(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"abc", headers={"content-type": "Audio/OGG; codecs=opus"}
            )

        medio = await download_media(
            "https://cdn.example.com/a", max_bytes=100, transport=_transporte(handler)
        )

        assert medio.content == b"abc"
        assert medio.content_type == "audio/ogg"

    async def test_content_length_por_encima_del_tope_se_rechaza(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 50, headers={"content-length": "5000"})

        with pytest.raises(PermanentTranscriptionError, match="maximo"):
            await download_media(
                "https://cdn.example.com/a", max_bytes=100, transport=_transporte(handler)
            )

    async def test_el_tope_se_aplica_aunque_el_servidor_no_declare_tamano(self) -> None:
        """Sin Content-Length (chunked) el corte ocurre mientras se lee."""

        async def trozos() -> Any:
            for _ in range(10):
                yield b"x" * 50

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=trozos())

        with pytest.raises(PermanentTranscriptionError, match="supera"):
            await download_media(
                "https://cdn.example.com/a", max_bytes=100, transport=_transporte(handler)
            )

    async def test_justo_en_el_tope_se_acepta(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 100)

        medio = await download_media(
            "https://cdn.example.com/a", max_bytes=100, transport=_transporte(handler)
        )

        assert len(medio.content) == 100

    @pytest.mark.parametrize("status", [500, 502, 503, 429])
    async def test_5xx_y_429_son_transitorios(self, status: int) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status)

        with pytest.raises(TranscriptionError) as info:
            await download_media(
                "https://cdn.example.com/a", max_bytes=100, transport=_transporte(handler)
            )

        assert not isinstance(info.value, PermanentTranscriptionError)

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
    async def test_4xx_son_permanentes(self, status: int) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status)

        with pytest.raises(PermanentTranscriptionError):
            await download_media(
                "https://cdn.example.com/a", max_bytes=100, transport=_transporte(handler)
            )

    async def test_fallo_de_red_es_transitorio_y_no_filtra_la_url(self) -> None:
        """La URL de Telegram lleva el token del bot: no puede llegar al mensaje."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no conecta", request=request)

        with pytest.raises(TranscriptionError) as info:
            await download_media(
                "https://api.telegram.org/file/botSECRETO/voice/1.oga",
                max_bytes=100,
                transport=_transporte(handler),
            )

        assert not isinstance(info.value, PermanentTranscriptionError)
        assert "SECRETO" not in str(info.value)

    async def test_el_error_http_no_incluye_la_url(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        with pytest.raises(PermanentTranscriptionError) as info:
            await download_media(
                "https://api.telegram.org/file/botSECRETO/voice/1.oga",
                max_bytes=100,
                transport=_transporte(handler),
            )

        assert "SECRETO" not in str(info.value)


class TestNombreDeArchivo:
    """Whisper decide el formato por la extension: hay que darle una valida."""

    @pytest.mark.parametrize(
        ("mime", "ruta", "esperado"),
        [
            ("audio/ogg", "/x", "audio.ogg"),
            ("audio/mpeg", "/x", "audio.mp3"),
            ("audio/mp4", "/x", "audio.m4a"),
            ("audio/aac", "/x", "audio.m4a"),
            ("audio/wav", "/x", "audio.wav"),
            ("video/mp4", "/x", "audio.mp4"),
            # Telegram sirve `application/octet-stream` con la extension en la ruta.
            ("application/octet-stream", "/file/bot1/voice/file_3.oga", "audio.oga"),
            ("", "/voice/a.MP3", "audio.mp3"),
        ],
    )
    def test_infiere_extension(self, mime: str, ruta: str, esperado: str) -> None:
        assert inferir_nombre_audio(mime, ruta) == esperado

    def test_el_content_type_manda_sobre_la_url(self) -> None:
        assert inferir_nombre_audio("audio/wav", "/a.ogg") == "audio.wav"

    @pytest.mark.parametrize(
        ("mime", "ruta"),
        [("audio/amr", "/a.amr"), ("application/octet-stream", "/a"), ("", "/a.exe")],
    )
    def test_formato_no_soportado_es_permanente(self, mime: str, ruta: str) -> None:
        with pytest.raises(PermanentTranscriptionError, match="no soportado"):
            inferir_nombre_audio(mime, ruta)


class _FakeTranscriptions:
    """Doble de `client.audio.transcriptions`."""

    def __init__(self, resultado: Any = None, error: Exception | None = None) -> None:
        self.resultado = resultado
        self.error = error
        self.llamadas: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.llamadas.append(kwargs)
        if self.error:
            raise self.error
        return self.resultado


class _FakeOpenAI:
    """Doble de `AsyncOpenAI` que se usa como context manager."""

    instancias: ClassVar[list["_FakeOpenAI"]] = []
    transcriptions: ClassVar[_FakeTranscriptions] = _FakeTranscriptions()

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.audio = type("Audio", (), {"transcriptions": _FakeOpenAI.transcriptions})()
        _FakeOpenAI.instancias.append(self)

    async def __aenter__(self) -> "_FakeOpenAI":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


@pytest.fixture
def whisper(monkeypatch: pytest.MonkeyPatch) -> type[_FakeOpenAI]:
    """Sustituye `AsyncOpenAI` por un doble.

    Args:
        monkeypatch: Fixture de pytest.

    Returns:
        La clase doble; `whisper.transcriptions` se configura por test.
    """
    _FakeOpenAI.instancias = []
    _FakeOpenAI.transcriptions = _FakeTranscriptions()
    monkeypatch.setattr(tr, "AsyncOpenAI", _FakeOpenAI)
    return _FakeOpenAI


def _respuesta(**campos: Any) -> Any:
    return type("Resp", (), campos)()


def _error_openai(clase: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    """Construye un error de la API de OpenAI del tipo pedido."""
    request = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
    return clase("boom", response=httpx.Response(status, request=request), body=None)


class TestWhisper:
    """Llamada a la API y clasificacion de sus errores."""

    async def test_devuelve_texto_y_duracion(self, whisper: type[_FakeOpenAI]) -> None:
        whisper.transcriptions = _FakeTranscriptions(
            _respuesta(text="  hola mundo \n", duration=12.5)
        )

        resultado = await transcribe_audio(b"abc", "audio.ogg", api_key="sk", model="whisper-1")

        assert resultado.text == "hola mundo"
        assert resultado.duration_seconds == 12.5

    async def test_sin_duracion_es_cero(self, whisper: type[_FakeOpenAI]) -> None:
        whisper.transcriptions = _FakeTranscriptions(_respuesta(text="hola"))

        resultado = await transcribe_audio(b"abc", "audio.ogg", api_key="sk", model="whisper-1")

        assert resultado.duration_seconds == 0.0

    async def test_manda_archivo_modelo_y_pide_verbose_json(
        self, whisper: type[_FakeOpenAI]
    ) -> None:
        whisper.transcriptions = _FakeTranscriptions(_respuesta(text="hola", duration=1))

        await transcribe_audio(b"abc", "audio.oga", api_key="sk", model="whisper-1")

        llamada = whisper.transcriptions.llamadas[0]
        assert llamada["file"] == ("audio.oga", b"abc")
        assert llamada["model"] == "whisper-1"
        assert llamada["response_format"] == "verbose_json"

    async def test_sin_idioma_configurado_whisper_autodetecta(
        self, whisper: type[_FakeOpenAI]
    ) -> None:
        """La plataforma atiende 6 idiomas: forzar uno degradaria a los demas."""
        whisper.transcriptions = _FakeTranscriptions(_respuesta(text="hola", duration=1))

        await transcribe_audio(b"abc", "audio.ogg", api_key="sk", model="whisper-1")

        assert "language" not in whisper.transcriptions.llamadas[0]

    async def test_con_idioma_configurado_se_envia(self, whisper: type[_FakeOpenAI]) -> None:
        whisper.transcriptions = _FakeTranscriptions(_respuesta(text="hola", duration=1))

        await transcribe_audio(b"abc", "audio.ogg", api_key="sk", model="whisper-1", language="pt")

        assert whisper.transcriptions.llamadas[0]["language"] == "pt"

    async def test_los_reintentos_los_decide_celery_no_el_sdk(
        self, whisper: type[_FakeOpenAI]
    ) -> None:
        whisper.transcriptions = _FakeTranscriptions(_respuesta(text="hola", duration=1))

        await transcribe_audio(b"abc", "audio.ogg", api_key="sk", model="whisper-1", timeout_s=7)

        assert whisper.instancias[0].kwargs["max_retries"] == 0
        assert whisper.instancias[0].kwargs["timeout"] == 7

    async def test_sin_api_key_es_permanente_y_no_llama(self, whisper: type[_FakeOpenAI]) -> None:
        with pytest.raises(PermanentTranscriptionError, match="OPENAI_API_KEY"):
            await transcribe_audio(b"abc", "audio.ogg", api_key="", model="whisper-1")

        assert whisper.instancias == []

    @pytest.mark.parametrize("texto", ["", "   ", "\n"])
    async def test_audio_sin_voz_es_permanente(
        self, whisper: type[_FakeOpenAI], texto: str
    ) -> None:
        whisper.transcriptions = _FakeTranscriptions(_respuesta(text=texto, duration=3))

        with pytest.raises(PermanentTranscriptionError, match="voz"):
            await transcribe_audio(b"abc", "audio.ogg", api_key="sk", model="whisper-1")

    @pytest.mark.parametrize(
        ("clase", "status"),
        [
            (openai.AuthenticationError, 401),
            (openai.PermissionDeniedError, 403),
            (openai.BadRequestError, 400),
            (openai.UnprocessableEntityError, 422),
        ],
    )
    async def test_errores_del_cliente_son_permanentes(
        self, whisper: type[_FakeOpenAI], clase: type[openai.APIStatusError], status: int
    ) -> None:
        whisper.transcriptions = _FakeTranscriptions(error=_error_openai(clase, status))

        with pytest.raises(PermanentTranscriptionError):
            await transcribe_audio(b"abc", "audio.ogg", api_key="sk", model="whisper-1")

    @pytest.mark.parametrize(
        ("clase", "status"),
        [(openai.RateLimitError, 429), (openai.InternalServerError, 500)],
    )
    async def test_limite_de_tasa_y_5xx_son_transitorios(
        self, whisper: type[_FakeOpenAI], clase: type[openai.APIStatusError], status: int
    ) -> None:
        whisper.transcriptions = _FakeTranscriptions(error=_error_openai(clase, status))

        with pytest.raises(TranscriptionError) as info:
            await transcribe_audio(b"abc", "audio.ogg", api_key="sk", model="whisper-1")

        assert not isinstance(info.value, PermanentTranscriptionError)

    async def test_fallo_de_conexion_es_transitorio(self, whisper: type[_FakeOpenAI]) -> None:
        request = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
        whisper.transcriptions = _FakeTranscriptions(
            error=openai.APIConnectionError(request=request)
        )

        with pytest.raises(TranscriptionError) as info:
            await transcribe_audio(b"abc", "audio.ogg", api_key="sk", model="whisper-1")

        assert not isinstance(info.value, PermanentTranscriptionError)
