"""Tests del endpoint WebSocket del widget (`app/api/v1/webchat.py`, Sprint 9 Dev B).

No hay Redis, ni Celery, ni base de datos: el cliente Redis, la tarea del worker
y la reposicion de mensajes se sustituyen. La app de los tests monta solo este
router, para no arrastrar el `lifespan` (que verifica DB y Redis de verdad).
"""

import asyncio
import json
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.api.v1 import webchat as webchat_module
from app.services.messaging.webchat import canal_de_sesion, firmar_sesion, sesion_de_token

CHANNEL_TOKEN = "token-de-instalacion-del-widget"
CLIENT_ID = "11111111-1111-1111-1111-111111111111"
URL = f"/api/v1/webchat/{CHANNEL_TOKEN}"


# ─── Dobles ──────────────────────────────────────────────────────────────────


class PubSubDoble:
    """Suscripcion falsa: entrega los frames sembrados y luego se queda quieta."""

    def __init__(self, sembrados: list[dict[str, Any]] | None = None) -> None:
        """Inicializa la suscripcion.

        Args:
            sembrados: Frames que `listen()` entregara antes de bloquearse.
        """
        self.sembrados = sembrados or []
        self.canales: list[str] = []
        self.cerrada = False

    async def subscribe(self, canal: str) -> None:
        """Registra el canal suscrito."""
        self.canales.append(canal)

    async def unsubscribe(self, *canales: str) -> None:
        """No hace nada: el doble no mantiene estado de suscripcion."""

    async def close(self) -> None:
        """Marca la suscripcion como cerrada."""
        self.cerrada = True

    async def listen(self) -> Any:
        """Entrega los frames sembrados y despues espera para siempre.

        Yields:
            Mensajes con la forma que devuelve redis-py.
        """
        yield {"type": "subscribe", "data": 1}
        for frame in self.sembrados:
            yield {"type": "message", "data": json.dumps(frame)}
        await asyncio.Event().wait()


class RedisDoble:
    """Cliente Redis falso: solo sabe crear la suscripcion."""

    def __init__(self, pubsub: PubSubDoble) -> None:
        """Inicializa el cliente.

        Args:
            pubsub: Suscripcion que devolvera `pubsub()`.
        """
        self._pubsub = pubsub

    def pubsub(self) -> PubSubDoble:
        """Devuelve la suscripcion falsa."""
        return self._pubsub


class TareaDoble:
    """Sustituto de la tarea de Celery: registra los encolados."""

    def __init__(self, falla: bool = False) -> None:
        """Inicializa la tarea.

        Args:
            falla: Si `delay()` debe simular un broker caido.
        """
        self.llamadas: list[dict[str, Any]] = []
        self.falla = falla

    def delay(self, **kwargs: Any) -> None:
        """Registra el encolado o simula un broker caido."""
        if self.falla:
            raise ConnectionError("broker no disponible")
        self.llamadas.append(kwargs)


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configura el canal: token de instalacion y tenant por defecto."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "WEBCHAT_CHANNEL_TOKEN", CHANNEL_TOKEN)
    monkeypatch.setattr(get_settings(), "DEFAULT_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(get_settings(), "WEBCHAT_RATE_LIMIT_PER_MINUTE", 30)


@pytest.fixture
def pubsub() -> PubSubDoble:
    """Suscripcion falsa sin frames sembrados."""
    return PubSubDoble()


@pytest.fixture
def tarea(monkeypatch: pytest.MonkeyPatch) -> TareaDoble:
    """Sustituye la tarea del worker de webhooks."""
    import app.tasks.webhook_processor as processor_module

    doble = TareaDoble()
    monkeypatch.setattr(processor_module, "process_incoming_message", doble)
    return doble


@pytest.fixture
def cliente(monkeypatch: pytest.MonkeyPatch, entorno: None, pubsub: PubSubDoble) -> TestClient:
    """Cliente de test con solo el router de webchat montado."""
    import app.services.dedup as dedup_module

    monkeypatch.setattr(dedup_module, "get_redis", lambda: RedisDoble(pubsub))

    app = FastAPI()
    app.include_router(webchat_module.router, prefix="/api/v1/webchat")
    return TestClient(app)


def _sin_reposicion(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Sustituye la reposicion de mensajes y devuelve el registro de llamadas.

    Args:
        monkeypatch: Utilidad de pytest.

    Returns:
        Lista donde se anotan los argumentos de cada llamada.
    """
    llamadas: list[tuple[Any, ...]] = []

    async def _falsa(*args: Any) -> list[dict[str, Any]]:
        llamadas.append(args)
        return []

    monkeypatch.setattr(webchat_module, "mensajes_perdidos", _falsa)
    return llamadas


# ─── Autenticacion del canal ─────────────────────────────────────────────────


class TestTokenDeCanal:
    """El token de la URL identifica la instalacion del widget."""

    def test_token_invalido_cierra_con_4001(self, cliente: TestClient) -> None:
        from starlette.websockets import WebSocketDisconnect

        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            cliente.websocket_connect("/api/v1/webchat/otro-token") as ws,
        ):
            ws.receive_json()
        assert excinfo.value.code == webchat_module.CIERRE_TOKEN_INVALIDO

    def test_canal_sin_configurar_cierra_con_4001(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un token vacio en el entorno no debe validar contra una URL vacia."""
        from starlette.websockets import WebSocketDisconnect

        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "WEBCHAT_CHANNEL_TOKEN", "")
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            cliente.websocket_connect(URL) as ws,
        ):
            ws.receive_json()
        assert excinfo.value.code == webchat_module.CIERRE_TOKEN_INVALIDO

    def test_sin_default_client_id_cierra(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from starlette.websockets import WebSocketDisconnect

        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "DEFAULT_CLIENT_ID", "")
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            cliente.websocket_connect(URL) as ws,
        ):
            ws.receive_json()
        assert excinfo.value.code == webchat_module.CIERRE_TOKEN_INVALIDO

    def test_resolver_tenant_devuelve_el_uuid(self, entorno: None) -> None:
        assert webchat_module.resolver_tenant(CHANNEL_TOKEN) == UUID(CLIENT_ID)


# ─── Sesion ──────────────────────────────────────────────────────────────────


class TestSesion:
    """El token de sesion lo emite y lo verifica el servidor."""

    def test_la_conexion_nueva_recibe_un_token_firmado(self, cliente: TestClient) -> None:
        with cliente.websocket_connect(URL) as ws:
            frame = ws.receive_json()

        assert frame["type"] == "connected"
        assert sesion_de_token(frame["session"]) is not None

    def test_reconectar_con_el_token_propio_conserva_la_sesion(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _sin_reposicion(monkeypatch)
        with cliente.websocket_connect(URL) as ws:
            primera = ws.receive_json()["session"]

        with cliente.websocket_connect(f"{URL}?session={primera}") as ws:
            segunda = ws.receive_json()["session"]

        assert sesion_de_token(segunda) == sesion_de_token(primera)

    def test_un_token_de_sesion_falsificado_no_se_acepta(self, cliente: TestClient) -> None:
        """Reclamar la sesion de otro devuelve una sesion nueva, no la suya."""
        victima = "f" * 32
        falsificado = f"{victima}.firmainventada"

        with cliente.websocket_connect(f"{URL}?session={falsificado}") as ws:
            frame = ws.receive_json()

        asignada = sesion_de_token(frame["session"])
        assert asignada is not None
        assert asignada != victima

    def test_el_socket_se_suscribe_al_canal_de_su_sesion(
        self, cliente: TestClient, pubsub: PubSubDoble
    ) -> None:
        with cliente.websocket_connect(URL) as ws:
            sesion = sesion_de_token(ws.receive_json()["session"])

        assert pubsub.canales == [canal_de_sesion(UUID(CLIENT_ID), str(sesion))]


# ─── Reposicion al reconectar ────────────────────────────────────────────────


class TestReposicion:
    """Lo que el agente respondio mientras no estaba conectado."""

    def test_se_repone_lo_perdido(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        perdidos = [
            {"type": "message", "message_id": "wc-1", "text": "seguimos?", "timestamp": "t"}
        ]

        async def _falsa(*args: Any) -> list[dict[str, Any]]:
            return perdidos

        monkeypatch.setattr(webchat_module, "mensajes_perdidos", _falsa)
        token = firmar_sesion("b" * 32)

        with cliente.websocket_connect(f"{URL}?session={token}&last_message_id=wc-0") as ws:
            ws.receive_json()  # connected
            assert ws.receive_json() == perdidos[0]

    def test_una_sesion_nueva_no_repone_nada(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin token de sesion valido no hay historial que reclamar."""
        llamadas = _sin_reposicion(monkeypatch)

        with cliente.websocket_connect(f"{URL}?last_message_id=wc-0") as ws:
            ws.receive_json()

        assert llamadas == []

    def test_un_fallo_al_reponer_no_tumba_la_conexion(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch, tarea: TareaDoble
    ) -> None:
        async def _revienta(*args: Any) -> list[dict[str, Any]]:
            raise RuntimeError("base caida")

        monkeypatch.setattr(webchat_module, "mensajes_perdidos", _revienta)
        token = firmar_sesion("c" * 32)

        with cliente.websocket_connect(f"{URL}?session={token}&last_message_id=wc-0") as ws:
            ws.receive_json()  # connected
            ws.send_json({"type": "message", "text": "hola"})
            assert ws.receive_json()["type"] == "ack"


# ─── Entrada ─────────────────────────────────────────────────────────────────


class TestEntrada:
    """Frames que manda el widget."""

    def test_un_mensaje_se_encola_y_se_confirma(
        self, cliente: TestClient, tarea: TareaDoble
    ) -> None:
        with cliente.websocket_connect(URL) as ws:
            sesion = sesion_de_token(ws.receive_json()["session"])
            ws.send_json({"type": "message", "text": "quiero una cita", "client_message_id": "7"})
            ack = ws.receive_json()

        assert ack["type"] == "ack"
        assert ack["client_message_id"] == "7"

        encolado = tarea.llamadas[0]
        assert encolado["provider"] == "webchat"
        assert encolado["channel"] == "webchat"
        mensaje = encolado["normalized_message"]
        assert mensaje["channel"] == "webchat"
        assert mensaje["text"] == "quiero una cita"
        assert mensaje["sender_identifier"] == sesion
        assert mensaje["external_message_id"] == ack["message_id"]

    def test_el_session_id_que_manda_el_cliente_se_ignora(
        self, cliente: TestClient, tarea: TareaDoble
    ) -> None:
        """El mensaje es de la sesion del socket, no de la que diga el frame."""
        with cliente.websocket_connect(URL) as ws:
            sesion = sesion_de_token(ws.receive_json()["session"])
            ws.send_json({"type": "message", "text": "hola", "session_id": "d" * 32})
            ws.receive_json()

        assert tarea.llamadas[0]["normalized_message"]["sender_identifier"] == sesion

    def test_el_message_id_del_cliente_no_es_el_id_externo(
        self, cliente: TestClient, tarea: TareaDoble
    ) -> None:
        """Si lo fuera, un visitante podria descartar los mensajes de otro."""
        with cliente.websocket_connect(URL) as ws:
            ws.receive_json()
            ws.send_json({"type": "message", "text": "hola", "message_id": "wc-ajeno"})
            ws.receive_json()

        assert tarea.llamadas[0]["normalized_message"]["external_message_id"] != "wc-ajeno"

    @pytest.mark.parametrize("tipo", ["typing", "read_receipt", "ping"])
    def test_los_frames_de_control_no_encolan_nada(
        self, cliente: TestClient, tarea: TareaDoble, tipo: str
    ) -> None:
        with cliente.websocket_connect(URL) as ws:
            ws.receive_json()
            ws.send_json({"type": tipo})
            ws.send_json({"type": "message", "text": "hola"})
            assert ws.receive_json()["type"] == "ack"

        assert len(tarea.llamadas) == 1

    def test_un_mensaje_vacio_no_encola_ni_confirma(
        self, cliente: TestClient, tarea: TareaDoble
    ) -> None:
        with cliente.websocket_connect(URL) as ws:
            ws.receive_json()
            ws.send_json({"type": "message", "text": "   "})
            ws.send_json({"type": "message", "text": "ahora si"})
            ack = ws.receive_json()

        assert ack["type"] == "ack"
        assert len(tarea.llamadas) == 1

    def test_un_frame_que_no_es_json_devuelve_error(
        self, cliente: TestClient, tarea: TareaDoble
    ) -> None:
        with cliente.websocket_connect(URL) as ws:
            ws.receive_json()
            ws.send_text("{esto no es json")
            error = ws.receive_json()

        assert error["type"] == "error"
        assert error["error_code"] == webchat_module.FRAME_INVALIDO
        assert tarea.llamadas == []

    def test_un_frame_demasiado_grande_se_rechaza(
        self, cliente: TestClient, tarea: TareaDoble
    ) -> None:
        with cliente.websocket_connect(URL) as ws:
            ws.receive_json()
            ws.send_text("x" * (webchat_module.MAX_FRAME_CHARS + 1))
            error = ws.receive_json()

        assert error["error_code"] == webchat_module.FRAME_DEMASIADO_GRANDE
        assert tarea.llamadas == []

    def test_un_broker_caido_avisa_sin_filtrar_el_motivo(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.tasks.webhook_processor as processor_module

        monkeypatch.setattr(processor_module, "process_incoming_message", TareaDoble(falla=True))

        with cliente.websocket_connect(URL) as ws:
            ws.receive_json()
            ws.send_json({"type": "message", "text": "hola"})
            error = ws.receive_json()

        assert error["type"] == "error"
        assert error["error_code"] == "QUEUE_UNAVAILABLE"
        assert "broker" not in json.dumps(error)

    def test_pasado_el_limite_se_cierra_el_socket(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch, tarea: TareaDoble
    ) -> None:
        from starlette.websockets import WebSocketDisconnect

        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "WEBCHAT_RATE_LIMIT_PER_MINUTE", 2)

        def _spamear() -> None:
            """Manda cuatro mensajes seguidos con el limite en dos."""
            with cliente.websocket_connect(URL) as ws:
                ws.receive_json()
                for i in range(4):
                    ws.send_json({"type": "message", "text": f"spam {i}"})
                    ws.receive_json()

        with pytest.raises(WebSocketDisconnect) as excinfo:
            _spamear()
        assert excinfo.value.code == webchat_module.CIERRE_EXCESO_DE_MENSAJES
        assert len(tarea.llamadas) == 2


# ─── Salida ──────────────────────────────────────────────────────────────────


class TestSalida:
    """Lo que publica el worker llega al socket."""

    def test_lo_publicado_en_redis_llega_al_widget(
        self, monkeypatch: pytest.MonkeyPatch, entorno: None
    ) -> None:
        import app.services.dedup as dedup_module

        respuesta = {"type": "message", "message_id": "wc-9", "text": "claro que si"}
        pubsub = PubSubDoble(sembrados=[respuesta])
        monkeypatch.setattr(dedup_module, "get_redis", lambda: RedisDoble(pubsub))

        app = FastAPI()
        app.include_router(webchat_module.router, prefix="/api/v1/webchat")

        with TestClient(app).websocket_connect(URL) as ws:
            ws.receive_json()  # connected
            assert ws.receive_json() == respuesta


# ─── Limitador ───────────────────────────────────────────────────────────────


class TestLimitador:
    """Ventana deslizante de mensajes por socket."""

    def test_permite_hasta_el_maximo(self) -> None:
        limitador = webchat_module._Limitador(maximo=2)
        assert limitador.permite(0.0)
        assert limitador.permite(0.1)
        assert not limitador.permite(0.2)

    def test_la_ventana_se_desliza(self) -> None:
        limitador = webchat_module._Limitador(maximo=1, ventana=10.0)
        assert limitador.permite(0.0)
        assert not limitador.permite(5.0)
        assert limitador.permite(11.0)

    def test_maximo_cero_es_sin_limite(self) -> None:
        limitador = webchat_module._Limitador(maximo=0)
        assert all(limitador.permite(float(i)) for i in range(100))


# ─── Cableado ────────────────────────────────────────────────────────────────


class TestCableado:
    """El router tiene que estar montado en la app real."""

    def test_la_ruta_existe_en_create_app(self) -> None:
        from app.main import create_app

        rutas = {getattr(ruta, "path", "") for ruta in create_app().routes}
        assert "/api/v1/webchat/{channel_token}" in rutas
