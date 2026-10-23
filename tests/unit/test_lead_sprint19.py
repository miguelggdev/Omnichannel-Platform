"""Sprint 19, Dev B: alertas de deals, recordatorios, voz IA, calendario y analitica (sin base)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import get_settings
from app.schemas.lead_automation import CallRemindersConfig, DealAlertsConfig, LeadAutomationConfig
from app.services import call_calendar, scheduled_call_voice, voice_ai_twilio
from app.services.call_notifications import formatear_cuando, texto_recordatorio
from app.services.call_scheduling import Intervalo
from app.services.deal_alerts import armar_texto, formatear_importe, supera_umbral
from app.services.lead_analytics import (
    MAX_DIAS,
    RangoInvalidoError,
    limites,
    rango_por_defecto,
    tasa,
    validar_rango,
)
from app.services.messaging.voice_provider import TwilioAPIError
from app.services.voice_call_provider import (
    OutboundCallRequest,
    ProviderCallRef,
    VoiceProviderError,
    VoiceProviderNotConfiguredError,
    VoiceProviderTemporaryError,
)

# ── Configuracion de alertas y recordatorios ────────────────────────────────────────────────


class TestDealAlertsConfig:
    def _ok(self, **extra: Any) -> dict[str, Any]:
        return {
            "enabled": True,
            "telegram_chat_id": "-1001234567890",
            "thresholds": {"USD": "5000"},
            **extra,
        }

    def test_valida(self) -> None:
        config = DealAlertsConfig.model_validate(self._ok())
        assert config.thresholds == {"USD": Decimal(5000)}

    @pytest.mark.parametrize("chat", ["123456", "-1001234567890", "@mi_canal", "@Canal_Ventas1"])
    def test_chats_validos(self, chat: str) -> None:
        assert (
            DealAlertsConfig.model_validate(self._ok(telegram_chat_id=chat)).telegram_chat_id
            == chat
        )

    @pytest.mark.parametrize("chat", ["mi canal", "@ab", "http://t.me/x", "12 34", "-", "@1canal"])
    def test_chats_invalidos(self, chat: str) -> None:
        with pytest.raises(ValidationError):
            DealAlertsConfig.model_validate(self._ok(telegram_chat_id=chat))

    @pytest.mark.parametrize("umbrales", [{"usd": "5"}, {"US": "5"}, {"USD": "0"}, {"USD": "-1"}])
    def test_umbrales_invalidos(self, umbrales: dict[str, str]) -> None:
        with pytest.raises(ValidationError):
            DealAlertsConfig.model_validate(self._ok(thresholds=umbrales))

    def test_activarla_exige_chat_y_umbral(self) -> None:
        with pytest.raises(ValidationError):
            DealAlertsConfig.model_validate({"enabled": True, "thresholds": {"USD": "5"}})
        with pytest.raises(ValidationError):
            DealAlertsConfig.model_validate({"enabled": True, "telegram_chat_id": "123"})

    def test_apagada_por_defecto_y_sin_nada(self) -> None:
        assert DealAlertsConfig().enabled is False

    def test_no_admite_claves_desconocidas(self) -> None:
        with pytest.raises(ValidationError):
            DealAlertsConfig.model_validate({"enabled": False, "token": "x"})

    def test_demasiadas_monedas(self) -> None:
        umbrales = {f"A{chr(65 + i)}{chr(65 + j)}": "1" for i in range(3) for j in range(4)}
        with pytest.raises(ValidationError):
            DealAlertsConfig.model_validate({"thresholds": umbrales})

    def test_forma_parte_de_la_configuracion_del_tenant(self) -> None:
        config = LeadAutomationConfig.model_validate(
            {"deal_alerts": self._ok(), "call_reminders": {"whatsapp_template": "recordatorio_v1"}}
        )
        assert config.deal_alerts.enabled
        assert config.call_reminders.whatsapp_template == "recordatorio_v1"

    def test_recordatorios_rechazan_plantilla_vacia(self) -> None:
        with pytest.raises(ValidationError):
            CallRemindersConfig.model_validate({"whatsapp_template": ""})


class TestUmbral:
    CONFIG = DealAlertsConfig(
        enabled=True,
        telegram_chat_id="123",
        thresholds={"USD": Decimal(5000), "COP": Decimal(20000000)},
    )

    @pytest.mark.parametrize(
        ("valor", "moneda", "esperado"),
        [
            ("5000", "USD", True),  # el umbral es inclusivo
            ("4999.99", "USD", False),
            ("9000", "USD", True),
            ("5000", "COP", False),  # 5.000 COP no son 5.000 USD
            ("20000000", "COP", True),
            ("100000", "EUR", False),  # una moneda sin umbral no alerta
        ],
    )
    def test_por_moneda(self, valor: str, moneda: str, esperado: bool) -> None:
        assert supera_umbral(self.CONFIG, Decimal(valor), moneda) is esperado

    def test_apagada_o_sin_chat_no_alerta(self) -> None:
        apagada = DealAlertsConfig(telegram_chat_id="1", thresholds={"USD": Decimal(1)})
        assert supera_umbral(apagada, Decimal(10**6), "USD") is False
        sin_chat = DealAlertsConfig.model_construct(
            enabled=True, telegram_chat_id=None, thresholds={"USD": Decimal(1)}
        )
        assert supera_umbral(sin_chat, Decimal(10**6), "USD") is False

    def test_formato_del_importe(self) -> None:
        assert formatear_importe(Decimal("12500"), "USD") == "12,500.00 USD"
        assert formatear_importe(Decimal("0.5"), "COP") == "0.50 COP"

    def test_el_titulo_se_limpia_y_se_recorta(self) -> None:
        texto = armar_texto("Venta\nBcc: x‮" + "a" * 500, Decimal(10), "USD", "Marta")
        primera, *resto = texto.split("\n")
        assert "‮" not in texto
        assert len(primera) <= len("Deal ganado: ") + 120
        assert resto[0] == "Importe: 10.00 USD"
        assert resto[1] == "Responsable: Marta"

    def test_sin_responsable_ni_titulo(self) -> None:
        assert (
            armar_texto("", Decimal(1), "USD", None)
            == "Deal ganado: (sin titulo)\nImporte: 1.00 USD"
        )


# ── Recordatorios ───────────────────────────────────────────────────────────────────────────


class TestRecordatorios:
    INICIO = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)

    def test_la_hora_va_en_la_zona_de_la_llamada(self) -> None:
        assert (
            formatear_cuando(self.INICIO, "America/Bogota") == "20/10/2026 10:00 (America/Bogota)"
        )
        assert formatear_cuando(self.INICIO, "Europe/Madrid") == "20/10/2026 17:00 (Europe/Madrid)"

    def test_zona_desconocida_cae_a_utc(self) -> None:
        assert formatear_cuando(self.INICIO, "Marte/Olimpo") == "20/10/2026 15:00 (UTC)"

    @pytest.mark.parametrize("idioma", ["es", "en", "pt"])
    @pytest.mark.parametrize("cual", ["24h", "1h"])
    def test_textos_en_cada_idioma(self, idioma: str, cual: str) -> None:
        asunto, cuerpo = texto_recordatorio(
            cual,
            idioma=idioma,
            nombre="Ana",
            negocio="Acme",
            inicio=self.INICIO,
            zona="America/Bogota",
        )
        assert "Acme" in asunto
        assert "Ana" in cuerpo
        assert "Acme" in cuerpo
        assert "10:00" in cuerpo

    def test_sin_nombre_el_saludo_no_queda_roto(self) -> None:
        _, cuerpo = texto_recordatorio(
            "24h", idioma="es", nombre=None, negocio="Acme", inicio=self.INICIO, zona="UTC"
        )
        assert cuerpo.startswith("Hola, te recordamos")

    def test_idioma_desconocido_usa_espanol(self) -> None:
        _, cuerpo = texto_recordatorio(
            "1h", idioma="xx", nombre="Ana", negocio="Acme", inicio=self.INICIO, zona="UTC"
        )
        assert cuerpo.startswith("Hola Ana")

    def test_el_nombre_del_negocio_se_limpia(self) -> None:
        asunto, _ = texto_recordatorio(
            "1h",
            idioma="es",
            nombre="Ana",
            negocio="Acme\r\nBcc: x",
            inicio=self.INICIO,
            zona="UTC",
        )
        assert "\n" not in asunto
        assert "\r" not in asunto

    def test_sin_negocio_usa_un_texto_generico(self) -> None:
        asunto, _ = texto_recordatorio(
            "1h", idioma="es", nombre="Ana", negocio=None, inicio=self.INICIO, zona="UTC"
        )
        assert "nuestro equipo" in asunto


# ── Voz IA sobre Twilio ─────────────────────────────────────────────────────────────────────

CLIENT = uuid4()


def _peticion(client_id: Any = None) -> OutboundCallRequest:
    return OutboundCallRequest(
        scheduled_call_id=uuid4(), client_id=client_id or CLIENT, to_phone="+573001112233"
    )


@pytest.fixture
def twilio_configurado(monkeypatch: pytest.MonkeyPatch) -> None:
    ajustes = get_settings()
    monkeypatch.setattr(ajustes, "DEFAULT_CLIENT_ID", str(CLIENT))
    monkeypatch.setattr(ajustes, "VOICE_PUBLIC_BASE_URL", "https://voz.example.com/")
    monkeypatch.setattr(
        voice_ai_twilio,
        "get_channel_config",
        lambda canal: (
            "twilio",
            {
                "account_sid": "AC1",
                "auth_token": "tok",
                "phone_number": "+15550001111",
                "api_base_url": "https://api.twilio.test",
            },
        ),
    )


class TestTwilioIa:
    @pytest.mark.parametrize(
        ("mensaje", "temporal"),
        [
            ("Twilio no respondio: ConnectTimeout", True),
            ("Twilio rechazo la llamada (503, codigo None): x", True),
            ("Twilio rechazo la llamada (429, codigo 20429): x", True),
            ("Twilio rechazo la llamada (400, codigo 21211): numero invalido", False),
            ("Twilio rechazo la llamada (401, codigo 20003): x", False),
            ("algo raro", False),
        ],
    )
    def test_clasifica_los_fallos(self, mensaje: str, temporal: bool) -> None:
        assert voice_ai_twilio._es_temporal(TwilioAPIError(mensaje)) is temporal

    async def test_inicia_con_las_urls_publicas(
        self, twilio_configurado: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vistos: dict[str, Any] = {}

        async def start_call(
            self: Any, to: str, config: Any, answer_url: str, status_callback_url: str
        ) -> str:
            vistos.update(to=to, answer=answer_url, status=status_callback_url)
            return "CA123"

        monkeypatch.setattr(voice_ai_twilio.TwilioVoiceProvider, "start_call", start_call)

        ref = await voice_ai_twilio.TwilioAiVoiceProvider().start_call(_peticion())

        assert ref == ProviderCallRef(provider="twilio", external_id="CA123", status="queued")
        assert vistos["to"] == "+573001112233"
        assert vistos["answer"] == "https://voz.example.com/api/v1/voice/twilio/incoming"
        assert vistos["status"] == "https://voz.example.com/api/v1/voice/twilio/status"

    async def test_otro_tenant_no_puede_usar_el_numero(self, twilio_configurado: None) -> None:
        with pytest.raises(VoiceProviderNotConfiguredError):
            await voice_ai_twilio.TwilioAiVoiceProvider().start_call(_peticion(uuid4()))

    async def test_sin_url_publica_no_llama(
        self, twilio_configurado: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "VOICE_PUBLIC_BASE_URL", "")
        with pytest.raises(VoiceProviderNotConfiguredError):
            await voice_ai_twilio.TwilioAiVoiceProvider().start_call(_peticion())

    async def test_sin_credenciales_salientes_no_llama(
        self, twilio_configurado: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(voice_ai_twilio, "get_channel_config", lambda c: ("twilio", {}))
        with pytest.raises(VoiceProviderNotConfiguredError):
            await voice_ai_twilio.TwilioAiVoiceProvider().start_call(_peticion())

    @pytest.mark.parametrize(
        ("falla", "tipo"),
        [
            (TwilioAPIError("Twilio no respondio: ConnectError"), VoiceProviderTemporaryError),
            (
                TwilioAPIError("Twilio rechazo la llamada (500, codigo 1): x"),
                VoiceProviderTemporaryError,
            ),
            (
                TwilioAPIError("Twilio rechazo la llamada (400, codigo 21211): x"),
                VoiceProviderError,
            ),
        ],
    )
    async def test_traduce_los_errores_sin_el_telefono(
        self,
        twilio_configurado: None,
        monkeypatch: pytest.MonkeyPatch,
        falla: Exception,
        tipo: type[Exception],
    ) -> None:
        async def start_call(self: Any, *args: Any, **kwargs: Any) -> str:
            raise falla

        monkeypatch.setattr(voice_ai_twilio.TwilioVoiceProvider, "start_call", start_call)

        with pytest.raises(tipo) as info:
            await voice_ai_twilio.TwilioAiVoiceProvider().start_call(_peticion())

        assert "573001112233" not in str(info.value)

    def test_esta_registrado(self) -> None:
        from app.services.voice_call_provider import available_voice_providers

        assert "twilio" in available_voice_providers()

    def test_el_repr_de_la_peticion_oculta_el_telefono(self) -> None:
        assert "573001112233" not in repr(_peticion())


class TestColgar:
    @pytest.mark.parametrize(("estado", "esperado"), [(200, True), (404, False), (400, False)])
    async def test_resultado(
        self,
        twilio_configurado: None,
        monkeypatch: pytest.MonkeyPatch,
        estado: int,
        esperado: bool,
    ) -> None:
        transporte = httpx.MockTransport(lambda req: httpx.Response(estado, json={}))
        real = httpx.AsyncClient
        monkeypatch.setattr(
            voice_ai_twilio.httpx,
            "AsyncClient",
            lambda **kw: real(transport=transporte, **kw),
        )

        ref = ProviderCallRef(provider="twilio", external_id="CA123")
        assert await voice_ai_twilio.TwilioAiVoiceProvider().cancel_call(ref) is esperado

    async def test_un_error_del_servidor_se_propaga(
        self, twilio_configurado: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transporte = httpx.MockTransport(lambda req: httpx.Response(500))
        real = httpx.AsyncClient
        monkeypatch.setattr(
            voice_ai_twilio.httpx, "AsyncClient", lambda **kw: real(transport=transporte, **kw)
        )
        with pytest.raises(VoiceProviderError):
            await voice_ai_twilio.TwilioAiVoiceProvider().cancel_call(
                ProviderCallRef(provider="twilio", external_id="CA1")
            )


class TestVozAgendada:
    @pytest.mark.parametrize(
        ("telefono", "esperado"),
        [
            ("+57 300 111-2233", "+573001112233"),
            ("573001112233", "+573001112233"),
            ("(1) 555-000-1111", "+15550001111"),
            ("3001112233", "+3001112233"),  # sin prefijo no se puede saber: lo da por internacional
            ("0123456789", None),  # un codigo de pais nunca empieza en 0
            ("abc", None),
            ("", None),
            (None, None),
        ],
    )
    def test_a_e164(self, telefono: str | None, esperado: str | None) -> None:
        assert scheduled_call_voice.a_e164(telefono) == esperado

    def test_marca_vigente(self) -> None:
        ahora = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
        reciente = {
            "runtime": {"state": "starting", "at": (ahora - timedelta(seconds=30)).isoformat()}
        }
        vieja = {"runtime": {"state": "starting", "at": (ahora - timedelta(minutes=5)).isoformat()}}
        iniciada = {"runtime": {"state": "started", "external_id": "CA1", "at": ahora.isoformat()}}
        rota = {"runtime": {"state": "starting", "at": "no es una fecha"}}
        marca = scheduled_call_voice._marca_vigente
        assert marca(reciente, ahora) is True
        assert marca(vieja, ahora) is False
        assert marca(iniciada, ahora) is False
        assert marca(rota, ahora) is False
        assert marca({}, ahora) is False
        assert marca({"runtime": "x"}, ahora) is False


# ── Google Calendar ─────────────────────────────────────────────────────────────────────────


class _Evento:
    def __init__(self, event_id: str, inicio: datetime, minutos: int = 30) -> None:
        self.event_id = event_id
        self.start = inicio
        self.end = inicio + timedelta(minutes=minutos)


class _Servicio:
    def __init__(
        self, eventos: list[_Evento] | None = None, falla: Exception | None = None
    ) -> None:
        self.eventos = eventos or []
        self.falla = falla
        self.creados: list[dict[str, Any]] = []
        self.movidos: list[tuple[str, datetime, datetime]] = []
        self.borrados: list[str] = []

    async def list_events(
        self, desde: datetime, hasta: datetime, max_results: int = 50
    ) -> list[_Evento]:
        if self.falla:
            raise self.falla
        return self.eventos

    async def create_event(self, **kw: Any) -> _Evento:
        if self.falla:
            raise self.falla
        self.creados.append(kw)
        return _Evento("evt-1", kw["start"])

    async def modify_event(self, event_id: str, new_start: datetime, new_end: datetime) -> _Evento:
        if self.falla:
            raise self.falla
        self.movidos.append((event_id, new_start, new_end))
        return _Evento(event_id, new_start)

    async def cancel_event(self, event_id: str) -> bool:
        if self.falla:
            raise self.falla
        self.borrados.append(event_id)
        return True


def _con_servicio(monkeypatch: pytest.MonkeyPatch, servicio: _Servicio | None) -> None:
    async def fabrica(client_id: Any) -> _Servicio | None:
        return servicio

    monkeypatch.setattr(call_calendar, "_servicio", fabrica)


INI = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)


class TestCalendario:
    async def test_sin_calendario_no_consulta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _con_servicio(monkeypatch, None)

        r = await call_calendar.ocupados_del_calendario(uuid4(), INI, INI + timedelta(days=1))

        assert r.consultado is False
        assert r.intervalos == []

    async def test_devuelve_lo_ocupado(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _con_servicio(
            monkeypatch, _Servicio([_Evento("a", INI), _Evento("b", INI + timedelta(hours=2))])
        )

        r = await call_calendar.ocupados_del_calendario(uuid4(), INI, INI + timedelta(days=1))

        assert r.consultado is True
        assert r.intervalos == [
            Intervalo(INI, INI + timedelta(minutes=30)),
            Intervalo(INI + timedelta(hours=2), INI + timedelta(hours=2, minutes=30)),
        ]

    async def test_ignora_nuestros_eventos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Las llamadas agendadas ya estan en la base: contarlas dos veces bloquearia a los demas."""
        _con_servicio(
            monkeypatch,
            _Servicio([_Evento("nuestro", INI), _Evento("ajeno", INI + timedelta(hours=2))]),
        )

        r = await call_calendar.ocupados_del_calendario(
            uuid4(), INI, INI + timedelta(days=1), ignorar_ids={"nuestro"}
        )

        assert [i.inicio for i in r.intervalos] == [INI + timedelta(hours=2)]

    async def test_un_fallo_de_google_no_bloquea(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _con_servicio(monkeypatch, _Servicio(falla=RuntimeError("503")))

        r = await call_calendar.ocupados_del_calendario(uuid4(), INI, INI + timedelta(days=1))

        assert r.consultado is False

    async def test_google_que_no_contesta_se_corta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        class Lento(_Servicio):
            async def list_events(self, *a: Any, **k: Any) -> list[_Evento]:
                await asyncio.sleep(5)
                return []

        _con_servicio(monkeypatch, Lento())
        monkeypatch.setattr(call_calendar, "TIMEOUT_SEGUNDOS", 0.05)

        r = await call_calendar.ocupados_del_calendario(uuid4(), INI, INI + timedelta(days=1))

        assert r.consultado is False

    async def test_crear_evento_sin_telefono_ni_email(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        servicio = _Servicio()
        _con_servicio(monkeypatch, servicio)

        event_id = await call_calendar.crear_evento(
            uuid4(), nombre="Ana", empresa="Globex", inicio=INI, fin=INI + timedelta(minutes=30)
        )

        assert event_id == "evt-1"
        assert servicio.creados[0]["summary"] == "Llamada con Ana (Globex)"
        assert "@" not in str(servicio.creados[0])

    async def test_titulos_sin_nombre_ni_empresa(self, monkeypatch: pytest.MonkeyPatch) -> None:
        servicio = _Servicio()
        _con_servicio(monkeypatch, servicio)

        await call_calendar.crear_evento(uuid4(), nombre=None, empresa=" ", inicio=INI, fin=INI)

        assert servicio.creados[0]["summary"] == "Llamada con un lead"

    @pytest.mark.parametrize("servicio", [None, _Servicio(falla=RuntimeError("x"))])
    async def test_crear_mover_y_cancelar_no_lanzan(
        self, monkeypatch: pytest.MonkeyPatch, servicio: _Servicio | None
    ) -> None:
        _con_servicio(monkeypatch, servicio)

        assert (
            await call_calendar.crear_evento(uuid4(), nombre="A", empresa=None, inicio=INI, fin=INI)
            is None
        )
        assert await call_calendar.mover_evento(uuid4(), "e", inicio=INI, fin=INI) is False
        assert await call_calendar.cancelar_evento(uuid4(), "e") is False

    async def test_mover_y_cancelar_llaman_a_google(self, monkeypatch: pytest.MonkeyPatch) -> None:
        servicio = _Servicio()
        _con_servicio(monkeypatch, servicio)

        assert await call_calendar.mover_evento(
            uuid4(), "e1", inicio=INI, fin=INI + timedelta(hours=1)
        )
        assert await call_calendar.cancelar_evento(uuid4(), "e1")
        assert servicio.movidos == [("e1", INI, INI + timedelta(hours=1))]
        assert servicio.borrados == ["e1"]


# ── Analitica (funciones puras) ─────────────────────────────────────────────────────────────


class TestAnaliticaPura:
    @pytest.mark.parametrize(
        ("parte", "total", "esperado"),
        [(1, 3, 33.3), (2, 3, 66.7), (0, 5, 0.0), (5, 5, 100.0), (1, 0, None), (0, 0, None)],
    )
    def test_tasa(self, parte: int, total: int, esperado: float | None) -> None:
        """Una tasa del 0 % y una tasa sin datos no son lo mismo."""
        assert tasa(parte, total) == esperado

    def test_rango_por_defecto_incluye_hoy(self) -> None:
        desde, hasta = rango_por_defecto(date(2026, 10, 30))
        assert hasta == date(2026, 10, 30)
        assert desde == date(2026, 10, 1)
        assert (hasta - desde).days + 1 == 30

    def test_validar_rango(self) -> None:
        validar_rango(date(2026, 1, 1), date(2026, 1, 1))
        validar_rango(date(2026, 1, 1), date(2026, 1, 1) + timedelta(days=MAX_DIAS - 1))
        with pytest.raises(RangoInvalidoError):
            validar_rango(date(2026, 1, 2), date(2026, 1, 1))
        with pytest.raises(RangoInvalidoError):
            validar_rango(date(2026, 1, 1), date(2026, 1, 1) + timedelta(days=MAX_DIAS))

    def test_los_limites_son_dias_locales_del_negocio(self) -> None:
        """El 20 de octubre en Bogota empieza a las 05:00 UTC y acaba a las 05:00 UTC del 21."""
        inicio, fin = limites(date(2026, 10, 20), date(2026, 10, 20), ZoneInfo("America/Bogota"))
        assert inicio == datetime(2026, 10, 20, 5, 0, tzinfo=timezone.utc)
        assert fin == datetime(2026, 10, 21, 5, 0, tzinfo=timezone.utc)

    def test_el_dia_del_cambio_de_hora_dura_23_horas(self) -> None:
        inicio, fin = limites(date(2026, 3, 29), date(2026, 3, 29), ZoneInfo("Europe/Madrid"))
        assert fin - inicio == timedelta(hours=23)
