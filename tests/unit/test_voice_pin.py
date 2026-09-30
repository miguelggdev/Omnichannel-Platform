"""PIN por DTMF que autentica una llamada (ADR-073)."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents.nodes import clinical as nodo_clinico
from app.agents.nodes import marketing as nodo_marketing
from app.agents.tools import clinical_tools as ct
from app.agents.tools import marketing_tools as mt
from app.services import channel_identity
from app.services.channel_identity import contacto_autenticado
from app.services.voice import call_manager as cm
from app.services.voice import pin_auth
from app.services.voice.pin_auth import PinInvalidoError, call_sid_de_mensaje, validar_formato_pin
from tests.unit.agent_doubles import AgentSettings, estado, parchear_agent_settings
from tests.unit.test_voice_calls import CLIENTE, TENANT, _claims, ajustes  # noqa: F401

PROFESIONAL = uuid.uuid4()
CALL_SID = "CA1"


class TestFormatoDelPin:
    @pytest.mark.parametrize("pin", ["482913", "90417265", "731905"])
    def test_acepta_de_seis_a_ocho_digitos(self, pin: str) -> None:
        validar_formato_pin(pin)

    @pytest.mark.parametrize(
        "pin",
        ["", "12345", "123456789", "48291a", "48 291", "111111", "000000", "123456", "654321"],
    )
    def test_rechaza_lo_corto_lo_largo_y_lo_predecible(self, pin: str) -> None:
        with pytest.raises(PinInvalidoError):
            validar_formato_pin(pin)

    def test_el_pin_se_mezcla_con_el_tenant_y_el_contacto(self, monkeypatch: Any) -> None:
        a = pin_auth.preparar_pin("482913", TENANT, PROFESIONAL)

        assert a == pin_auth.preparar_pin("482913", str(TENANT).upper(), PROFESIONAL)
        assert a != pin_auth.preparar_pin("482913", TENANT, uuid.uuid4())
        assert a != pin_auth.preparar_pin("482913", uuid.uuid4(), PROFESIONAL)
        assert "482913" not in a
        assert len(a) == 64  # cabe en los 72 bytes de bcrypt


def test_el_callsid_sale_del_id_del_mensaje() -> None:
    assert call_sid_de_mensaje("CA1:3") == CALL_SID
    assert call_sid_de_mensaje("wamid.abc") is None
    assert call_sid_de_mensaje(None) is None
    assert call_sid_de_mensaje(":3") is None


class TestContactoAutenticado:
    async def _pedir(self, canal: str | None, **extra: Any) -> str | None:
        return await contacto_autenticado(
            channel=canal, client_id=TENANT, contact_id=str(PROFESIONAL), **extra
        )

    @pytest.mark.parametrize("canal", ["whatsapp", "telegram", "instagram", "facebook"])
    async def test_un_canal_verificado_devuelve_el_contacto_de_la_conversacion(
        self, canal: str
    ) -> None:
        assert await self._pedir(canal) == str(PROFESIONAL)

    async def test_sin_contacto_no_hay_identidad(self) -> None:
        assert (
            await contacto_autenticado(channel="whatsapp", client_id=TENANT, contact_id=None)
            is None
        )

    @pytest.mark.parametrize("canal", ["email", "webchat", None])
    async def test_los_canales_sin_identidad_no_autentican(self, canal: str | None) -> None:
        assert await self._pedir(canal, external_message_id="CA1:1") is None

    async def test_una_llamada_sin_pin_no_autentica(
        self, llamada_sin_autenticar: AsyncMock
    ) -> None:
        assert await self._pedir("voice", external_message_id="CA1:1") is None
        llamada_sin_autenticar.assert_awaited_once_with(TENANT, CALL_SID)

    async def test_una_llamada_con_pin_autentica_como_el_contacto_del_pin(
        self, llamada_sin_autenticar: AsyncMock
    ) -> None:
        otro = uuid.uuid4()  # el del PIN no es el del caller ID: son contactos distintos
        llamada_sin_autenticar.return_value = otro

        assert await self._pedir("voice", external_message_id="CA1:1") == str(otro)

    async def test_una_llamada_sin_callsid_no_autentica(
        self, llamada_sin_autenticar: AsyncMock
    ) -> None:
        llamada_sin_autenticar.return_value = None

        assert await self._pedir("voice", external_message_id="sin-callsid") is None


class _Redis:
    """Redis minimo: `set`/`get` con caducidad registrada."""

    def __init__(self) -> None:
        self.datos: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.falla = False

    async def set(self, clave: str, valor: str, ex: int | None = None) -> None:
        self.datos[clave] = valor
        self.ttl[clave] = ex or 0

    async def get(self, clave: str) -> str | None:
        if self.falla:
            raise ConnectionError("redis caido")
        return self.datos.get(clave)


class TestLlamadaAutenticadaEnRedis:
    @pytest.fixture
    def redis(self, monkeypatch: pytest.MonkeyPatch) -> _Redis:
        falso = _Redis()
        monkeypatch.setattr(pin_auth, "get_redis", lambda: falso)
        return falso

    async def test_se_guarda_por_tenant_y_llamada_con_la_duracion_maxima(
        self, redis: _Redis
    ) -> None:
        await pin_auth.marcar_llamada_autenticada(TENANT, CALL_SID, PROFESIONAL)

        [clave] = redis.datos
        assert str(TENANT) in clave
        assert CALL_SID in clave
        assert redis.datos[clave] == str(PROFESIONAL)
        assert redis.ttl[clave] > pin_auth.get_settings().VOICE_MAX_CALL_SECONDS

    async def test_otro_tenant_o_otra_llamada_no_la_ven(self, redis: _Redis) -> None:
        await pin_auth.marcar_llamada_autenticada(TENANT, CALL_SID, PROFESIONAL)

        assert await pin_auth.profesional_de_la_llamada(TENANT, CALL_SID) == PROFESIONAL
        assert await pin_auth.profesional_de_la_llamada(uuid.uuid4(), CALL_SID) is None
        assert await pin_auth.profesional_de_la_llamada(TENANT, "CA2") is None
        assert await pin_auth.profesional_de_la_llamada(TENANT, None) is None

    async def test_sin_redis_falla_cerrado(self, redis: _Redis) -> None:
        await pin_auth.marcar_llamada_autenticada(TENANT, CALL_SID, PROFESIONAL)
        redis.falla = True

        assert await pin_auth.profesional_de_la_llamada(TENANT, CALL_SID) is None


class _Sesion:
    """`CallSession` con dobles para el PIN; registra lo que se dice y se marca."""

    def __init__(self, verificar: AsyncMock | None = None) -> None:
        self.verificar = verificar or AsyncMock(return_value=None)
        self.marcar = AsyncMock()
        self.enviados: list[dict[str, Any]] = []

        async def _enviar(evento: dict[str, Any]) -> None:
            self.enviados.append(evento)

        self.sesion = cm.CallSession(
            _claims(),
            "MZ1",
            _enviar,
            synthesize=AsyncMock(return_value=b"\xff" * 160),
            transcriber=AsyncMock(return_value=None),
            enqueue_message=lambda _m: None,
            enqueue_record=lambda *_a: None,
            verify_pin=self.verificar,
            mark_authenticated=self.marcar,
        )

    async def teclear(self, teclas: str) -> None:
        for tecla in teclas:
            await self.sesion.on_dtmf(tecla)

    async def avisos(self) -> list[str]:
        await asyncio.gather(*self.sesion._avisos, return_exceptions=True)
        # Sin `start()` la transcripcion no lleva el saludo: solo estan los avisos.
        return [t["text"] for t in self.sesion.transcript if t["role"] == "agent"]


@pytest.mark.usefixtures("ajustes")
class TestCallSessionDtmf:
    async def test_un_pin_correcto_autentica_la_llamada(self) -> None:
        arnes = _Sesion(AsyncMock(return_value=PROFESIONAL))

        await arnes.teclear("482913#")

        arnes.verificar.assert_awaited_once_with(TENANT, CLIENTE, "482913")
        arnes.marcar.assert_awaited_once_with(TENANT, CALL_SID, PROFESIONAL)
        assert arnes.sesion.autenticada_como == PROFESIONAL
        assert await arnes.avisos() == [cm.PIN_ACEPTADO]

    async def test_un_pin_incorrecto_avisa_y_no_autentica(self) -> None:
        arnes = _Sesion()

        await arnes.teclear("482913#")

        arnes.marcar.assert_not_awaited()
        assert arnes.sesion.autenticada_como is None
        assert await arnes.avisos() == [cm.PIN_RECHAZADO]

    async def test_sin_almohadilla_no_se_verifica(self) -> None:
        arnes = _Sesion()

        await arnes.teclear("482913")

        arnes.verificar.assert_not_awaited()

    async def test_asterisco_borra_lo_tecleado(self) -> None:
        arnes = _Sesion()

        await arnes.teclear("111*482913#")

        arnes.verificar.assert_awaited_once_with(TENANT, CLIENTE, "482913")

    async def test_almohadilla_sola_no_cuenta_como_intento(self) -> None:
        arnes = _Sesion()

        await arnes.teclear("###")

        arnes.verificar.assert_not_awaited()

    async def test_demasiadas_teclas_para_un_pin_se_descartan(self) -> None:
        arnes = _Sesion()

        await arnes.teclear("1234567890#")

        arnes.verificar.assert_not_awaited()

    async def test_tres_intentos_por_llamada(self) -> None:
        arnes = _Sesion()

        await arnes.teclear("482913#" * 5)

        assert arnes.verificar.await_count == cm.MAX_INTENTOS_PIN_POR_LLAMADA
        avisos = await arnes.avisos()
        assert avisos.count(cm.PIN_RECHAZADO) == 3
        assert avisos.count(cm.PIN_SIN_INTENTOS) == 2

    async def test_ya_autenticada_ignora_mas_teclas(self) -> None:
        arnes = _Sesion(AsyncMock(return_value=PROFESIONAL))

        await arnes.teclear("482913#")
        await arnes.teclear("999999#")

        assert arnes.verificar.await_count == 1

    async def test_un_fallo_al_verificar_no_gasta_el_intento(self) -> None:
        arnes = _Sesion(AsyncMock(side_effect=RuntimeError("db caida")))

        await arnes.teclear("482913#" * 4)

        assert arnes.verificar.await_count == 4, "el error no cuenta contra los tres intentos"
        assert arnes.sesion.autenticada_como is None
        assert set(await arnes.avisos()) == {cm.PIN_NO_DISPONIBLE}

    async def test_el_pin_no_queda_en_la_transcripcion_ni_en_el_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        arnes = _Sesion()

        with caplog.at_level("DEBUG"):
            await arnes.teclear("482913#")
            await arnes.avisos()

        assert "482913" not in str(arnes.sesion.transcript)
        assert "482913" not in caplog.text


# ─── Agentes: en voz solo vale la identidad de la llamada ────────────────────


def _nodo_clinico(monkeypatch: pytest.MonkeyPatch, *, es_profesional: bool) -> dict[str, Any]:
    llamadas: dict[str, Any] = {}
    parchear_agent_settings(
        monkeypatch, nodo_clinico, AgentSettings(model="gpt-4o", enabled_agents=("rag", "clinical"))
    )

    @asynccontextmanager
    async def _cm(client_id: Any) -> Any:
        yield None

    monkeypatch.setattr(nodo_clinico, "tenant_session", _cm)

    async def _es(session: Any, client_id: Any, contact_id: Any) -> bool:
        llamadas["contacto"] = contact_id
        return es_profesional

    monkeypatch.setattr(nodo_clinico, "es_profesional_clinico", _es)

    async def _responder(**kwargs: Any) -> str:
        llamadas["responder"] = kwargs
        return "ok"

    monkeypatch.setattr(nodo_clinico, "responder_con_tools", _responder)
    return llamadas


class TestAgentesPorVoz:
    async def test_el_nodo_clinico_no_atiende_una_llamada_sin_pin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llamadas = _nodo_clinico(monkeypatch, es_profesional=True)

        resultado = await nodo_clinico.clinical_agent_node(
            estado(
                client_id=str(TENANT),
                channel="voice",
                contact_id=str(uuid.uuid4()),
                message={"text": "dicto", "external_message_id": "CA1:1"},
            )
        )

        assert resultado["response_text"] == nodo_clinico.MENSAJE_NO_AUTORIZADO
        assert "contacto" not in llamadas

    async def test_el_nodo_clinico_atiende_una_llamada_autenticada_como_profesional(
        self, monkeypatch: pytest.MonkeyPatch, llamada_sin_autenticar: AsyncMock
    ) -> None:
        llamada_sin_autenticar.return_value = PROFESIONAL
        llamadas = _nodo_clinico(monkeypatch, es_profesional=True)

        await nodo_clinico.clinical_agent_node(
            estado(
                client_id=str(TENANT),
                channel="voice",
                contact_id=str(uuid.uuid4()),  # el contacto de voz, distinto del profesional
                message={"text": "dicto", "external_message_id": "CA1:1"},
            )
        )

        assert llamadas["contacto"] == str(PROFESIONAL), (
            "se autoriza al del PIN, no al del caller ID"
        )
        assert "responder" in llamadas

    async def test_un_pin_valido_no_basta_si_ese_contacto_no_es_profesional(
        self, monkeypatch: pytest.MonkeyPatch, llamada_sin_autenticar: AsyncMock
    ) -> None:
        llamada_sin_autenticar.return_value = PROFESIONAL
        llamadas = _nodo_clinico(monkeypatch, es_profesional=False)

        resultado = await nodo_clinico.clinical_agent_node(
            estado(
                client_id=str(TENANT),
                channel="voice",
                message={"text": "dicto", "external_message_id": "CA1:1"},
            )
        )

        assert resultado["response_text"] == nodo_clinico.MENSAJE_NO_AUTORIZADO
        assert "responder" not in llamadas

    async def test_el_nodo_de_marketing_tambien_exige_la_llamada_autenticada(
        self, monkeypatch: pytest.MonkeyPatch, llamada_sin_autenticar: AsyncMock
    ) -> None:
        monkeypatch.setattr(
            nodo_marketing,
            "get_agent_settings",
            AsyncMock(return_value=SimpleNamespace(enabled_agents=("marketing",), model="gpt-4o")),
        )

        @asynccontextmanager
        async def _cm(client_id: Any) -> Any:
            yield None

        monkeypatch.setattr(nodo_marketing, "tenant_session", _cm)
        es_operador = AsyncMock(return_value=True)
        monkeypatch.setattr(nodo_marketing, "es_operador_de_marketing", es_operador)
        con_tools = AsyncMock(return_value="hecho")
        monkeypatch.setattr(nodo_marketing, "responder_con_tools", con_tools)
        entrada = {
            "client_id": str(TENANT),
            "channel": "voice",
            "contact_id": str(uuid.uuid4()),
            "message": {"text": "manda una promo", "external_message_id": "CA1:1"},
        }

        sin_pin = await nodo_marketing.marketing_node(dict(entrada))
        con_tools.assert_not_awaited()
        llamada_sin_autenticar.return_value = PROFESIONAL
        con_pin = await nodo_marketing.marketing_node(dict(entrada))

        assert sin_pin["response_text"] == nodo_marketing.MENSAJE_NO_AUTORIZADO
        assert con_pin["response_text"] == "hecho"
        es_operador.assert_awaited_once()
        assert es_operador.await_args.args[2] == str(PROFESIONAL)

    async def test_las_tools_clinicas_usan_al_profesional_del_pin(
        self, monkeypatch: pytest.MonkeyPatch, llamada_sin_autenticar: AsyncMock
    ) -> None:
        llamada_sin_autenticar.return_value = PROFESIONAL

        @asynccontextmanager
        async def _cm(client_id: Any, user_id: Any = None) -> Any:
            yield None

        monkeypatch.setattr(ct, "tenant_session", _cm)
        es_profesional = AsyncMock(return_value=True)
        monkeypatch.setattr(ct, "es_profesional_clinico", es_profesional)
        config = {
            "configurable": {
                "client_id": str(TENANT),
                "channel": "voice",
                "contact_id": str(uuid.uuid4()),
                "external_message_id": "CA1:1",
            }
        }

        resultado = await ct._profesional(config)

        assert resultado == PROFESIONAL
        assert es_profesional.await_args.args[2] == str(PROFESIONAL)

    async def test_las_tools_clinicas_rechazan_una_llamada_sin_pin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ct, "es_profesional_clinico", AsyncMock(return_value=True))
        config = {
            "configurable": {
                "client_id": str(TENANT),
                "channel": "voice",
                "contact_id": str(uuid.uuid4()),
                "external_message_id": "CA1:1",
            }
        }

        resultado = await ct._profesional(config)

        assert isinstance(resultado, dict)
        assert resultado["error"] == ct.NO_AUTORIZADO

    async def test_las_tools_de_marketing_rechazan_una_llamada_sin_pin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mt, "es_operador_de_marketing", AsyncMock(return_value=True))
        config = {
            "configurable": {
                "client_id": str(TENANT),
                "channel": "voice",
                "contact_id": str(uuid.uuid4()),
                "external_message_id": "CA1:1",
            }
        }

        assert await mt._no_autorizado(config) == mt.NO_AUTORIZADO


def test_el_canal_de_voz_sigue_sin_identidad_verificada_por_si_solo() -> None:
    assert not channel_identity.identidad_verificada("voice")
