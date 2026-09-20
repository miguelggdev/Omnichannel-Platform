"""Tests de la transcripcion de notas de voz (`app/tasks/audio_transcription.py`).

No hay red, ni Redis, ni base de datos: la descarga se sirve con
`httpx.MockTransport`, Whisper y la sesion de SQLAlchemy se sustituyen.
"""

import uuid
from typing import Any, ClassVar

import httpx
import pytest

from app.tasks import audio_transcription as at

CLIENT_ID = uuid.uuid4()
CONVERSATION_ID = uuid.uuid4()
CONTACT_ID = uuid.uuid4()


def _mensaje(**extra: Any) -> dict[str, Any]:
    """Arma un `NormalizedMessage` serializado de una nota de voz.

    Args:
        **extra: Campos que sustituyen a los del audio de Telegram.

    Returns:
        El mensaje serializado.
    """
    datos: dict[str, Any] = {
        "channel": "telegram",
        "sender_identifier": "789",
        "text": None,
        "media_url": "telegram-file:BAADBAADrwADBREAAf",
        "media_type": "audio",
        "timestamp": "2026-09-20T10:00:00+00:00",
        "external_message_id": "tg.1001",
        "raw_payload": {},
    }
    datos.update(extra)
    return datos


class MensajeFalso:
    """Fila de `messages` con lo justo para que la tarea la actualice."""

    def __init__(self) -> None:
        self.content: str | None = None
        self.metadata_: dict[str, Any] = {"origen": "webhook"}


class ResultadoFalso:
    """Resultado de `session.execute()` con un valor prefijado."""

    def __init__(self, valor: Any) -> None:
        self._valor = valor

    def scalar_one_or_none(self) -> Any:
        """Devuelve el valor configurado."""
        return self._valor


class SesionFalsa:
    """AsyncSession minima que devuelve siempre la misma fila."""

    def __init__(self, fila: Any) -> None:
        self.fila = fila

    async def execute(self, *args: Any, **kwargs: Any) -> ResultadoFalso:
        """Devuelve la fila configurada."""
        return ResultadoFalso(self.fila)


def _tenant_session_falsa(monkeypatch: pytest.MonkeyPatch, fila: Any) -> None:
    """Sustituye `tenant_session` por un contexto que sirve `fila`.

    Args:
        monkeypatch: Utilidad de pytest.
        fila: Lo que devolvera `scalar_one_or_none()`.
    """

    class Contexto:
        async def __aenter__(self) -> SesionFalsa:
            return SesionFalsa(fila)

        async def __aexit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr(at, "tenant_session", lambda _client_id: Contexto())


# ─── Resolucion de la URL ────────────────────────────────────────────────────


class TestResolverUrl:
    """Telegram no manda URLs: manda un file_id que hay que canjear."""

    @pytest.mark.asyncio
    async def test_una_url_directa_se_devuelve_tal_cual(self) -> None:
        url = "https://cdn.ycloud.com/audio.ogg"
        assert await at.resolver_url(CLIENT_ID, "whatsapp", url) == url

    @pytest.mark.asyncio
    async def test_telegram_canjea_el_file_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.agents.nodes import _tenant as tenant_module
        from app.services.messaging import telegram as telegram_module

        monkeypatch.setattr(
            tenant_module,
            "get_channel_config",
            lambda canal, cid=None: ("telegram", {"bot_token": "T"}),
        )

        pedidos: list[tuple[str, dict[str, Any]]] = []

        async def _get_file_url(self: Any, file_id: str, config: dict[str, Any]) -> str:
            pedidos.append((file_id, config))
            return f"https://api.telegram.org/file/bot{config['bot_token']}/voice/x.ogg"

        monkeypatch.setattr(telegram_module.TelegramProvider, "get_file_url", _get_file_url)

        url = await at.resolver_url(CLIENT_ID, "telegram", "telegram-file:ABC")

        assert pedidos == [("ABC", {"bot_token": "T"})]
        assert url.endswith("voice/x.ogg")


# ─── Descarga ────────────────────────────────────────────────────────────────


def _cliente_que_sirve(cuerpo: bytes) -> Any:
    """Construye un transporte falso que responde con `cuerpo`.

    Args:
        cuerpo: Contenido de la respuesta.

    Returns:
        Una fabrica de `httpx.AsyncClient` con ese transporte.
    """

    real = httpx.AsyncClient  # antes del monkeypatch, para no recursionar

    def _fabrica(**kwargs: Any) -> httpx.AsyncClient:
        transporte = httpx.MockTransport(lambda _peticion: httpx.Response(200, content=cuerpo))
        return real(transport=transporte)

    return _fabrica


class TestDescargarAudio:
    """El tope de tamano se aplica sobre lo que llega, no sobre lo que declara."""

    @pytest.mark.asyncio
    async def test_devuelve_el_audio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(at.httpx, "AsyncClient", _cliente_que_sirve(b"OggS-audio"))
        assert await at.descargar_audio("https://x/a.ogg", 1000) == b"OggS-audio"

    @pytest.mark.asyncio
    async def test_corta_si_se_pasa_del_tope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(at.httpx, "AsyncClient", _cliente_que_sirve(b"x" * 500))
        with pytest.raises(at.AudioDemasiadoGrandeError):
            await at.descargar_audio("https://x/a.ogg", 100)

    @pytest.mark.asyncio
    async def test_un_error_http_propaga(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = httpx.AsyncClient

        def _fabrica(**kwargs: Any) -> httpx.AsyncClient:
            transporte = httpx.MockTransport(lambda _p: httpx.Response(404))
            return real(transport=transporte)

        monkeypatch.setattr(at.httpx, "AsyncClient", _fabrica)
        with pytest.raises(httpx.HTTPStatusError):
            await at.descargar_audio("https://x/a.ogg", 1000)


# ─── Whisper ─────────────────────────────────────────────────────────────────


class WhisperFalso:
    """Cliente de OpenAI minimo: registra la llamada y devuelve un texto."""

    ultima_llamada: ClassVar[dict[str, Any]] = {}

    def __init__(self, texto: str = " hola que tal  ") -> None:
        self.texto = texto
        self.audio = type("Audio", (), {"transcriptions": self})()

    async def create(self, **kwargs: Any) -> Any:
        """Registra los argumentos y devuelve la respuesta de Whisper."""
        WhisperFalso.ultima_llamada = kwargs
        return type("Respuesta", (), {"text": self.texto})()


class TestTranscribir:
    """Llamada a la API de transcripcion."""

    @pytest.mark.asyncio
    async def test_sin_api_key_no_llama(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "OPENAI_API_KEY", "")
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            await at.transcribir(b"audio")

    @pytest.mark.asyncio
    async def test_manda_el_modelo_y_el_idioma_configurados(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(get_settings(), "WHISPER_MODEL", "whisper-1")
        monkeypatch.setattr(get_settings(), "WHISPER_LANGUAGE", "es")
        monkeypatch.setattr(at, "AsyncOpenAI", lambda **kwargs: WhisperFalso())

        texto = await at.transcribir(b"audio")

        assert texto == "hola que tal"
        assert WhisperFalso.ultima_llamada["model"] == "whisper-1"
        assert WhisperFalso.ultima_llamada["language"] == "es"

    @pytest.mark.asyncio
    async def test_idioma_vacio_deja_que_lo_detecte(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(get_settings(), "WHISPER_LANGUAGE", "")
        monkeypatch.setattr(at, "AsyncOpenAI", lambda **kwargs: WhisperFalso())

        await at.transcribir(b"audio")

        assert "language" not in WhisperFalso.ultima_llamada


# ─── Guardado ────────────────────────────────────────────────────────────────


class TestGuardarTranscripcion:
    """La transcripcion entra en `content`; el audio original se conserva."""

    @pytest.mark.asyncio
    async def test_escribe_contenido_y_metadatos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "WHISPER_MODEL", "whisper-1")
        monkeypatch.setattr(get_settings(), "WHISPER_LANGUAGE", "es")
        fila = MensajeFalso()
        _tenant_session_falsa(monkeypatch, fila)

        await at.guardar_transcripcion(CLIENT_ID, CONVERSATION_ID, "tg.1001", "quiero una cita")

        assert fila.content == "quiero una cita"
        assert fila.metadata_["original_type"] == "audio"
        assert fila.metadata_["transcription_model"] == "whisper-1"
        assert fila.metadata_["transcription_language"] == "es"
        assert fila.metadata_["origen"] == "webhook"  # no pisa lo que ya habia

    @pytest.mark.asyncio
    async def test_un_mensaje_que_no_esta_no_rompe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _tenant_session_falsa(monkeypatch, None)
        await at.guardar_transcripcion(CLIENT_ID, CONVERSATION_ID, "tg.1001", "texto")


# ─── La tarea ────────────────────────────────────────────────────────────────


class TareaSelfFalsa:
    """Sustituto del `self` de una tarea Celery con bind=True."""

    def __init__(self, retries: int = 0, max_retries: int = 3) -> None:
        self.request = type("Request", (), {"retries": retries})()
        self.max_retries = max_retries
        self.retry_calls = 0

    def retry(self, exc: Exception | None = None, countdown: float | None = None) -> Exception:
        """Devuelve la excepcion que el codigo relanza como reintento."""
        self.retry_calls += 1
        return RuntimeError("retry solicitado")


def _capturar_ia(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Sustituye el encolado de la IA y devuelve el registro de llamadas.

    Args:
        monkeypatch: Utilidad de pytest.

    Returns:
        Lista donde se anota cada encolado.
    """
    encolados: list[dict[str, Any]] = []
    monkeypatch.setattr(
        at,
        "_encolar_ia",
        lambda client_id, conversation_id, contact_id, channel, message_data: encolados.append(
            {"channel": channel, "message_data": message_data, "conversation_id": conversation_id}
        ),
    )
    return encolados


def _correr(monkeypatch: pytest.MonkeyPatch, resultado: Any, self_falso: Any) -> Any:
    """Ejecuta la tarea con `_transcribir_mensaje` sustituido.

    Args:
        monkeypatch: Utilidad de pytest.
        resultado: Texto a devolver, o una excepcion a lanzar.
        self_falso: Doble del `self` de la tarea.

    Returns:
        Lo que devuelva la tarea.
    """

    async def _falsa(*args: Any, **kwargs: Any) -> str:
        if isinstance(resultado, Exception):
            raise resultado
        return str(resultado)

    monkeypatch.setattr(at, "_transcribir_mensaje", _falsa)
    monkeypatch.setattr(at, "run_isolated", lambda coro: __import__("asyncio").run(coro))

    # Celery expone la funcion original en __wrapped__ ya ligada a la instancia
    # de la tarea; __func__ la devuelve sin ligar para inyectar un `self` falso.
    return at.transcribe_audio.__wrapped__.__func__(
        self_falso,
        client_id=str(CLIENT_ID),
        conversation_id=str(CONVERSATION_ID),
        contact_id=str(CONTACT_ID),
        channel="telegram",
        message_data=_mensaje(),
    )


class TestTarea:
    """Camino feliz, reintentos y degradado."""

    def test_encola_la_ia_con_el_texto_transcrito(self, monkeypatch: pytest.MonkeyPatch) -> None:
        encolados = _capturar_ia(monkeypatch)

        salida = _correr(monkeypatch, "quiero una cita", TareaSelfFalsa())

        assert salida == {"status": "transcribed"}
        assert encolados[0]["message_data"]["text"] == "quiero una cita"
        assert encolados[0]["message_data"]["media_url"].startswith("telegram-file:")
        assert encolados[0]["channel"] == "telegram"

    def test_un_fallo_transitorio_reintenta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        encolados = _capturar_ia(monkeypatch)
        self_falso = TareaSelfFalsa(retries=0)

        with pytest.raises(RuntimeError, match="retry"):
            _correr(monkeypatch, httpx.ConnectError("sin red"), self_falso)

        assert self_falso.retry_calls == 1
        assert encolados == []

    def test_agotados_los_reintentos_responde_igual(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """El contacto mando un audio: merece respuesta aunque no se transcriba."""
        encolados = _capturar_ia(monkeypatch)

        salida = _correr(monkeypatch, httpx.ConnectError("sin red"), TareaSelfFalsa(retries=3))

        assert salida == {"status": "failed"}
        assert encolados[0]["message_data"]["text"] == at.TEXTO_SIN_TRANSCRIBIR

    def test_un_audio_demasiado_grande_no_reintenta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Descargarlo otra vez da el mismo tamano."""
        encolados = _capturar_ia(monkeypatch)
        self_falso = TareaSelfFalsa(retries=0)

        salida = _correr(monkeypatch, at.AudioDemasiadoGrandeError("25 MB"), self_falso)

        assert salida == {"status": "failed"}
        assert self_falso.retry_calls == 0
        assert encolados[0]["message_data"]["text"] == at.TEXTO_SIN_TRANSCRIBIR


class TestTranscribirMensaje:
    """Orquestacion: resolver, descargar, transcribir, guardar."""

    @pytest.mark.asyncio
    async def test_recorrido_completo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pasos: list[str] = []

        async def _resolver(*args: Any) -> str:
            pasos.append("resolver")
            return "https://api.telegram.org/file/botT/x.ogg"

        async def _descargar(url: str, limite: int) -> bytes:
            pasos.append("descargar")
            return b"audio"

        async def _transcribir(audio: bytes) -> str:
            pasos.append("transcribir")
            return "hola"

        async def _guardar(*args: Any) -> None:
            pasos.append("guardar")

        monkeypatch.setattr(at, "resolver_url", _resolver)
        monkeypatch.setattr(at, "descargar_audio", _descargar)
        monkeypatch.setattr(at, "transcribir", _transcribir)
        monkeypatch.setattr(at, "guardar_transcripcion", _guardar)

        texto = await at._transcribir_mensaje(CLIENT_ID, CONVERSATION_ID, _mensaje())

        assert texto == "hola"
        assert pasos == ["resolver", "descargar", "transcribir", "guardar"]

    @pytest.mark.asyncio
    async def test_sin_media_url_es_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValueError, match="media_url"):
            await at._transcribir_mensaje(
                CLIENT_ID, CONVERSATION_ID, _mensaje(media_url=None, text="x")
            )

    @pytest.mark.asyncio
    async def test_una_transcripcion_vacia_es_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Si Whisper no oyo nada, no hay que guardar un contenido vacio."""

        async def _resolver(*args: Any) -> str:
            return "https://x/a.ogg"

        async def _descargar(url: str, limite: int) -> bytes:
            return b"audio"

        async def _transcribir(audio: bytes) -> str:
            return "   "

        guardados: list[Any] = []

        async def _guardar(*args: Any) -> None:
            guardados.append(args)

        monkeypatch.setattr(at, "resolver_url", _resolver)
        monkeypatch.setattr(at, "descargar_audio", _descargar)
        monkeypatch.setattr(at, "transcribir", _transcribir)
        monkeypatch.setattr(at, "guardar_transcripcion", _guardar)

        with pytest.raises(ValueError, match="vacia"):
            await at._transcribir_mensaje(CLIENT_ID, CONVERSATION_ID, _mensaje())
        assert guardados == []


# ─── Cableado ────────────────────────────────────────────────────────────────


class TestCableado:
    """La cola y el worker tienen que existir de verdad."""

    def test_la_tarea_esta_registrada(self) -> None:
        import importlib

        from app.tasks.celery_app import TASK_MODULES, celery_app

        assert "app.tasks.audio_transcription" in TASK_MODULES
        for modulo in TASK_MODULES:
            importlib.import_module(modulo)
        assert "app.tasks.media_transcribe_audio" in celery_app.tasks

    def test_la_cola_media_existe_y_la_recibe(self) -> None:
        from app.tasks.celery_app import celery_app

        colas = {q.name for q in celery_app.conf.task_queues}
        assert "media" in colas
        assert celery_app.conf.task_routes["app.tasks.media_*"] == {"queue": "media"}

    def test_hay_un_worker_que_consume_la_cola(self) -> None:
        """Una cola sin worker deja los audios encolados para siempre."""
        from pathlib import Path

        import yaml

        compose = Path(__file__).resolve().parents[2] / "docker-compose.yml"
        servicios = yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]

        assert "celery-media" in servicios
        assert "-Q media" in servicios["celery-media"]["command"]

    def test_el_worker_tiene_las_variables_obligatorias(self) -> None:
        """BUG-026: sin JWT_SECRET ni ENCRYPTION_KEY, `Settings` no valida y el
        worker ni siquiera arranca."""
        from pathlib import Path

        import yaml

        compose = Path(__file__).resolve().parents[2] / "docker-compose.yml"
        servicios = yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]
        entorno = {linea.split("=", 1)[0] for linea in servicios["celery-media"]["environment"]}

        assert {"JWT_SECRET", "ENCRYPTION_KEY", "DATABASE_URL", "OPENAI_API_KEY"} <= entorno
        # El audio de Telegram se descarga con getFile, que necesita el token.
        assert "TELEGRAM_CHANNEL_BOT_TOKEN" in entorno
