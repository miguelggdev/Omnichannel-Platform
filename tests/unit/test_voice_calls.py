"""Tests de la llamada en curso: token, sesion, webhooks y WebSocket (Sprint 13)."""

import asyncio
import base64
import contextlib
import json
import math
import uuid
import xml.etree.ElementTree as ET
from array import array
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.core.config import get_settings
from app.models.call_record import CALL_FINAL_STATUSES, CALL_STATUSES, CallRecord
from app.services.messaging.voice_provider import canal_de_salida, twilio_signature
from app.services.voice import audio
from app.services.voice import call_manager as cm
from app.services.voice.stream_token import StreamClaims, emitir_token, verificar_token
from app.tasks.voice_tasks import direccion_reconocida
from tests.unit.test_webchat_endpoint import FakeRedis

TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
AUTH_TOKEN = "twilio-auth-token"
BASE = "https://api.ejemplo.com"
CLIENTE = "+573001234567"
PLATAFORMA = "+15005550006"


def _tono(ms: int, amplitud: int) -> bytes:
    n = 8 * ms
    return array(
        "h", (int(amplitud * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(n))
    ).tobytes()


def _media(pcm: bytes) -> list[str]:
    """Tramas de 20 ms en mu-law base64, como las manda Twilio."""
    return [base64.b64encode(t).decode() for t in audio.frames(audio.pcm16_to_mulaw(pcm), 160)]


@pytest.fixture
def ajustes(monkeypatch: pytest.MonkeyPatch) -> Any:
    s = get_settings()
    for clave, valor in {
        "DEFAULT_CLIENT_ID": str(TENANT),
        "TWILIO_AUTH_TOKEN": AUTH_TOKEN,
        "TWILIO_ACCOUNT_SID": "AC123",
        "TWILIO_PHONE_NUMBER": PLATAFORMA,
        "VOICE_PUBLIC_BASE_URL": BASE,
        "VOICE_END_OF_SPEECH_SILENCE_MS": 200,
        "VOICE_BARGE_IN_MIN_MS": 100,
    }.items():
        monkeypatch.setattr(s, clave, valor)
    return s


def _claims(**cambios: Any) -> StreamClaims:
    datos = {
        "client_id": str(TENANT),
        "call_sid": "CA1",
        "direction": "inbound",
        "phone_from": CLIENTE,
        "phone_to": PLATAFORMA,
        "contact_phone": CLIENTE,
        "exp": 2**40,
    }
    datos.update(cambios)
    return StreamClaims(**datos)


# ─── Token del stream ────────────────────────────────────────────────────────


class TestStreamToken:
    def test_ida_y_vuelta(self, ajustes: Any) -> None:
        token = emitir_token(
            client_id=str(TENANT),
            call_sid="CA1",
            direction="inbound",
            phone_from=CLIENTE,
            phone_to=PLATAFORMA,
            contact_phone=CLIENTE,
        )
        claims = verificar_token(token, "CA1")
        assert claims is not None
        assert claims.contact_phone == CLIENTE
        assert claims.client_id == str(TENANT)

    def test_no_sirve_para_otra_llamada(self, ajustes: Any) -> None:
        token = emitir_token(
            client_id=str(TENANT),
            call_sid="CA1",
            direction="inbound",
            phone_from=CLIENTE,
            phone_to=PLATAFORMA,
            contact_phone=CLIENTE,
        )
        assert verificar_token(token, "CA2") is None

    def test_caduca(self, ajustes: Any) -> None:
        token = emitir_token(
            client_id=str(TENANT),
            call_sid="CA1",
            direction="inbound",
            phone_from=CLIENTE,
            phone_to=PLATAFORMA,
            contact_phone=CLIENTE,
            ahora=1000,
        )
        assert (
            verificar_token(token, "CA1", ahora=1000 + ajustes.VOICE_STREAM_TOKEN_TTL_SECONDS + 1)
            is None
        )

    def test_un_payload_alterado_no_verifica(self, ajustes: Any) -> None:
        token = emitir_token(
            client_id=str(TENANT),
            call_sid="CA1",
            direction="inbound",
            phone_from=CLIENTE,
            phone_to=PLATAFORMA,
            contact_phone=CLIENTE,
        )
        _payload, firma = token.split(".")
        otro = emitir_token(
            client_id=str(uuid.uuid4()),
            call_sid="CA1",
            direction="inbound",
            phone_from=CLIENTE,
            phone_to=PLATAFORMA,
            contact_phone=CLIENTE,
        ).split(".")[0]
        assert verificar_token(f"{otro}.{firma}", "CA1") is None

    @pytest.mark.parametrize("token", [None, "", "sin-punto", "a.b.c", "!!.!!"])
    def test_basura(self, ajustes: Any, token: str | None) -> None:
        assert verificar_token(token, "CA1") is None

    def test_no_es_intercambiable_con_otra_clave(
        self, ajustes: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = emitir_token(
            client_id=str(TENANT),
            call_sid="CA1",
            direction="inbound",
            phone_from=CLIENTE,
            phone_to=PLATAFORMA,
            contact_phone=CLIENTE,
        )
        monkeypatch.setattr(ajustes, "JWT_SECRET", "otra-clave-completamente-distinta-32ch")
        assert verificar_token(token, "CA1") is None


# ─── CallSession ─────────────────────────────────────────────────────────────


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    falso = FakeRedis()
    marcas: set[str] = set()

    async def _mark(canal: str, externo: str, **kw: Any) -> bool:
        if (canal, externo) in marcas:
            return False
        marcas.add((canal, externo))
        return True

    monkeypatch.setattr(cm, "get_redis", lambda: falso)
    monkeypatch.setattr(cm, "mark_if_new", _mark)
    monkeypatch.setattr(cm, "release_mark", AsyncMock())
    return falso


class _Sesion:
    """Arma una `CallSession` con dobles y registra lo que manda a Twilio."""

    def __init__(self, textos: list[str] | None = None, audio_tts: bytes = b"\xff" * 480) -> None:
        self.enviados: list[dict[str, Any]] = []
        self.mensajes: list[dict[str, Any]] = []
        self.registros: list[tuple[str, str, dict[str, Any]]] = []
        pendientes = list(textos or [])

        async def _enviar(evento: dict[str, Any]) -> None:
            self.enviados.append(evento)

        async def _transcribir(pcm: bytes) -> str | None:
            return pendientes.pop(0) if pendientes else None

        self.sintetizar = AsyncMock(return_value=audio_tts)
        self.sesion = cm.CallSession(
            _claims(),
            "MZ1",
            _enviar,
            synthesize=self.sintetizar,
            transcriber=_transcribir,
            enqueue_message=self.mensajes.append,
            enqueue_record=lambda c, s, d: self.registros.append((c, s, d)),
        )

    def eventos(self, tipo: str) -> list[dict[str, Any]]:
        return [e for e in self.enviados if e["event"] == tipo]


async def _hablar(sesion: cm.CallSession, pcm: bytes) -> None:
    for trama in _media(pcm):
        await sesion.on_media(trama)


async def _esperar(condicion: Any, intentos: int = 100) -> None:
    for _ in range(intentos):
        if condicion():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("la condicion no se cumplio a tiempo")


class TestCallSession:
    async def test_cada_frase_se_encola_como_mensaje_del_canal_voice(
        self, ajustes: Any, redis: FakeRedis
    ) -> None:
        arnes = _Sesion(textos=["quiero una cita", "para el martes"])
        await arnes.sesion.start()
        await _hablar(arnes.sesion, _tono(400, 3000) + _tono(220, 0))
        await _hablar(arnes.sesion, _tono(400, 3000) + _tono(220, 0))
        await arnes.sesion.close("stop")

        assert [m["text"] for m in arnes.mensajes] == ["quiero una cita", "para el martes"]
        primero = arnes.mensajes[0]
        assert primero["channel"] == "voice"
        assert primero["sender_identifier"] == CLIENTE
        assert [m["external_message_id"] for m in arnes.mensajes] == ["CA1:1", "CA1:2"]

    async def test_al_colgar_se_guarda_la_transcripcion_completa(
        self, ajustes: Any, redis: FakeRedis
    ) -> None:
        arnes = _Sesion(textos=["hola"])
        await arnes.sesion.start()
        await _hablar(arnes.sesion, _tono(400, 3000) + _tono(220, 0))
        await arnes.sesion.close("stop")

        [(cliente, sid, datos)] = arnes.registros
        assert (cliente, sid) == (str(TENANT), "CA1")
        assert [t["role"] for t in datos["transcript"]] == ["agent", "caller"]
        assert datos["transcript"][0]["text"] == ajustes.VOICE_WELCOME_MESSAGE
        assert datos["ended_at"]

    async def test_una_respuesta_publicada_se_sintetiza_y_se_manda_con_marca(
        self, ajustes: Any, redis: FakeRedis
    ) -> None:
        arnes = _Sesion()
        await arnes.sesion.start()
        await redis.publish(
            canal_de_salida(TENANT, CLIENTE),
            '{"type": "say", "message_id": "m1", "text": "Claro, el martes"}',
        )
        await _esperar(lambda: arnes.eventos("mark"))
        await arnes.sesion.close("stop")

        arnes.sintetizar.assert_awaited_once_with("Claro, el martes", str(TENANT))
        medias = arnes.eventos("media")
        assert len(medias) == 3  # 480 bytes en tramas de 160
        assert all(e["streamSid"] == "MZ1" for e in medias)
        assert arnes.eventos("mark")[0]["mark"]["name"] == "m1"
        assert "Claro, el martes" in [t["text"] for t in arnes.sesion.transcript]

    async def test_el_agente_habla_hasta_que_twilio_confirma_la_marca(
        self, ajustes: Any, redis: FakeRedis
    ) -> None:
        arnes = _Sesion()
        await arnes.sesion.start()
        await redis.publish(
            canal_de_salida(TENANT, CLIENTE), '{"type": "say", "message_id": "m1", "text": "Hola"}'
        )
        await _esperar(lambda: arnes.eventos("mark"))
        assert arnes.sesion.agente_hablando
        arnes.sesion.on_mark("m1")
        assert not arnes.sesion.agente_hablando
        await arnes.sesion.close("stop")

    async def test_barge_in_calla_al_agente_y_vacia_la_cola_de_twilio(
        self, ajustes: Any, redis: FakeRedis
    ) -> None:
        arnes = _Sesion()
        await arnes.sesion.start()
        await redis.publish(
            canal_de_salida(TENANT, CLIENTE),
            '{"type": "say", "message_id": "m1", "text": "Una respuesta larga"}',
        )
        await _esperar(lambda: arnes.eventos("mark"))

        # El cliente habla fuerte 120 ms mientras suena la respuesta.
        await _hablar(arnes.sesion, _tono(120, 5000))

        assert arnes.eventos("clear") == [{"event": "clear", "streamSid": "MZ1"}]
        assert not arnes.sesion.agente_hablando
        await arnes.sesion.close("stop")

    async def test_sin_el_agente_hablando_la_voz_no_es_barge_in(
        self, ajustes: Any, redis: FakeRedis
    ) -> None:
        arnes = _Sesion()
        await arnes.sesion.start()
        await _hablar(arnes.sesion, _tono(300, 5000))
        await arnes.sesion.close("stop")
        assert arnes.eventos("clear") == []

    async def test_una_respuesta_de_otro_llamante_no_se_dice(
        self, ajustes: Any, redis: FakeRedis
    ) -> None:
        arnes = _Sesion()
        await arnes.sesion.start()
        entregados = await redis.publish(
            canal_de_salida(TENANT, "+573009999999"),
            '{"type": "say", "message_id": "m1", "text": "Hola"}',
        )
        await asyncio.sleep(0.05)
        await arnes.sesion.close("stop")
        assert entregados == 0
        assert arnes.eventos("media") == []

    async def test_un_fallo_del_tts_no_corta_la_llamada(
        self, ajustes: Any, redis: FakeRedis
    ) -> None:
        arnes = _Sesion()
        arnes.sintetizar.side_effect = [RuntimeError("tts caido"), b"\xff" * 160]
        await arnes.sesion.start()
        canal = canal_de_salida(TENANT, CLIENTE)
        await redis.publish(canal, '{"type": "say", "message_id": "m1", "text": "uno"}')
        await redis.publish(canal, '{"type": "say", "message_id": "m2", "text": "dos"}')
        await _esperar(lambda: arnes.eventos("mark"))
        await arnes.sesion.close("stop")
        assert [e["mark"]["name"] for e in arnes.eventos("mark")] == ["m2"]

    async def test_audio_no_valido_se_ignora(self, ajustes: Any, redis: FakeRedis) -> None:
        arnes = _Sesion()
        await arnes.sesion.start()
        await arnes.sesion.on_media("esto no es base64!!")
        await arnes.sesion.close("stop")

    async def test_una_frase_duplicada_no_se_encola_dos_veces(
        self, ajustes: Any, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cm, "mark_if_new", AsyncMock(return_value=False))
        arnes = _Sesion(textos=["hola"])
        await arnes.sesion.start()
        await _hablar(arnes.sesion, _tono(400, 3000) + _tono(220, 0))
        await arnes.sesion.close("stop")
        assert arnes.mensajes == []


# ─── Webhooks de Twilio ──────────────────────────────────────────────────────


@pytest.fixture
def registros(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from app.tasks import voice_tasks

    encolados: list[dict[str, Any]] = []
    monkeypatch.setattr(
        voice_tasks, "save_call_record", SimpleNamespace(delay=lambda **kw: encolados.append(kw))
    )
    return encolados


def _firmado(path: str, params: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    cuerpo = urlencode(params).encode()
    firma = twilio_signature(f"{BASE}{path}", list(params.items()), AUTH_TOKEN)
    return cuerpo, {
        "X-Twilio-Signature": firma,
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _twiml(texto: str) -> ET.Element:
    """Parsea el TwiML que devolvio nuestro propio endpoint (no es entrada externa)."""
    return ET.fromstring(texto)  # noqa: S314


LLAMADA = {
    "CallSid": "CA1",
    "From": CLIENTE,
    "To": PLATAFORMA,
    "Direction": "inbound",
    "CallStatus": "ringing",
}


class TestWebhooks:
    async def test_incoming_devuelve_twiml_con_stream_y_token(
        self, ajustes: Any, registros: list[dict[str, Any]], api_client: Any
    ) -> None:
        cuerpo, headers = _firmado("/api/v1/voice/twilio/incoming", LLAMADA)
        # El Host del request no puede decidir a donde va el audio.
        respuesta = await api_client.post(
            "/api/v1/voice/twilio/incoming", content=cuerpo, headers={**headers, "Host": "evil.com"}
        )

        assert respuesta.status_code == 200, respuesta.text
        assert respuesta.headers["content-type"].startswith("application/xml")
        raiz = _twiml(respuesta.text)
        stream = raiz.find("./Connect/Stream")
        assert stream is not None
        assert stream.get("url") == "wss://api.ejemplo.com/api/v1/voice/stream"
        token = stream.find("./Parameter[@name='token']").get("value")  # type: ignore[union-attr]
        claims = verificar_token(token, "CA1")
        assert claims is not None
        assert claims.contact_phone == CLIENTE
        assert claims.direction == "inbound"
        assert raiz.find("./Say").text == ajustes.VOICE_WELCOME_MESSAGE  # type: ignore[union-attr]
        assert registros[0]["call_sid"] == "CA1"
        assert registros[0]["datos"]["direction"] == "inbound"

    async def test_hostname_con_caracteres_de_xml_no_rompe_el_twiml(
        self,
        ajustes: Any,
        registros: list[dict[str, Any]],
        api_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(ajustes, "VOICE_WELCOME_MESSAGE", 'Hola & "bienvenido" <cliente>')
        cuerpo, headers = _firmado("/api/v1/voice/twilio/incoming", LLAMADA)
        respuesta = await api_client.post(
            "/api/v1/voice/twilio/incoming", content=cuerpo, headers=headers
        )
        assert _twiml(respuesta.text).find("./Say").text == 'Hola & "bienvenido" <cliente>'  # type: ignore[union-attr]

    async def test_una_saliente_contestada_saluda_distinto_y_el_contacto_es_el_destino(
        self, ajustes: Any, registros: list[dict[str, Any]], api_client: Any
    ) -> None:
        params = {**LLAMADA, "From": PLATAFORMA, "To": CLIENTE, "Direction": "outbound-api"}
        cuerpo, headers = _firmado("/api/v1/voice/twilio/incoming", params)
        respuesta = await api_client.post(
            "/api/v1/voice/twilio/incoming", content=cuerpo, headers=headers
        )

        raiz = _twiml(respuesta.text)
        assert raiz.find("./Say").text == ajustes.VOICE_OUTBOUND_WELCOME_MESSAGE  # type: ignore[union-attr]
        token = raiz.find("./Connect/Stream/Parameter").get("value")  # type: ignore[union-attr]
        claims = verificar_token(token, "CA1")
        assert claims is not None
        assert claims.contact_phone == CLIENTE
        assert claims.direction == "outbound"

    @pytest.mark.parametrize(
        "path", ["/api/v1/voice/twilio/incoming", "/api/v1/voice/twilio/status"]
    )
    async def test_firma_invalida_es_401_y_no_encola(
        self, ajustes: Any, registros: list[dict[str, Any]], api_client: Any, path: str
    ) -> None:
        cuerpo, headers = _firmado(path, LLAMADA)
        headers["X-Twilio-Signature"] = "falsa"
        respuesta = await api_client.post(path, content=cuerpo, headers=headers)
        assert respuesta.status_code == 401
        assert respuesta.json()["error_code"] == "INVALID_WEBHOOK_SIGNATURE"
        assert registros == []

    async def test_sin_url_publica_el_canal_no_esta_configurado(
        self,
        ajustes: Any,
        registros: list[dict[str, Any]],
        api_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cuerpo, headers = _firmado("/api/v1/voice/twilio/incoming", LLAMADA)
        monkeypatch.setattr(ajustes, "VOICE_PUBLIC_BASE_URL", "")
        respuesta = await api_client.post(
            "/api/v1/voice/twilio/incoming", content=cuerpo, headers=headers
        )
        assert respuesta.status_code == 503

    async def test_status_final_encola_estado_duracion_y_fin(
        self, ajustes: Any, registros: list[dict[str, Any]], api_client: Any
    ) -> None:
        params = {**LLAMADA, "CallStatus": "completed", "CallDuration": "42"}
        cuerpo, headers = _firmado("/api/v1/voice/twilio/status", params)
        respuesta = await api_client.post(
            "/api/v1/voice/twilio/status", content=cuerpo, headers=headers
        )

        assert respuesta.status_code == 204
        datos = registros[0]["datos"]
        assert datos["status"] == "completed"
        assert datos["duration_seconds"] == 42
        assert datos["ended_at"]

    async def test_status_intermedio_no_marca_fin(
        self, ajustes: Any, registros: list[dict[str, Any]], api_client: Any
    ) -> None:
        params = {**LLAMADA, "CallStatus": "in-progress"}
        cuerpo, headers = _firmado("/api/v1/voice/twilio/status", params)
        await api_client.post("/api/v1/voice/twilio/status", content=cuerpo, headers=headers)
        assert "ended_at" not in registros[0]["datos"]

    async def test_status_sin_direction_no_toca_la_direccion_guardada(
        self, ajustes: Any, registros: list[dict[str, Any]], api_client: Any
    ) -> None:
        """Un status callback sin `Direction` no puede voltear un `outbound`.

        `normalizar_direccion()` convierte lo ausente en `inbound`, asi que
        mandar siempre su resultado hacia el upsert pisaba la direccion correcta
        de una llamada saliente con cada evento que llegara sin el campo.
        """
        params = {k: v for k, v in LLAMADA.items() if k != "Direction"}
        params["CallStatus"] = "completed"
        cuerpo, headers = _firmado("/api/v1/voice/twilio/status", params)
        await api_client.post("/api/v1/voice/twilio/status", content=cuerpo, headers=headers)

        assert registros[0]["datos"]["direction"] is None

    async def test_status_con_direction_desconocida_no_la_adivina(
        self, ajustes: Any, registros: list[dict[str, Any]], api_client: Any
    ) -> None:
        """`trunking-terminating` (troncales SIP) no es entrante."""
        params = {**LLAMADA, "CallStatus": "completed", "Direction": "trunking-terminating"}
        cuerpo, headers = _firmado("/api/v1/voice/twilio/status", params)
        await api_client.post("/api/v1/voice/twilio/status", content=cuerpo, headers=headers)

        assert registros[0]["datos"]["direction"] is None

    async def test_status_con_direction_saliente_si_la_guarda(
        self, ajustes: Any, registros: list[dict[str, Any]], api_client: Any
    ) -> None:
        params = {**LLAMADA, "CallStatus": "completed", "Direction": "outbound-api"}
        cuerpo, headers = _firmado("/api/v1/voice/twilio/status", params)
        await api_client.post("/api/v1/voice/twilio/status", content=cuerpo, headers=headers)

        assert registros[0]["datos"]["direction"] == "outbound"

    async def test_status_initiated_se_acepta(
        self, ajustes: Any, registros: list[dict[str, Any]], api_client: Any
    ) -> None:
        """`start_call()` se suscribe al evento `initiated`: su estado vale.

        No esta en la lista de `CallStatus` de la documentacion, pero es lo que
        trae ese callback. Sin el en `CALL_STATUSES` toda llamada saliente
        perdia su primer estado y dejaba un error en el log.
        """
        params = {**LLAMADA, "CallStatus": "initiated", "Direction": "outbound-api"}
        cuerpo, headers = _firmado("/api/v1/voice/twilio/status", params)
        respuesta = await api_client.post(
            "/api/v1/voice/twilio/status", content=cuerpo, headers=headers
        )

        assert respuesta.status_code == 204
        assert registros[0]["datos"]["status"] == "initiated"
        assert "initiated" in CALL_STATUSES
        assert "initiated" not in CALL_FINAL_STATUSES


class TestEstadosYMigracion:
    """El CHECK de la migracion y el del modelo tienen que decir lo mismo."""

    def test_la_migracion_admite_los_mismos_estados_que_el_modelo(self) -> None:
        fuente = (
            Path(__file__).resolve().parents[2]
            / "migrations"
            / "versions"
            / "016_call_clinical_records.py"
        ).read_text(encoding="utf-8")
        [check] = [
            c for c in CallRecord.__table__.constraints if c.name == "ck_call_records_status"
        ]
        # La migracion parte el CHECK en dos literales, asi que se comparan sin
        # espacios ni comillas dobles: lo que queda es el mismo `IN (...)`.
        esperado = str(check.sqltext)  # type: ignore[attr-defined]
        normal = "".join(fuente.split()).replace('"', "")
        assert "".join(esperado.split()) in normal

    def test_ningun_estado_final_es_intermedio(self) -> None:
        assert set(CALL_STATUSES) >= CALL_FINAL_STATUSES
        assert "initiated" not in CALL_FINAL_STATUSES


class TestDireccionReconocida:
    @pytest.mark.parametrize(
        ("crudo", "esperado"),
        [
            ("inbound", "inbound"),
            ("outbound", "outbound"),
            ("outbound-api", "outbound"),
            ("outbound-dial", "outbound"),
            (None, None),
            ("", None),
            ("trunking-terminating", None),
            ("lo-que-sea", None),
        ],
    )
    def test_solo_traduce_lo_que_entiende(self, crudo: str | None, esperado: str | None) -> None:
        assert direccion_reconocida(crudo) == esperado


# ─── Llamadas salientes ──────────────────────────────────────────────────────


class TestLlamadasSalientes:
    async def test_sin_jwt_es_401(self, ajustes: Any, api_client: Any) -> None:
        respuesta = await api_client.post("/api/v1/voice/calls", json={"to": CLIENTE})
        assert respuesta.status_code == 401

    async def test_un_agente_no_puede_llamar(
        self, ajustes: Any, authenticated_client_factory: Any
    ) -> None:
        cliente = authenticated_client_factory(role="agent", client_id=TENANT)
        respuesta = await cliente.post("/api/v1/voice/calls", json={"to": CLIENTE})
        assert respuesta.status_code == 403

    async def test_otro_tenant_no_usa_el_numero_de_la_plataforma(
        self, ajustes: Any, authenticated_client_factory: Any
    ) -> None:
        cliente = authenticated_client_factory(role="admin", client_id=uuid.uuid4())
        respuesta = await cliente.post("/api/v1/voice/calls", json={"to": CLIENTE})
        assert respuesta.status_code == 403

    async def test_numero_no_e164_es_422(
        self, ajustes: Any, authenticated_client_factory: Any
    ) -> None:
        cliente = authenticated_client_factory(role="admin", client_id=TENANT)
        respuesta = await cliente.post("/api/v1/voice/calls", json={"to": "3001234567"})
        assert respuesta.status_code == 422

    async def test_crea_la_llamada_con_las_urls_publicas(
        self, ajustes: Any, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services.messaging.voice_provider import TwilioVoiceProvider

        iniciar = AsyncMock(return_value="CA999")
        monkeypatch.setattr(TwilioVoiceProvider, "start_call", iniciar)
        cliente = authenticated_client_factory(role="admin", client_id=TENANT)

        respuesta = await cliente.post("/api/v1/voice/calls", json={"to": CLIENTE})

        assert respuesta.status_code == 202, respuesta.text
        assert respuesta.json() == {"call_sid": "CA999", "status": "queued"}
        kwargs = iniciar.await_args.kwargs
        assert kwargs["answer_url"] == f"{BASE}/api/v1/voice/twilio/incoming"
        assert kwargs["status_callback_url"] == f"{BASE}/api/v1/voice/twilio/status"

    async def test_twilio_rechaza_es_502(
        self, ajustes: Any, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services.messaging.voice_provider import TwilioAPIError, TwilioVoiceProvider

        monkeypatch.setattr(
            TwilioVoiceProvider, "start_call", AsyncMock(side_effect=TwilioAPIError("21211"))
        )
        cliente = authenticated_client_factory(role="admin", client_id=TENANT)
        respuesta = await cliente.post("/api/v1/voice/calls", json={"to": CLIENTE})
        assert respuesta.status_code == 502
        assert "21211" not in respuesta.text

    async def test_sin_credenciales_es_503(
        self, ajustes: Any, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ajustes, "TWILIO_ACCOUNT_SID", "")
        cliente = authenticated_client_factory(role="admin", client_id=TENANT)
        respuesta = await cliente.post("/api/v1/voice/calls", json={"to": CLIENTE})
        assert respuesta.status_code == 503


# ─── WebSocket de audio ──────────────────────────────────────────────────────


@pytest.fixture
def entorno_ws(ajustes: Any, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from app.api.v1 import voice_ws

    e = SimpleNamespace(registros=[], mensajes=[], streams=set())

    async def _mark_stream(canal: str, externo: str, **kw: Any) -> bool:
        if externo in e.streams:
            return False
        e.streams.add(externo)
        return True

    monkeypatch.setattr(voice_ws, "mark_if_new", _mark_stream)
    monkeypatch.setattr(voice_ws, "_llamadas_activas", 0)
    monkeypatch.setattr(cm, "_encolar_registro", lambda c, s, d: e.registros.append(d))
    monkeypatch.setattr(cm, "_encolar_mensaje", e.mensajes.append)
    monkeypatch.setattr(cm, "transcribir_con_whisper", AsyncMock(return_value="hola"))
    monkeypatch.setattr(cm, "synthesize_mulaw", AsyncMock(return_value=b"\xff" * 160))
    return e


@pytest.fixture
def cliente_ws(entorno_ws: SimpleNamespace) -> Iterator[TestClient]:
    from app.api.v1 import voice_ws

    app = FastAPI()
    app.include_router(voice_ws.router, prefix="/api/v1/voice")
    with TestClient(app) as c:
        yield c


def _start(token: str | None, call_sid: str = "CA1") -> dict[str, Any]:
    return {
        "event": "start",
        "streamSid": "MZ1",
        "start": {
            "streamSid": "MZ1",
            "callSid": call_sid,
            "customParameters": {"token": token} if token else {},
        },
    }


def _token(call_sid: str = "CA1") -> str:
    return emitir_token(
        client_id=str(TENANT),
        call_sid=call_sid,
        direction="inbound",
        phone_from=CLIENTE,
        phone_to=PLATAFORMA,
        contact_phone=CLIENTE,
    )


@contextlib.contextmanager
def _conectar(cliente: TestClient) -> Iterator[Any]:
    with cliente.websocket_connect("/api/v1/voice/stream") as ws:
        try:
            yield ws
        finally:
            with contextlib.suppress(Exception):
                ws.close(1000)
            cliente.portal.call(asyncio.sleep, 0.05)


def _codigo_de_cierre(ws: Any) -> int:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as info:
        ws.receive_json()
    return info.value.code


class TestWebSocket:
    def test_sin_token_se_cierra(self, cliente_ws: TestClient) -> None:
        with _conectar(cliente_ws) as ws:
            ws.send_json({"event": "connected"})
            ws.send_json(_start(None))
            assert _codigo_de_cierre(ws) == 4401

    def test_token_de_otra_llamada_se_cierra(self, cliente_ws: TestClient) -> None:
        with _conectar(cliente_ws) as ws:
            ws.send_json(_start(_token("CA-otra"), call_sid="CA1"))
            assert _codigo_de_cierre(ws) == 4401

    def test_un_evento_raro_antes_del_start_se_cierra(self, cliente_ws: TestClient) -> None:
        with _conectar(cliente_ws) as ws:
            ws.send_json({"event": "media", "media": {"payload": "AA=="}})
            assert _codigo_de_cierre(ws) == 4401

    def test_llamada_completa_encola_la_frase_y_guarda_el_registro(
        self, cliente_ws: TestClient, entorno_ws: SimpleNamespace
    ) -> None:
        with _conectar(cliente_ws) as ws:
            ws.send_json({"event": "connected"})
            ws.send_json(_start(_token()))
            for trama in _media(_tono(400, 3000) + _tono(240, 0)):
                ws.send_json({"event": "media", "media": {"track": "inbound", "payload": trama}})
            ws.send_json({"event": "stop"})
            cliente_ws.portal.call(asyncio.sleep, 0.2)

        assert [m["text"] for m in entorno_ws.mensajes] == ["hola"]
        assert entorno_ws.mensajes[0]["external_message_id"] == "CA1:1"
        [registro] = entorno_ws.registros
        assert [t["role"] for t in registro["transcript"]] == ["agent", "caller"]

    def test_un_segundo_stream_para_la_misma_llamada_se_rechaza(
        self, cliente_ws: TestClient, entorno_ws: SimpleNamespace
    ) -> None:
        entorno_ws.streams.add("CA1")
        with _conectar(cliente_ws) as ws:
            ws.send_json(_start(_token()))
            assert _codigo_de_cierre(ws) == 4409

    def test_tope_de_llamadas_simultaneas(
        self, cliente_ws: TestClient, ajustes: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ajustes, "VOICE_MAX_CONCURRENT_CALLS", 0)
        with _conectar(cliente_ws) as ws:
            ws.send_json(_start(_token()))
            assert _codigo_de_cierre(ws) == 4429

    def test_la_respuesta_del_agente_llega_como_audio(
        self, cliente_ws: TestClient, redis: FakeRedis
    ) -> None:
        with _conectar(cliente_ws) as ws:
            ws.send_json(_start(_token()))
            cliente_ws.portal.call(asyncio.sleep, 0.05)
            cliente_ws.portal.call(
                redis.publish,
                canal_de_salida(TENANT, CLIENTE),
                '{"type": "say", "message_id": "m1", "text": "Con gusto"}',
            )
            media = ws.receive_json()
            marca = ws.receive_json()
            assert media["event"] == "media"
            assert media["streamSid"] == "MZ1"
            assert marca == {"event": "mark", "streamSid": "MZ1", "mark": {"name": "m1"}}
            ws.send_json({"event": "stop"})


class TestEscuchaResiliente:
    """Una caida de Redis no puede dejar al agente mudo el resto de la llamada."""

    async def test_se_resuscribe_y_sigue_hablando(
        self, ajustes: Any, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`listen()` lanza `ConnectionError` en un failover; hay que reanudar.

        Antes la tarea moria en silencio —su excepcion solo la recogia el
        `gather(return_exceptions=True)` de `close()`— y la llamada seguia
        abierta pero sorda: el worker publicaba a un canal sin suscriptor.
        """
        monkeypatch.setattr(cm, "ESPERA_REINTENTO_PUBSUB", 0)
        arnes = _Sesion()
        await arnes.sesion.start()

        # La primera escucha se corta como si Redis se hubiera caido.
        primero = arnes.sesion._pubsub
        await primero.cola.put({"type": "caida"})
        original = primero.listen

        async def _listen_que_falla() -> Any:
            async for mensaje in original():
                if mensaje.get("type") == "caida":
                    raise ConnectionError("Connection closed by server")
                yield mensaje

        primero.listen = _listen_que_falla
        await _esperar(lambda: arnes.sesion._pubsub is not primero)

        # Ya resuscrita: lo que se publique ahora si se dice.
        await redis.publish(
            canal_de_salida(TENANT, CLIENTE),
            json.dumps({"type": "say", "message_id": "m1", "text": "Sigo aqui"}),
        )
        await _esperar(lambda: arnes.eventos("mark"))
        await arnes.sesion.close("stop")

        assert arnes.sintetizar.await_args_list[0].args[0] == "Sigo aqui"

    async def test_se_rinde_tras_varios_intentos_y_lo_deja_en_el_log(
        self, ajustes: Any, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cm, "ESPERA_REINTENTO_PUBSUB", 0)
        monkeypatch.setattr(cm, "REINTENTOS_PUBSUB", 2)
        arnes = _Sesion()
        await arnes.sesion.start()

        intentos = 0

        async def _siempre_falla() -> Any:
            nonlocal intentos
            intentos += 1
            raise ConnectionError("Connection closed by server")
            yield  # pragma: no cover

        # El original, antes de parchear: si no, `_pubsub_roto` se llama a si
        # mismo y revienta con RecursionError dentro de `_resuscribir`.
        pubsub_real = redis.pubsub

        def _pubsub_roto() -> Any:
            falso = pubsub_real()
            falso.listen = _siempre_falla
            return falso

        arnes.sesion._pubsub.listen = _siempre_falla
        monkeypatch.setattr(redis, "pubsub", _pubsub_roto)
        await _esperar(lambda: arnes.sesion._escucha is not None and arnes.sesion._escucha.done())

        assert intentos == cm.REINTENTOS_PUBSUB + 1
        # Se rinde, pero no se lleva la llamada por delante.
        assert arnes.sesion._escucha is not None
        assert arnes.sesion._escucha.exception() is None
        await arnes.sesion.close("stop")


class TestMarcoStartMalformado:
    """El `start` llega sin autenticar: sus tipos no se pueden dar por buenos."""

    @pytest.mark.parametrize(
        "marco",
        [
            pytest.param({"event": "start", "start": "abc"}, id="start-texto"),
            pytest.param(
                {"event": "start", "start": {"callSid": "CA1", "customParameters": ["a"]}},
                id="parametros-lista",
            ),
            pytest.param(
                {
                    "event": "start",
                    "start": {
                        "streamSid": "MZ1",
                        "callSid": "CA1",
                        "customParameters": {"token": 5},
                    },
                },
                id="token-numero",
            ),
            pytest.param(
                {
                    "event": "start",
                    "start": {"streamSid": "MZ1", "callSid": {"a": 1}, "customParameters": {}},
                },
                id="callsid-dict",
            ),
        ],
    )
    def test_se_cierra_con_4401_y_no_revienta(
        self, cliente_ws: TestClient, marco: dict[str, Any]
    ) -> None:
        with _conectar(cliente_ws) as ws:
            ws.send_json(marco)
            assert _codigo_de_cierre(ws) == 4401

    def test_verificar_token_rechaza_lo_que_no_es_texto(self) -> None:
        assert verificar_token(5, "CA1") is None  # type: ignore[arg-type]
        assert verificar_token(["a.b"], "CA1") is None  # type: ignore[arg-type]


class TestRespuestaHablada:
    def test_texto_para_voz_quita_markdown_y_urls(self) -> None:
        texto = "**Horario:** lunes a viernes. Mira [la web](https://x.co/a) o https://y.co\n# Nota"
        assert cm.texto_para_voz(texto) == "Horario: lunes a viernes. Mira la web o Nota"

    @pytest.mark.parametrize(
        "texto",
        [
            pytest.param("https://pagos.example/factura/123", id="solo-url"),
            pytest.param("**", id="solo-markdown"),
            pytest.param("[https://x.co/a](https://x.co/a)", id="enlace-que-es-la-url"),
        ],
    )
    async def test_una_respuesta_que_se_queda_sin_texto_no_deja_la_llamada_muda(
        self, ajustes: Any, redis: FakeRedis, texto: str
    ) -> None:
        """`texto_para_voz()` quita URLs y markdown; puede no quedar nada.

        Sin el aviso generico el bucle de `_decir()` no se ejecutaba: ni audio,
        ni marca, y el llamante oia silencio mientras la conversacion daba la
        respuesta por entregada.
        """
        arnes = _Sesion()
        await arnes.sesion.start()
        await redis.publish(
            canal_de_salida(TENANT, CLIENTE),
            json.dumps({"type": "say", "message_id": "m1", "text": texto}),
        )
        await _esperar(lambda: arnes.eventos("mark"))
        await arnes.sesion.close("stop")

        assert arnes.eventos("media"), "la llamada se quedo en silencio"
        arnes.sintetizar.assert_awaited()
        dicho = arnes.sintetizar.await_args_list[0].args[0]
        assert dicho in cm.RESPUESTA_SIN_VOZ
        # La transcripcion guarda lo que el agente dijo de verdad, no el aviso.
        assert arnes.sesion.transcript[-1]["text"] == texto

    async def test_una_respuesta_con_texto_util_no_usa_el_aviso(
        self, ajustes: Any, redis: FakeRedis
    ) -> None:
        arnes = _Sesion()
        await arnes.sesion.start()
        await redis.publish(
            canal_de_salida(TENANT, CLIENTE),
            json.dumps({"type": "say", "message_id": "m1", "text": "Paga en https://x.co/a hoy"}),
        )
        await _esperar(lambda: arnes.eventos("mark"))
        await arnes.sesion.close("stop")

        dicho = arnes.sintetizar.await_args_list[0].args[0]
        assert dicho == "Paga en hoy"

    async def test_una_respuesta_larga_se_sintetiza_y_se_manda_por_oraciones(
        self, ajustes: Any, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La primera oracion suena mientras se sintetiza la siguiente."""
        monkeypatch.setattr(cm, "TTS_CHUNK_CHARS", 20)
        arnes = _Sesion(audio_tts=b"\xff" * 160)
        orden: list[str] = []

        async def _sintetizar(texto: str, client_id: str) -> bytes:
            orden.append(f"tts:{texto}")
            return b"\xff" * 160

        async def _enviar(evento: dict[str, Any]) -> None:
            orden.append(evento["event"])
            arnes.enviados.append(evento)

        arnes.sesion._synthesize = _sintetizar
        arnes.sesion._send = _enviar
        await arnes.sesion.start()
        await redis.publish(
            canal_de_salida(TENANT, CLIENTE),
            '{"type": "say", "message_id": "m1", "text": "Primera oracion. Segunda oracion."}',
        )
        await _esperar(lambda: arnes.eventos("mark"))
        await arnes.sesion.close("stop")

        assert orden == [
            "tts:Primera oracion.",
            "media",
            "tts:Segunda oracion.",
            "media",
            "mark",
        ]
