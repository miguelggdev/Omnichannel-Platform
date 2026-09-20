"""Tests de la tarea que transcribe audios entrantes (Sprint 9).

Fijan el contrato de la tarea: transcribe y encola la IA con el texto puesto;
reutiliza una transcripcion ya guardada; escala a un humano con
`transcription_failed` ante un fallo permanente o con los reintentos agotados;
y nunca le quita la conversacion a un humano que ya la tomo.
"""

import uuid
from typing import Any

import pytest

from app.services.transcription import (
    DownloadedMedia,
    PermanentTranscriptionError,
    Transcription,
    TranscriptionError,
)
from app.tasks import ai_processor
from app.tasks import audio_transcription as tarea

CLIENT_ID = str(uuid.uuid4())
CONVERSATION_ID = str(uuid.uuid4())
CONTACT_ID = str(uuid.uuid4())
MESSAGE_ID = str(uuid.uuid4())

MESSAGE_DATA: dict[str, Any] = {
    "channel": "whatsapp",
    "sender_identifier": "573001112233",
    "text": None,
    "media_url": "https://cdn.example.com/voz.ogg",
    "media_type": "audio",
    "external_message_id": "wa.audio.1",
}


class FakeTaskSelf:
    """Sustituto del `self` de una tarea Celery con bind=True."""

    def __init__(self, retries: int = 0, max_retries: int = 2) -> None:
        """Prepara el doble.

        Args:
            retries: Reintentos ya consumidos.
            max_retries: Maximo configurado en la tarea.
        """
        self.request = type("Request", (), {"retries": retries})()
        self.max_retries = max_retries
        self.retry_calls = 0

    def retry(self, exc: Exception | None = None, countdown: float | None = None) -> Exception:
        """Devuelve la excepcion que el codigo relanza como reintento."""
        self.retry_calls += 1
        return RuntimeError("retry solicitado")


class FakeResult:
    """Resultado de `session.execute()`."""

    def __init__(self, fila: Any) -> None:
        self.fila = fila

    def one_or_none(self) -> Any:
        return self.fila

    def scalar_one_or_none(self) -> Any:
        return self.fila


class FakeSession:
    """Sesion que devuelve una fila prefijada y registra los `execute`."""

    def __init__(self, fila: Any) -> None:
        self.fila = fila
        self.sentencias: list[str] = []

    async def execute(self, stmt: Any) -> FakeResult:
        self.sentencias.append(str(stmt.compile(compile_kwargs={"literal_binds": False})))
        return FakeResult(self.fila)


def _tenant_session(sesion: FakeSession) -> Any:
    """Fabrica un `tenant_session` falso que entrega siempre `sesion`."""

    class _Ctx:
        async def __aenter__(self) -> FakeSession:
            return sesion

        async def __aexit__(self, *exc: Any) -> None:
            return None

    return lambda _client_id: _Ctx()


@pytest.fixture
def encolados(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Captura lo que la tarea encola en `process_ai_response`.

    Args:
        monkeypatch: Fixture de pytest.

    Returns:
        Lista de kwargs de cada `.delay()`.
    """
    llamadas: list[dict[str, Any]] = []
    monkeypatch.setattr(ai_processor.process_ai_response, "delay", lambda **kw: llamadas.append(kw))
    return llamadas


@pytest.fixture
def handoffs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Captura los handoffs de emergencia.

    Args:
        monkeypatch: Fixture de pytest.

    Returns:
        Lista de handoffs pedidos.
    """
    llamadas: list[dict[str, Any]] = []

    async def _handoff(
        client_id: str, conversation_id: str, contact_id: str, channel: str, reason: str
    ) -> None:
        llamadas.append({"conversation_id": conversation_id, "reason": reason})

    monkeypatch.setattr(ai_processor, "_emergency_handoff", _handoff)
    return llamadas


def _estado(monkeypatch: pytest.MonkeyPatch, estado: str | None) -> None:
    """Fija el estado que la tarea lee de la conversacion."""

    async def _leer(client_id: Any, conversation_id: Any) -> str | None:
        return estado

    monkeypatch.setattr(tarea, "_estado_conversacion", _leer)


def _ejecutar(self_: FakeTaskSelf | None = None, data: dict[str, Any] | None = None) -> Any:
    """Corre el cuerpo de la tarea (sin pasar por el broker)."""
    return tarea.transcribe_audio_message.run.__func__(  # type: ignore[attr-defined]
        self_ or FakeTaskSelf(),
        CLIENT_ID,
        CONVERSATION_ID,
        CONTACT_ID,
        "whatsapp",
        MESSAGE_ID,
        MESSAGE_DATA if data is None else data,
    )


def _pipeline(monkeypatch: pytest.MonkeyPatch, *, previo: str | None = None) -> dict[str, Any]:
    """Sustituye red, Whisper y persistencia del flujo feliz.

    Args:
        monkeypatch: Fixture de pytest.
        previo: Transcripcion ya guardada por una ejecucion anterior.

    Returns:
        Registro con las llamadas hechas (`descargas`, `whisper`, `guardados`,
        `costos`).
    """
    registro: dict[str, Any] = {"descargas": [], "whisper": [], "guardados": [], "costos": []}

    async def _previa(client_id: Any, message_id: Any) -> str | None:
        return previo

    async def _resolver(media_url: str, channel: str) -> str:
        return media_url

    async def _descargar(url: str, **kw: Any) -> DownloadedMedia:
        registro["descargas"].append((url, kw))
        return DownloadedMedia(content=b"audio", content_type="audio/ogg", url_path="/voz.ogg")

    async def _whisper(audio: bytes, filename: str, **kw: Any) -> Transcription:
        registro["whisper"].append((filename, kw))
        return Transcription(text="quiero una cita", duration_seconds=30.0)

    async def _guardar(client_id: Any, message_id: Any, resultado: Any, model: str) -> None:
        registro["guardados"].append((message_id, resultado.text, model))

    monkeypatch.setattr(tarea, "_leer_transcripcion_previa", _previa)
    monkeypatch.setattr(tarea, "resolve_media_url", _resolver)
    monkeypatch.setattr(tarea, "download_media", _descargar)
    monkeypatch.setattr(tarea, "transcribe_audio", _whisper)
    monkeypatch.setattr(tarea, "_guardar_transcripcion", _guardar)
    monkeypatch.setattr(tarea, "record_tokens", lambda *a, **kw: registro["costos"].append((a, kw)))
    _estado(monkeypatch, "bot_active")
    return registro


class TestFlujoFeliz:
    """Transcribe, guarda y encola la IA con el texto."""

    def test_encola_la_ia_con_el_texto_transcrito(
        self, monkeypatch: pytest.MonkeyPatch, encolados: list[dict[str, Any]]
    ) -> None:
        _pipeline(monkeypatch)

        resultado = _ejecutar()

        assert resultado == {"status": "transcribed"}
        assert len(encolados) == 1
        enviado = encolados[0]
        assert enviado["message_data"]["text"] == "quiero una cita"
        assert enviado["client_id"] == CLIENT_ID
        assert enviado["conversation_id"] == CONVERSATION_ID
        assert enviado["contact_id"] == CONTACT_ID
        assert enviado["channel"] == "whatsapp"

    def test_no_muta_el_message_data_original(
        self, monkeypatch: pytest.MonkeyPatch, encolados: list[dict[str, Any]]
    ) -> None:
        _pipeline(monkeypatch)

        _ejecutar()

        assert MESSAGE_DATA["text"] is None

    def test_guarda_la_transcripcion_en_el_mensaje(
        self, monkeypatch: pytest.MonkeyPatch, encolados: list[dict[str, Any]]
    ) -> None:
        registro = _pipeline(monkeypatch)

        _ejecutar()

        assert len(registro["guardados"]) == 1
        message_id, texto, _model = registro["guardados"][0]
        assert str(message_id) == MESSAGE_ID
        assert texto == "quiero una cita"

    def test_registra_el_costo_por_minuto_de_audio(
        self, monkeypatch: pytest.MonkeyPatch, encolados: list[dict[str, Any]]
    ) -> None:
        """30 s a 0.006 USD/min = 0.003 USD, sin tokens."""
        registro = _pipeline(monkeypatch)

        _ejecutar()

        (args, kwargs) = registro["costos"][0]
        assert args[0] == CLIENT_ID
        assert args[2] == "transcription"
        assert kwargs["cost_usd"] == pytest.approx(0.003)

    def test_descarga_con_el_tope_configurado(
        self, monkeypatch: pytest.MonkeyPatch, encolados: list[dict[str, Any]]
    ) -> None:
        registro = _pipeline(monkeypatch)

        _ejecutar()

        _, kwargs = registro["descargas"][0]
        assert kwargs["max_bytes"] == tarea.get_settings().WHISPER_MAX_AUDIO_BYTES

    def test_el_nombre_del_archivo_sale_del_tipo_descargado(
        self, monkeypatch: pytest.MonkeyPatch, encolados: list[dict[str, Any]]
    ) -> None:
        registro = _pipeline(monkeypatch)

        _ejecutar()

        assert registro["whisper"][0][0] == "audio.ogg"


class TestIdempotencia:
    """Un reintento tras guardar no vuelve a pagar Whisper."""

    def test_con_transcripcion_previa_no_llama_a_whisper(
        self, monkeypatch: pytest.MonkeyPatch, encolados: list[dict[str, Any]]
    ) -> None:
        registro = _pipeline(monkeypatch, previo="texto ya guardado")

        resultado = _ejecutar()

        assert resultado == {"status": "transcribed"}
        assert registro["descargas"] == []
        assert registro["whisper"] == []
        assert registro["guardados"] == []
        assert encolados[0]["message_data"]["text"] == "texto ya guardado"


class TestConversacionDeUnHumano:
    """El bot no responde en una conversacion que un humano ya tiene."""

    @pytest.mark.parametrize("estado", ["human_active", "waiting_human"])
    def test_no_encola_la_ia_pero_conserva_el_texto(
        self, monkeypatch: pytest.MonkeyPatch, encolados: list[dict[str, Any]], estado: str
    ) -> None:
        registro = _pipeline(monkeypatch)
        _estado(monkeypatch, estado)

        resultado = _ejecutar()

        assert resultado == {"status": "human_owned"}
        assert encolados == []
        assert len(registro["guardados"]) == 1

    @pytest.mark.parametrize("estado", ["human_active", "waiting_human"])
    def test_un_fallo_no_le_pisa_la_conversacion_al_humano(
        self,
        monkeypatch: pytest.MonkeyPatch,
        encolados: list[dict[str, Any]],
        handoffs: list[dict[str, Any]],
        estado: str,
    ) -> None:
        _pipeline(monkeypatch)
        _estado(monkeypatch, estado)

        async def _falla(*a: Any, **kw: Any) -> Any:
            raise PermanentTranscriptionError("audio sin voz")

        monkeypatch.setattr(tarea, "transcribe_audio", _falla)

        resultado = _ejecutar()

        assert resultado == {"status": "human_owned"}
        assert handoffs == []


class TestFallos:
    """Escalado a un humano cuando el audio no se puede leer."""

    @pytest.mark.parametrize(
        "error",
        [
            PermanentTranscriptionError("demasiado grande"),
            PermanentTranscriptionError("sin voz"),
        ],
    )
    def test_un_error_permanente_escala_sin_reintentar(
        self,
        monkeypatch: pytest.MonkeyPatch,
        encolados: list[dict[str, Any]],
        handoffs: list[dict[str, Any]],
        error: Exception,
    ) -> None:
        _pipeline(monkeypatch)

        async def _falla(*a: Any, **kw: Any) -> Any:
            raise error

        monkeypatch.setattr(tarea, "download_media", _falla)
        yo = FakeTaskSelf()

        resultado = _ejecutar(yo)

        assert resultado == {"status": "handoff"}
        assert yo.retry_calls == 0
        assert handoffs == [{"conversation_id": CONVERSATION_ID, "reason": "transcription_failed"}]
        assert encolados == []

    def test_un_error_transitorio_reintenta(
        self, monkeypatch: pytest.MonkeyPatch, handoffs: list[dict[str, Any]]
    ) -> None:
        _pipeline(monkeypatch)

        async def _falla(*a: Any, **kw: Any) -> Any:
            raise TranscriptionError("Whisper 500")

        monkeypatch.setattr(tarea, "transcribe_audio", _falla)
        yo = FakeTaskSelf(retries=0)

        with pytest.raises(RuntimeError, match="retry solicitado"):
            _ejecutar(yo)

        assert yo.retry_calls == 1
        assert handoffs == []

    def test_reintentos_agotados_escalan(
        self,
        monkeypatch: pytest.MonkeyPatch,
        encolados: list[dict[str, Any]],
        handoffs: list[dict[str, Any]],
    ) -> None:
        _pipeline(monkeypatch)

        async def _falla(*a: Any, **kw: Any) -> Any:
            raise TranscriptionError("Whisper 500")

        monkeypatch.setattr(tarea, "transcribe_audio", _falla)
        yo = FakeTaskSelf(retries=2)

        resultado = _ejecutar(yo)

        assert resultado == {"status": "handoff"}
        assert yo.retry_calls == 0
        assert handoffs[0]["reason"] == "transcription_failed"
        assert encolados == []

    def test_sin_media_url_escala(
        self, monkeypatch: pytest.MonkeyPatch, handoffs: list[dict[str, Any]]
    ) -> None:
        _pipeline(monkeypatch)

        resultado = _ejecutar(data={**MESSAGE_DATA, "media_url": None})

        assert resultado == {"status": "handoff"}
        assert handoffs[0]["reason"] == "transcription_failed"


class TestResolverUrl:
    """Telegram entrega una referencia, no una URL."""

    async def test_una_url_normal_pasa_tal_cual(self) -> None:
        url = await tarea.resolve_media_url("https://cdn.example.com/a.ogg", "whatsapp")

        assert url == "https://cdn.example.com/a.ogg"

    async def test_telegram_se_resuelve_con_getfile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.agents.nodes import _tenant
        from app.services.messaging import telegram

        visto: dict[str, Any] = {}

        async def _get_file_url(self: Any, file_id: str, config: dict[str, Any]) -> str:
            visto.update(file_id=file_id, config=config)
            return "https://api.telegram.org/file/botT/voice/1.oga"

        monkeypatch.setattr(
            _tenant, "get_channel_config", lambda canal: ("telegram", {"bot_token": "T"})
        )
        monkeypatch.setattr(telegram.TelegramProvider, "get_file_url", _get_file_url)

        url = await tarea.resolve_media_url("telegram-file:AbC123", "telegram")

        assert url == "https://api.telegram.org/file/botT/voice/1.oga"
        assert visto == {"file_id": "AbC123", "config": {"bot_token": "T"}}

    async def test_telegram_sin_configurar_es_permanente(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.agents.nodes import _tenant

        def _sin_config(canal: str) -> Any:
            raise _tenant.ChannelNotConfiguredError("faltan credenciales")

        monkeypatch.setattr(_tenant, "get_channel_config", _sin_config)

        with pytest.raises(PermanentTranscriptionError):
            await tarea.resolve_media_url("telegram-file:AbC123", "telegram")


class TestPersistencia:
    """Lectura y escritura del mensaje, siempre acotadas al tenant."""

    async def test_previa_devuelve_none_si_el_contenido_esta_vacio(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sesion = FakeSession(fila=(None,))
        monkeypatch.setattr(tarea, "tenant_session", _tenant_session(sesion))

        previa = await tarea._leer_transcripcion_previa(uuid.UUID(CLIENT_ID), uuid.UUID(MESSAGE_ID))

        assert previa is None

    async def test_previa_filtra_por_tenant_y_no_solo_por_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sesion = FakeSession(fila=("hola",))
        monkeypatch.setattr(tarea, "tenant_session", _tenant_session(sesion))

        previa = await tarea._leer_transcripcion_previa(uuid.UUID(CLIENT_ID), uuid.UUID(MESSAGE_ID))

        assert previa == "hola"
        assert "client_id" in sesion.sentencias[0]

    async def test_mensaje_inexistente_es_permanente(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sesion = FakeSession(fila=None)
        monkeypatch.setattr(tarea, "tenant_session", _tenant_session(sesion))

        with pytest.raises(PermanentTranscriptionError):
            await tarea._leer_transcripcion_previa(uuid.UUID(CLIENT_ID), uuid.UUID(MESSAGE_ID))

    async def test_guardar_conserva_el_payload_original_y_agrega_la_duracion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mensaje = type("Msg", (), {"content": None, "metadata_": {"id": "wa.1"}})()
        sesion = FakeSession(fila=mensaje)
        monkeypatch.setattr(tarea, "tenant_session", _tenant_session(sesion))

        await tarea._guardar_transcripcion(
            uuid.UUID(CLIENT_ID),
            uuid.UUID(MESSAGE_ID),
            Transcription(text="hola", duration_seconds=4.5),
            "whisper-1",
        )

        assert mensaje.content == "hola"
        assert mensaje.metadata_ == {
            "id": "wa.1",
            "transcription": {"model": "whisper-1", "duration_seconds": 4.5},
        }
        assert "client_id" in sesion.sentencias[0]
