"""Tests del endpoint WebSocket del Webchat, con un Redis falso y sin base de datos.

El endpoint es la puerta de un visitante anonimo de internet: cada test de rechazo
protege una entrada que no debe pasar (origen ajeno, sesion falsificada, frame
gigante, inundacion de mensajes) y cada test de aceptacion, que un visitante legitimo
conserve su identidad y reciba lo que le corresponde.
"""

import asyncio
import contextlib
import json
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient, WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from app.api.v1 import webchat as api
from app.core.config import get_settings
from app.services.messaging.webchat import canal_de_salida
from app.services.webchat_session import emitir_sesion, verificar_sesion

TOKEN = "token-del-canal-de-prueba"
ORIGEN = "https://cliente.example"
TENANT = uuid.uuid4()
URL = f"/api/v1/webchat/{TOKEN}"
HEADERS = {"origin": ORIGEN}


class FakePubSub:
    """Suscripcion de Redis falsa: una cola por conexion."""

    def __init__(self, redis: "FakeRedis") -> None:
        self.redis = redis
        self.cola: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.canales: set[str] = set()
        self.cerrado = False

    async def subscribe(self, canal: str) -> None:
        self.canales.add(canal)
        self.redis.suscriptores.setdefault(canal, []).append(self)

    async def unsubscribe(self, canal: str) -> None:
        self.canales.discard(canal)
        if self in self.redis.suscriptores.get(canal, []):
            self.redis.suscriptores[canal].remove(self)

    async def close(self) -> None:
        self.cerrado = True

    async def listen(self) -> Any:
        yield {"type": "subscribe", "data": 1}
        while True:
            yield await self.cola.get()


class FakeRedis:
    """Redis falso con contadores, claves y pub/sub."""

    def __init__(self) -> None:
        self.claves: dict[str, int] = {}
        self.suscriptores: dict[str, list[FakePubSub]] = {}
        self.pubsubs: list[FakePubSub] = []
        self.ttls: dict[str, int] = {}

    async def set(self, clave: str, valor: int, ex: int | None = None, nx: bool = False) -> bool:
        if nx and clave in self.claves:
            return False
        self.claves[clave] = valor
        if ex is not None:
            self.ttls[clave] = ex
        return True

    async def incr(self, clave: str) -> int:
        self.claves[clave] = self.claves.get(clave, 0) + 1
        return self.claves[clave]

    async def publish(self, canal: str, mensaje: str) -> int:
        destinos = self.suscriptores.get(canal, [])
        for destino in destinos:
            destino.cola.put_nowait({"type": "message", "data": mensaje})
        return len(destinos)

    def pubsub(self) -> FakePubSub:
        nuevo = FakePubSub(self)
        self.pubsubs.append(nuevo)
        return nuevo


@pytest.fixture
def entorno(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Configura el canal y sustituye Redis, la dedup, la cola y la base.

    Args:
        monkeypatch: Fixture de pytest.

    Returns:
        Espacio de nombres con `redis`, `encolados`, `marcas`, `liberadas`,
        `perdidos` (lo que devuelve la recuperacion) y `consultas_perdidos`.
    """
    from app.tasks import webhook_processor

    s = get_settings()
    monkeypatch.setattr(s, "WEBCHAT_CHANNEL_TOKEN", TOKEN)
    monkeypatch.setattr(s, "WEBCHAT_ALLOWED_ORIGINS", [ORIGEN])
    monkeypatch.setattr(s, "DEFAULT_CLIENT_ID", str(TENANT))
    monkeypatch.setattr(s, "WEBCHAT_HELLO_TIMEOUT_SECONDS", 1.0)

    e = SimpleNamespace(
        redis=FakeRedis(),
        encolados=[],
        marcas=set(),
        liberadas=[],
        perdidos=[],
        consultas_perdidos=[],
        falla_la_cola=False,
    )

    async def _mark_if_new(canal: str, externo: str, **kw: Any) -> bool:
        if externo in e.marcas:
            return False
        e.marcas.add(externo)
        return True

    async def _release(canal: str, externo: str, **kw: Any) -> None:
        e.liberadas.append(externo)
        e.marcas.discard(externo)

    async def _perdidos(client_id: Any, visitor_id: str, last: str | None, limite: int) -> Any:
        e.consultas_perdidos.append((client_id, visitor_id, last, limite))
        return e.perdidos

    def _delay(**kwargs: Any) -> None:
        if e.falla_la_cola:
            raise ConnectionError("broker caido")
        e.encolados.append(kwargs)

    monkeypatch.setattr(api, "get_redis", lambda: e.redis)
    monkeypatch.setattr(api, "mark_if_new", _mark_if_new)
    monkeypatch.setattr(api, "release_mark", _release)
    monkeypatch.setattr(api, "mensajes_perdidos", _perdidos)
    # Como en test_webhooks.py: se sustituye la tarea entera (el endpoint la importa al
    # llamar). Parchear solo `.delay` no basta: Celery resuelve la tarea por hilo y el
    # del threadpool no vería el parche.
    monkeypatch.setattr(
        webhook_processor, "process_incoming_message", SimpleNamespace(delay=_delay)
    )
    api._conexiones.clear()
    return e


@pytest.fixture
def cliente(entorno: SimpleNamespace) -> Iterator[TestClient]:
    """Cliente con una app minima que solo monta el router del Webchat."""
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1/webchat")
    with TestClient(app) as c:
        yield c


@contextlib.contextmanager
def _abrir(
    cliente: TestClient, url: str = URL, headers: dict[str, str] | None = None
) -> Iterator[WebSocketTestSession]:
    """Abre un WebSocket con el Origin permitido por defecto.

    Al salir cierra desde el cliente y espera a que el servidor termine su limpieza:
    `TestClient` cancela la tarea de la app justo despues de salir, y si el handler
    aun esta limpiando la cancelacion se cuela como `CancelledError` (una carrera
    del arnes de pruebas; uvicorn no cancela el handler al desconectar).
    """
    with cliente.websocket_connect(url, headers=HEADERS if headers is None else headers) as ws:
        try:
            yield ws
        finally:
            with contextlib.suppress(Exception):
                ws.close(1000)
            cliente.portal.call(asyncio.sleep, 0.05)


def _hello(ws: WebSocketTestSession, session: str | None = None, **extra: Any) -> dict[str, Any]:
    """Manda el `hello` y devuelve la respuesta."""
    ws.send_json({"type": "hello", "session": session, **extra})
    return ws.receive_json()


def _codigo(ejecutar: Any) -> int:
    """Codigo con el que el servidor cerro la conexion."""
    with pytest.raises(WebSocketDisconnect) as info:
        ejecutar()
    return int(info.value.code)


class TestRechazoAntesDeAceptar:
    def test_un_token_de_canal_incorrecto(self, cliente: TestClient) -> None:
        def intento() -> None:
            with _abrir(cliente, "/api/v1/webchat/otro-token") as ws:
                ws.receive_json()

        assert _codigo(intento) == api.CLOSE_TOKEN_INVALIDO

    def test_con_el_canal_desactivado_ningun_token_vale(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Vacio = desactivado: ni siquiera el token vacio puede colarse."""
        monkeypatch.setattr(get_settings(), "WEBCHAT_CHANNEL_TOKEN", "")

        def intento() -> None:
            with _abrir(cliente, "/api/v1/webchat/x") as ws:
                ws.receive_json()

        assert _codigo(intento) == api.CLOSE_TOKEN_INVALIDO

    @pytest.mark.parametrize(
        "headers",
        [{}, {"origin": "https://evil.example"}, {"origin": "http://cliente.example"}],
    )
    def test_un_origen_ajeno_o_ausente(self, cliente: TestClient, headers: dict[str, str]) -> None:
        """Un navegador siempre manda Origin: sin el, o con otro, no es un widget legitimo."""

        def intento() -> None:
            with _abrir(cliente, headers=headers) as ws:
                ws.receive_json()

        assert _codigo(intento) == api.CLOSE_ORIGEN_NO_PERMITIDO

    def test_el_origen_admite_barra_final_y_mayusculas(self, cliente: TestClient) -> None:
        with _abrir(cliente, headers={"origin": "HTTPS://Cliente.Example/"}) as ws:
            assert _hello(ws)["type"] == "connected"

    def test_sin_lista_propia_se_usan_los_origenes_cors(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "WEBCHAT_ALLOWED_ORIGINS", [])
        monkeypatch.setattr(get_settings(), "CORS_ORIGINS", ["https://dashboard.example"])

        with _abrir(cliente, headers={"origin": "https://dashboard.example"}) as ws:
            assert _hello(ws)["type"] == "connected"

    def test_sin_tenant_configurado(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "DEFAULT_CLIENT_ID", "")

        def intento() -> None:
            with _abrir(cliente) as ws:
                ws.receive_json()

        assert _codigo(intento) == api.CLOSE_ERROR_INTERNO


class TestHello:
    def test_un_visitante_nuevo_recibe_una_sesion_valida(self, cliente: TestClient) -> None:
        with _abrir(cliente) as ws:
            conectado = _hello(ws)

        assert conectado["type"] == "connected"
        assert verificar_sesion(conectado["session"], TENANT) is not None

    def test_la_respuesta_no_expone_el_visitor_id(self, cliente: TestClient) -> None:
        with _abrir(cliente) as ws:
            conectado = _hello(ws)

        assert set(conectado) == {"type", "session"}

    def test_reconectar_con_la_sesion_conserva_al_visitante(self, cliente: TestClient) -> None:
        with _abrir(cliente) as ws:
            token = _hello(ws)["session"]

        with _abrir(cliente) as ws:
            conectado = _hello(ws, token)

        assert conectado["session"] == token

    def test_una_sesion_falsificada_da_un_visitante_nuevo(self, cliente: TestClient) -> None:
        """Adivinar o alterar un token no permite hacerse pasar por otro visitante."""
        ajena = emitir_sesion(uuid.uuid4())  # de otro tenant: la firma no cuadra
        with _abrir(cliente) as ws:
            conectado = _hello(ws, ajena.token)

        assert conectado["session"] != ajena.token
        assert verificar_sesion(conectado["session"], TENANT) != ajena.visitor_id

    def test_una_sesion_caducada_da_un_visitante_nuevo(self, cliente: TestClient) -> None:
        caducada = emitir_sesion(TENANT, ahora=1_000.0)
        with _abrir(cliente) as ws:
            conectado = _hello(ws, caducada.token)

        assert conectado["session"] != caducada.token

    @pytest.mark.parametrize(
        "frame",
        [
            "esto no es json",
            json.dumps({"type": "message", "message_id": "a", "text": "x"}),
            json.dumps({"type": "hello", "sorpresa": 1}),
            json.dumps([1, 2]),
        ],
    )
    def test_un_primer_frame_que_no_es_hello(self, cliente: TestClient, frame: str) -> None:
        def intento() -> None:
            with _abrir(cliente) as ws:
                ws.send_text(frame)
                assert ws.receive_json()["code"] == "invalid_hello"
                ws.receive_json()

        assert _codigo(intento) == api.CLOSE_HELLO_INVALIDO

    def test_sin_hello_a_tiempo_se_cierra(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "WEBCHAT_HELLO_TIMEOUT_SECONDS", 0.2)

        def intento() -> None:
            with _abrir(cliente) as ws:
                assert ws.receive_json()["code"] == "invalid_hello"
                ws.receive_json()

        assert _codigo(intento) == api.CLOSE_HELLO_INVALIDO

    def test_un_hello_demasiado_grande(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "WEBCHAT_MAX_FRAME_BYTES", 64)

        def intento() -> None:
            with _abrir(cliente) as ws:
                ws.send_json({"type": "hello", "name": "x" * 90})
                assert ws.receive_json()["code"] == "invalid_hello"
                ws.receive_json()

        assert _codigo(intento) == api.CLOSE_HELLO_INVALIDO


class TestMensajes:
    def test_un_mensaje_se_encola_como_cualquier_otro_webhook(
        self, cliente: TestClient, entorno: SimpleNamespace
    ) -> None:
        with _abrir(cliente) as ws:
            token = _hello(ws, name="Ada")["session"]
            visitante = verificar_sesion(token, TENANT)
            ws.send_json({"type": "message", "message_id": "m1", "text": "  hola  "})
            ack = ws.receive_json()

        assert ack == {"type": "ack", "message_id": "m1"}
        (llamada,) = entorno.encolados
        assert llamada["provider"] == "webchat"
        assert llamada["channel"] == "webchat"
        mensaje = llamada["normalized_message"]
        assert mensaje["channel"] == "webchat"
        assert mensaje["sender_identifier"] == visitante
        assert mensaje["sender_name"] == "Ada"
        assert mensaje["text"] == "hola"
        assert mensaje["external_message_id"] == f"{visitante}:m1"

    def test_el_visitante_lo_pone_el_servidor_no_el_cliente(
        self, cliente: TestClient, entorno: SimpleNamespace
    ) -> None:
        """Un frame no puede traer un `visitor_id` para escribir en nombre de otro."""
        with _abrir(cliente) as ws:
            _hello(ws)
            ws.send_json(
                {"type": "message", "message_id": "m1", "text": "x", "visitor_id": "b" * 32}
            )
            assert ws.receive_json()["code"] == "invalid_frame"

        assert entorno.encolados == []

    def test_un_reintento_del_cliente_no_se_encola_dos_veces(
        self, cliente: TestClient, entorno: SimpleNamespace
    ) -> None:
        with _abrir(cliente) as ws:
            _hello(ws)
            for _ in range(2):
                ws.send_json({"type": "message", "message_id": "m1", "text": "hola"})
                assert ws.receive_json() == {"type": "ack", "message_id": "m1"}

        assert len(entorno.encolados) == 1

    def test_dos_visitantes_con_el_mismo_message_id_no_chocan(
        self, cliente: TestClient, entorno: SimpleNamespace
    ) -> None:
        for _ in range(2):
            with _abrir(cliente) as ws:
                _hello(ws)
                ws.send_json({"type": "message", "message_id": "igual", "text": "hola"})
                ws.receive_json()

        assert len(entorno.encolados) == 2

    def test_si_la_cola_falla_se_avisa_y_se_libera_la_marca(
        self, cliente: TestClient, entorno: SimpleNamespace
    ) -> None:
        """Sin liberarla, el reintento del cliente se descartaria como duplicado."""
        entorno.falla_la_cola = True
        with _abrir(cliente) as ws:
            _hello(ws)
            ws.send_json({"type": "message", "message_id": "m1", "text": "hola"})
            error = ws.receive_json()

        assert error["code"] == "queue_unavailable"
        assert len(entorno.liberadas) == 1
        assert entorno.marcas == set()

    def test_un_boton_se_encola_como_respuesta_interactiva(
        self, cliente: TestClient, entorno: SimpleNamespace
    ) -> None:
        with _abrir(cliente) as ws:
            _hello(ws)
            ws.send_json(
                {"type": "button_reply", "message_id": "b1", "id": "si", "title": "Si, confirmo"}
            )
            assert ws.receive_json() == {"type": "ack", "message_id": "b1"}

        mensaje = entorno.encolados[0]["normalized_message"]
        assert mensaje["text"] == "Si, confirmo"
        assert mensaje["interactive_response"]["id"] == "si"

    def test_ping_responde_pong(self, cliente: TestClient) -> None:
        with _abrir(cliente) as ws:
            _hello(ws)
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}


class TestLimites:
    @pytest.mark.parametrize(
        "crudo",
        [
            "no es json",
            json.dumps({"type": "typing"}),
            json.dumps({"type": "message", "message_id": "a b", "text": "x"}),
            json.dumps({"type": "message", "message_id": "a", "text": "   "}),
            json.dumps(
                {"type": "message", "message_id": "a", "text": "x", "media_url": "http://x"}
            ),
            json.dumps({"type": "hello"}),
        ],
    )
    def test_un_frame_no_valido_da_error_y_no_cierra(
        self, cliente: TestClient, entorno: SimpleNamespace, crudo: str
    ) -> None:
        with _abrir(cliente) as ws:
            _hello(ws)
            ws.send_text(crudo)
            assert ws.receive_json()["code"] == "invalid_frame"
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}  # sigue viva

        assert entorno.encolados == []

    def test_un_frame_binario_no_se_admite(self, cliente: TestClient) -> None:
        with _abrir(cliente) as ws:
            _hello(ws)
            ws.send_bytes(b"\x00\x01")
            assert ws.receive_json()["code"] == "invalid_frame"

    def test_un_frame_demasiado_grande_cierra_la_conexion(
        self, cliente: TestClient, entorno: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "WEBCHAT_MAX_FRAME_BYTES", 512)

        def intento() -> None:
            with _abrir(cliente) as ws:
                _hello(ws)
                ws.send_json({"type": "message", "message_id": "m", "text": "x" * 2000})
                assert ws.receive_json()["code"] == "frame_too_large"
                ws.receive_json()

        assert _codigo(intento) == api.CLOSE_FRAME_GRANDE
        assert entorno.encolados == []

    def test_un_mensaje_mas_largo_que_el_maximo_se_rechaza(
        self, cliente: TestClient, entorno: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "WEBCHAT_MAX_MESSAGE_CHARS", 10)
        with _abrir(cliente) as ws:
            _hello(ws)
            ws.send_json({"type": "message", "message_id": "m", "text": "x" * 11})
            assert ws.receive_json()["code"] == "message_too_long"
            ws.send_json({"type": "message", "message_id": "m2", "text": "x" * 10})
            assert ws.receive_json()["type"] == "ack"

        assert len(entorno.encolados) == 1

    def test_por_encima_del_limite_por_minuto_no_se_encola(
        self, cliente: TestClient, entorno: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "WEBCHAT_MAX_MESSAGES_PER_MINUTE", 2)
        respuestas = []
        with _abrir(cliente) as ws:
            _hello(ws)
            for i in range(4):
                ws.send_json({"type": "message", "message_id": f"m{i}", "text": "hola"})
                respuestas.append(ws.receive_json())

        assert [r["type"] for r in respuestas] == ["ack", "ack", "error", "error"]
        assert respuestas[2]["code"] == "rate_limited"
        assert len(entorno.encolados) == 2

    def test_el_contador_del_limite_siempre_tiene_caducidad(
        self, cliente: TestClient, entorno: SimpleNamespace
    ) -> None:
        """Sin TTL, un fallo entre INCR y EXPIRE bloquearia al visitante para siempre."""
        with _abrir(cliente) as ws:
            _hello(ws)
            ws.send_json({"type": "message", "message_id": "m", "text": "hola"})
            ws.receive_json()

        (clave,) = [k for k in entorno.redis.claves if k.startswith("webchat:rate:")]
        assert entorno.redis.ttls[clave] == 60

    def test_el_limite_es_por_visitante(
        self, cliente: TestClient, entorno: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "WEBCHAT_MAX_MESSAGES_PER_MINUTE", 1)
        for i in range(2):
            with _abrir(cliente) as ws:
                _hello(ws)
                ws.send_json({"type": "message", "message_id": f"m{i}", "text": "hola"})
                assert ws.receive_json()["type"] == "ack"

    def test_demasiadas_conexiones_del_mismo_visitante(
        self, cliente: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "WEBCHAT_MAX_CONNECTIONS_PER_VISITOR", 1)
        with _abrir(cliente) as primera:
            token = _hello(primera)["session"]

            def intento() -> None:
                with _abrir(cliente) as segunda:
                    segunda.send_json({"type": "hello", "session": token})
                    assert segunda.receive_json()["code"] == "too_many_connections"
                    segunda.receive_json()

            assert _codigo(intento) == api.CLOSE_DEMASIADAS_CONEXIONES

    def test_al_desconectar_se_libera_el_cupo_y_la_suscripcion(
        self, cliente: TestClient, entorno: SimpleNamespace
    ) -> None:
        with _abrir(cliente) as ws:
            _hello(ws)
            assert sum(api._conexiones.values()) == 1

        # El cierre del servidor ocurre en el hilo del portal: se espera a que termine.
        cliente.portal.call(asyncio.sleep, 0.05)
        assert api._conexiones == {}
        assert all(p.cerrado and not p.canales for p in entorno.redis.pubsubs)


class TestEntregaEnVivo:
    def test_lo_publicado_para_el_visitante_le_llega(
        self, cliente: TestClient, entorno: SimpleNamespace
    ) -> None:
        with _abrir(cliente) as ws:
            visitante = verificar_sesion(_hello(ws)["session"], TENANT)
            frame = {"type": "message", "message_id": "r1", "text": "Hola, ¿en que ayudo?"}

            cliente.portal.call(
                entorno.redis.publish, canal_de_salida(TENANT, str(visitante)), json.dumps(frame)
            )

            assert ws.receive_json() == frame

    def test_lo_publicado_para_otro_visitante_no_le_llega(
        self, cliente: TestClient, entorno: SimpleNamespace
    ) -> None:
        with _abrir(cliente) as ws:
            _hello(ws)
            ajeno = {"type": "message", "message_id": "x", "text": "secreto de otro visitante"}
            cliente.portal.call(
                entorno.redis.publish, canal_de_salida(TENANT, "c" * 32), json.dumps(ajeno)
            )
            cliente.portal.call(
                entorno.redis.publish, canal_de_salida(uuid.uuid4(), "c" * 32), json.dumps(ajeno)
            )

            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}  # lo primero que llega es el pong

    def test_dos_pestanas_del_mismo_visitante_reciben_lo_mismo(
        self, cliente: TestClient, entorno: SimpleNamespace
    ) -> None:
        with _abrir(cliente) as una:
            token = _hello(una)["session"]
            with _abrir(cliente) as otra:
                _hello(otra, token)
                frame = {"type": "message", "message_id": "r1", "text": "Hola"}
                cliente.portal.call(
                    entorno.redis.publish,
                    canal_de_salida(TENANT, str(verificar_sesion(token, TENANT))),
                    json.dumps(frame),
                )

                assert una.receive_json() == frame
                assert otra.receive_json() == frame

    def test_un_frame_corrupto_en_el_canal_no_tumba_la_conexion(
        self, cliente: TestClient, entorno: SimpleNamespace
    ) -> None:
        with _abrir(cliente) as ws:
            visitante = verificar_sesion(_hello(ws)["session"], TENANT)
            canal = canal_de_salida(TENANT, str(visitante))
            cliente.portal.call(entorno.redis.publish, canal, "esto no es json")
            bueno = {"type": "message", "message_id": "r2", "text": "sigo aqui"}
            cliente.portal.call(entorno.redis.publish, canal, json.dumps(bueno))

            assert ws.receive_json() == bueno


class TestRecuperacion:
    def test_lo_perdido_llega_despues_de_connected(
        self, cliente: TestClient, entorno: SimpleNamespace
    ) -> None:
        perdidos = [
            {"type": "message", "message_id": "p1", "text": "uno", "timestamp": "t1"},
            {"type": "message", "message_id": "p2", "text": "dos", "timestamp": "t2"},
        ]
        entorno.perdidos = perdidos
        with _abrir(cliente) as ws:
            ws.send_json({"type": "hello", "session": None})
            assert ws.receive_json()["type"] == "connected"
            assert ws.receive_json() == perdidos[0]
            assert ws.receive_json() == perdidos[1]

    def test_se_pide_lo_posterior_al_ultimo_mensaje_solo_con_sesion_valida(
        self, cliente: TestClient, entorno: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "WEBCHAT_REPLAY_LIMIT", 7)
        with _abrir(cliente) as ws:
            token = _hello(ws)["session"]
        visitante = verificar_sesion(token, TENANT)

        with _abrir(cliente) as ws:
            _hello(ws, token, last_message_id="ultimo-visto")

        assert entorno.consultas_perdidos[-1] == (TENANT, visitante, "ultimo-visto", 7)

    def test_un_last_message_id_sin_sesion_valida_no_da_derecho_a_recuperar(
        self, cliente: TestClient, entorno: SimpleNamespace
    ) -> None:
        """Con una sesion falsificada se es un visitante nuevo: no hay nada que recuperar."""
        with _abrir(cliente) as ws:
            _hello(ws, "sesion-inventada", last_message_id="algo")

        _, _, last, _ = entorno.consultas_perdidos[-1]
        assert last is None

    def test_lo_ya_recuperado_no_se_reenvia_en_vivo(
        self, cliente: TestClient, entorno: SimpleNamespace
    ) -> None:
        entorno.perdidos = [
            {"type": "message", "message_id": "p1", "text": "uno", "timestamp": "t"}
        ]
        with _abrir(cliente) as ws:
            visitante = verificar_sesion(_hello(ws)["session"], TENANT)
            assert ws.receive_json()["message_id"] == "p1"
            canal = canal_de_salida(TENANT, str(visitante))
            duplicado = {"type": "message", "message_id": "p1", "text": "uno", "timestamp": "t"}
            nuevo = {"type": "message", "message_id": "n1", "text": "nuevo", "timestamp": "t"}
            cliente.portal.call(entorno.redis.publish, canal, json.dumps(duplicado))
            cliente.portal.call(entorno.redis.publish, canal, json.dumps(nuevo))

            assert ws.receive_json() == nuevo

    def test_se_suscribe_antes_de_leer_la_base(
        self, cliente: TestClient, entorno: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lo publicado entre la lectura y la suscripcion se perderia."""
        visto: dict[str, Any] = {}

        async def _perdidos(client_id: Any, visitor_id: str, last: Any, limite: int) -> list[Any]:
            visto["suscriptores"] = sum(len(v) for v in entorno.redis.suscriptores.values())
            return []

        monkeypatch.setattr(api, "mensajes_perdidos", _perdidos)
        with _abrir(cliente) as ws:
            _hello(ws)

        assert visto["suscriptores"] == 1
