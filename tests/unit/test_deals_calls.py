"""Deals y llamadas agendadas sin base de datos (Sprint 19, slice Dev A).

Cubre los schemas, las reglas de etapa de un deal y su efecto en el lead, el resumen del
pipeline, la agenda (huecos, solapes, estados, recordatorios), el registro de proveedores de voz
y que la migracion 029 y los modelos usen los mismos valores.
"""

import importlib.util
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.models import deal as modelo_deal
from app.models import scheduled_call as modelo_llamada
from app.models.deal import Deal
from app.models.lead import Lead
from app.models.scheduled_call import ScheduledCall
from app.schemas.deal import (
    DealCreate,
    DealStageChange,
    DealUpdate,
    ScheduledCallCreate,
    ScheduledCallReschedule,
    validar_zona,
)
from app.services.call_scheduling import (
    RECORDATORIO_1H,
    RECORDATORIO_24H,
    TRANSICIONES,
    EstadoInvalidoError,
    Intervalo,
    huecos_libres,
    marcar_recordatorio,
    primer_solape,
    recordatorio_pendiente,
    validar_estado,
)
from app.services.deal_pipeline import (
    PROBABILIDAD_POR_ETAPA,
    TransicionInvalidaError,
    aplicar_efecto,
    aplicar_etapa,
    efecto_en_lead,
    resumir_pipeline,
    valor_ponderado,
)
from app.services.lead_sequence_timing import ventana_desde_perfil
from app.services.voice_call_provider import (
    OutboundCallRequest,
    ProviderCallRef,
    UnknownVoiceProviderError,
    VoiceCallProvider,
    available_voice_providers,
    build_voice_provider,
    register_voice_provider,
    unregister_voice_provider,
)

BOGOTA = ZoneInfo("America/Bogota")
AHORA = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)  # lunes 10:00 en Bogota
LEAD_ID = uuid.uuid4()
VENTANA = ventana_desde_perfil(None)  # Bogota, L-V 08:00-18:00


def _bog(*args: int) -> datetime:
    return datetime(*args, tzinfo=BOGOTA)  # type: ignore[misc]


def _deal(**kw: Any) -> Deal:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "client_id": uuid.uuid4(),
        "lead_id": LEAD_ID,
        "title": "Plan anual",
        "value": Decimal("1000.00"),
        "currency": "USD",
        "stage": "qualified",
        "probability": 25,
        "stage_changed_at": AHORA - timedelta(days=4),
    }
    return Deal(**{**base, **kw})


def _lead(**kw: Any) -> Lead:
    return Lead(**{"id": LEAD_ID, "client_id": uuid.uuid4(), "status": "active", **kw})


# ─── Schemas ────────────────────────────────────────────────────────────────────────────────


class TestSchemasDeal:
    def test_moneda_en_mayusculas(self) -> None:
        assert DealCreate(lead_id=LEAD_ID, title="x", currency=" cop ").currency == "COP"

    @pytest.mark.parametrize("moneda", ["US", "USDT", "U$D", "ÑAÑ", ""])
    def test_moneda_invalida(self, moneda: str) -> None:
        with pytest.raises(ValidationError, match="ISO"):
            DealCreate(lead_id=LEAD_ID, title="x", currency=moneda)

    def test_no_nace_cerrado(self) -> None:
        with pytest.raises(ValidationError, match="abierto"):
            DealCreate(lead_id=LEAD_ID, title="x", stage="closed_won")

    def test_valor_no_negativo_ni_con_tres_decimales(self) -> None:
        with pytest.raises(ValidationError):
            DealCreate(lead_id=LEAD_ID, title="x", value=Decimal("-1"))
        with pytest.raises(ValidationError):
            DealCreate(lead_id=LEAD_ID, title="x", value=Decimal("1.005"))

    def test_update_parcial(self) -> None:
        datos = DealUpdate(currency="eur")
        assert datos.model_dump(exclude_unset=True) == {"currency": "EUR"}

    def test_motivo_solo_al_perder(self) -> None:
        with pytest.raises(ValidationError, match="lost_reason"):
            DealStageChange(stage="proposal", lost_reason="caro")
        assert DealStageChange(stage="closed_lost", lost_reason="caro").lost_reason == "caro"

    def test_cerrado_sin_probabilidad_elegida(self) -> None:
        with pytest.raises(ValidationError, match="probabilidad"):
            DealStageChange(stage="closed_won", probability=80)


class TestSchemasLlamada:
    INICIO = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)

    def test_humana_necesita_persona(self) -> None:
        with pytest.raises(ValidationError, match="assigned_user_id"):
            ScheduledCallCreate(lead_id=LEAD_ID, scheduled_at=self.INICIO)

    def test_ia_necesita_proveedor_y_no_persona(self) -> None:
        with pytest.raises(ValidationError, match="ai_voice_provider"):
            ScheduledCallCreate(lead_id=LEAD_ID, scheduled_at=self.INICIO, call_type="ai_voice")
        ok = ScheduledCallCreate(
            lead_id=LEAD_ID, scheduled_at=self.INICIO, call_type="ai_voice",
            ai_voice_provider="vapi",
        )  # fmt: skip
        assert ok.assigned_user_id is None

    def test_mixta_necesita_ambos(self) -> None:
        with pytest.raises(ValidationError):
            ScheduledCallCreate(
                lead_id=LEAD_ID, scheduled_at=self.INICIO, call_type="hybrid",
                assigned_user_id=uuid.uuid4(),
            )  # fmt: skip

    def test_humana_sin_proveedor(self) -> None:
        with pytest.raises(ValidationError, match="no aplica"):
            ScheduledCallCreate(
                lead_id=LEAD_ID, scheduled_at=self.INICIO, assigned_user_id=uuid.uuid4(),
                ai_voice_provider="vapi",
            )  # fmt: skip

    def test_hora_sin_zona(self) -> None:
        with pytest.raises(ValidationError, match="zona horaria"):
            ScheduledCallCreate(
                lead_id=LEAD_ID, scheduled_at=datetime(2026, 10, 20, 15, 0),
                assigned_user_id=uuid.uuid4(),
            )  # fmt: skip
        with pytest.raises(ValidationError, match="zona horaria"):
            ScheduledCallReschedule(scheduled_at=datetime(2026, 10, 20, 15, 0))

    def test_zona_desconocida(self) -> None:
        with pytest.raises(ValidationError, match="desconocida"):
            ScheduledCallCreate(
                lead_id=LEAD_ID, scheduled_at=self.INICIO, assigned_user_id=uuid.uuid4(),
                timezone="Marte/Olimpo",
            )  # fmt: skip
        assert validar_zona("Europe/Madrid") == "Europe/Madrid"

    @pytest.mark.parametrize("minutos", [4, 481])
    def test_duracion(self, minutos: int) -> None:
        with pytest.raises(ValidationError):
            ScheduledCallCreate(
                lead_id=LEAD_ID, scheduled_at=self.INICIO, assigned_user_id=uuid.uuid4(),
                duration_minutes=minutos,
            )  # fmt: skip


# ─── Etapas de un deal ──────────────────────────────────────────────────────────────────────


class TestEtapas:
    def test_avanzar_usa_la_probabilidad_de_la_etapa(self) -> None:
        deal = _deal()
        assert aplicar_etapa(deal, "proposal", ahora=AHORA, zona="America/Bogota") == "qualified"
        assert (deal.stage, deal.probability, deal.stage_changed_at) == ("proposal", 50, AHORA)

    def test_retroceder_y_probabilidad_explicita(self) -> None:
        deal = _deal(stage="negotiation", probability=75)
        aplicar_etapa(deal, "qualified", ahora=AHORA, zona="America/Bogota", probabilidad=40)
        assert deal.probability == 40

    def test_ganar(self) -> None:
        deal = _deal()
        aplicar_etapa(deal, "closed_won", ahora=AHORA, zona="America/Bogota")
        assert (deal.probability, deal.won_at, deal.lost_at) == (100, AHORA, None)
        assert deal.actual_close_date == date(2026, 10, 5)

    def test_el_dia_de_cierre_es_el_local(self) -> None:
        # 02:00 UTC del 6 = 21:00 del 5 en Bogota.
        deal = _deal()
        noche = datetime(2026, 10, 6, 2, 0, tzinfo=timezone.utc)
        aplicar_etapa(deal, "closed_won", ahora=noche, zona="America/Bogota")
        assert deal.actual_close_date == date(2026, 10, 5)

    def test_perder_guarda_el_motivo(self) -> None:
        deal = _deal()
        aplicar_etapa(deal, "closed_lost", ahora=AHORA, zona="UTC", motivo_perdida="precio")
        assert (deal.probability, deal.lost_at, deal.won_at, deal.lost_reason) == (
            0, AHORA, None, "precio",
        )  # fmt: skip

    def test_reabrir_borra_el_cierre(self) -> None:
        deal = _deal()
        aplicar_etapa(deal, "closed_lost", ahora=AHORA, zona="UTC", motivo_perdida="precio")
        aplicar_etapa(deal, "negotiation", ahora=AHORA, zona="UTC")
        assert (deal.lost_at, deal.lost_reason, deal.actual_close_date) == (None, None, None)
        assert deal.probability == 75

    @pytest.mark.parametrize(
        ("actual", "nueva", "codigo"),
        [
            ("qualified", "qualified", "same_stage"),
            ("closed_won", "closed_lost", "closed_to_closed"),
            ("closed_lost", "closed_won", "closed_to_closed"),
            ("qualified", "archived", "unknown_stage"),
        ],
    )
    def test_transiciones_invalidas(self, actual: str, nueva: str, codigo: str) -> None:
        deal = _deal(stage=actual)
        with pytest.raises(TransicionInvalidaError) as exc:
            aplicar_etapa(deal, nueva, ahora=AHORA, zona="UTC")
        assert exc.value.code == codigo
        assert deal.stage == actual  # no se toco

    def test_probabilidades_por_defecto_cubren_todas_las_etapas(self) -> None:
        assert set(PROBABILIDAD_POR_ETAPA) == set(modelo_deal.DEAL_STAGES)


class TestEfectoEnLead:
    def test_ganar_convierte_al_lead(self) -> None:
        lead = _lead()
        efecto = efecto_en_lead(lead, "proposal", "closed_won", ahora=AHORA, otros_ganados=0)
        assert aplicar_efecto(lead, efecto)
        assert (lead.status, lead.converted_at) == ("won", AHORA)

    def test_conserva_la_primera_conversion(self) -> None:
        antes = AHORA - timedelta(days=30)
        lead = _lead(converted_at=antes)
        efecto = efecto_en_lead(lead, "proposal", "closed_won", ahora=AHORA, otros_ganados=0)
        assert efecto.converted_at == antes

    def test_perder_no_toca_al_lead(self) -> None:
        lead = _lead()
        efecto = efecto_en_lead(lead, "proposal", "closed_lost", ahora=AHORA, otros_ganados=0)
        assert not aplicar_efecto(lead, efecto)
        assert lead.status == "active"

    def test_reabrir_el_unico_ganado_devuelve_a_activo(self) -> None:
        lead = _lead(status="won", converted_at=AHORA)
        efecto = efecto_en_lead(lead, "closed_won", "negotiation", ahora=AHORA, otros_ganados=0)
        aplicar_efecto(lead, efecto)
        assert (lead.status, lead.converted_at) == ("active", None)

    def test_reabrir_con_otro_ganado_no_cambia(self) -> None:
        lead = _lead(status="won")
        efecto = efecto_en_lead(lead, "closed_won", "negotiation", ahora=AHORA, otros_ganados=1)
        assert not aplicar_efecto(lead, efecto)

    @pytest.mark.parametrize("estado", ["lost", "disqualified"])
    def test_no_resucita_un_lead_cerrado_a_mano(self, estado: str) -> None:
        lead = _lead(status=estado)
        efecto = efecto_en_lead(lead, "proposal", "closed_won", ahora=AHORA, otros_ganados=0)
        assert not aplicar_efecto(lead, efecto)


class TestResumen:
    def test_valor_ponderado_redondea_la_mitad_hacia_arriba(self) -> None:
        assert valor_ponderado(Decimal("0.05"), 50) == Decimal("0.03")
        assert valor_ponderado(Decimal("1000.00"), 25) == Decimal("250.00")

    def test_agrupa_por_etapa_y_moneda_sin_mezclar(self) -> None:
        deals = [
            _deal(value=Decimal("100"), probability=50, stage="proposal"),
            _deal(value=Decimal("300"), probability=50, stage="proposal"),
            _deal(value=Decimal("1000000"), currency="COP", probability=10, stage="proposal"),
            _deal(value=Decimal("50"), probability=10, stage="new_contact",
                  stage_changed_at=AHORA - timedelta(days=2)),
        ]  # fmt: skip
        filas = resumir_pipeline(deals, ahora=AHORA)
        assert [(f.stage, f.currency, f.count) for f in filas] == [
            ("new_contact", "USD", 1),
            ("proposal", "COP", 1),
            ("proposal", "USD", 2),
        ]
        usd = filas[2]
        assert (usd.total_value, usd.weighted_value, usd.avg_days_in_stage) == (
            Decimal("400"), Decimal("200.00"), 4.0,
        )  # fmt: skip
        assert filas[0].avg_days_in_stage == 2.0

    def test_vacio(self) -> None:
        assert resumir_pipeline([], ahora=AHORA) == []


# ─── Agenda ─────────────────────────────────────────────────────────────────────────────────


class TestIntervalos:
    def test_tocarse_no_es_solapar(self) -> None:
        a = Intervalo.de_llamada(AHORA, 30)
        assert not a.solapa(Intervalo.de_llamada(AHORA + timedelta(minutes=30), 30))
        assert a.solapa(Intervalo.de_llamada(AHORA + timedelta(minutes=29), 30))
        assert a.solapa(Intervalo.de_llamada(AHORA - timedelta(minutes=10), 120))

    def test_primer_solape(self) -> None:
        ocupado = Intervalo.de_llamada(AHORA, 60)
        assert primer_solape(Intervalo.de_llamada(AHORA, 30), [ocupado]) == ocupado
        assert (
            primer_solape(Intervalo.de_llamada(AHORA + timedelta(hours=2), 30), [ocupado]) is None
        )


class TestHuecos:
    def _huecos(self, **kw: Any) -> list[datetime]:
        base: dict[str, Any] = {
            "desde": _bog(2026, 10, 6, 0, 0),  # martes
            "hasta": _bog(2026, 10, 7, 0, 0),
            "duracion_minutos": 30,
            "ventana": VENTANA,
            "ahora": AHORA,
        }
        return huecos_libres(**{**base, **kw})

    def test_dia_completo(self) -> None:
        huecos = self._huecos()
        assert len(huecos) == 20  # 08:00 a 17:30 cada 30 min
        assert huecos[0] == _bog(2026, 10, 6, 8, 0)
        assert huecos[-1] == _bog(2026, 10, 6, 17, 30)
        assert all(h.tzinfo == timezone.utc for h in huecos)

    def test_la_llamada_cabe_entera_antes_del_cierre(self) -> None:
        huecos = self._huecos(duracion_minutos=90)
        assert huecos[-1] == _bog(2026, 10, 6, 16, 30)

    def test_salta_lo_ocupado(self) -> None:
        ocupado = Intervalo.de_llamada(_bog(2026, 10, 6, 9, 15), 30)
        huecos = self._huecos(ocupados=[ocupado])
        assert _bog(2026, 10, 6, 9, 0) not in huecos
        assert _bog(2026, 10, 6, 9, 30) not in huecos
        assert _bog(2026, 10, 6, 8, 30) in huecos
        assert _bog(2026, 10, 6, 10, 0) in huecos

    def test_antelacion_minima(self) -> None:
        huecos = self._huecos(desde=_bog(2026, 10, 5, 0, 0), hasta=_bog(2026, 10, 6, 0, 0))
        # Son las 10:00 del lunes: lo primero es a las 11:00.
        assert huecos[0] == _bog(2026, 10, 5, 11, 0)

    def test_fin_de_semana_cerrado(self) -> None:
        assert self._huecos(desde=_bog(2026, 10, 10, 0, 0), hasta=_bog(2026, 10, 12, 0, 0)) == []

    def test_maximo(self) -> None:
        assert len(self._huecos(hasta=_bog(2026, 10, 20, 0, 0), maximo=7)) == 7

    def test_negocio_siempre_cerrado_no_cuelga(self) -> None:
        dias = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        cerrado = ventana_desde_perfil({"operating_hours": {d: {"is_open": False} for d in dias}})
        assert self._huecos(ventana=cerrado, hasta=_bog(2027, 10, 6, 0, 0)) == []

    def test_cambio_de_hora(self) -> None:
        # Madrid pasa a invierno el 25-10-2026: el lunes 26 abre a las 08:00 locales = 07:00 UTC.
        madrid = ventana_desde_perfil({"timezone": "Europe/Madrid"})
        huecos = self._huecos(
            ventana=madrid,
            desde=datetime(2026, 10, 26, 0, 0, tzinfo=ZoneInfo("Europe/Madrid")),
            hasta=datetime(2026, 10, 27, 0, 0, tzinfo=ZoneInfo("Europe/Madrid")),
            ahora=datetime(2026, 10, 20, tzinfo=timezone.utc),
        )
        assert huecos[0] == datetime(2026, 10, 26, 7, 0, tzinfo=timezone.utc)


class TestEstados:
    @pytest.mark.parametrize(
        ("actual", "nuevo"),
        [("pending", "confirmed"), ("confirmed", "completed"), ("in_progress", "no_show")],
    )
    def test_validas(self, actual: str, nuevo: str) -> None:
        validar_estado(actual, nuevo)

    @pytest.mark.parametrize(
        ("actual", "nuevo"),
        [
            ("completed", "pending"),
            ("cancelled", "confirmed"),
            ("pending", "completed"),  # no se completa lo que nadie confirmo ni empezo
            ("rescheduled", "confirmed"),
        ],
    )
    def test_invalidas(self, actual: str, nuevo: str) -> None:
        with pytest.raises(EstadoInvalidoError):
            validar_estado(actual, nuevo)

    def test_todos_los_estados_tienen_regla(self) -> None:
        assert set(TRANSICIONES) == set(modelo_llamada.CALL_STATUSES)


class TestRecordatorios:
    INICIO = AHORA + timedelta(days=3)

    def _llamada(self, **kw: Any) -> ScheduledCall:
        base: dict[str, Any] = {
            "status": "pending",
            "scheduled_at": self.INICIO,
            "created_at": AHORA,
        }
        return ScheduledCall(**{**base, **kw})

    @pytest.mark.parametrize(
        ("antes", "esperado"),
        [
            (timedelta(hours=30), None),
            (timedelta(hours=24), RECORDATORIO_24H),
            (timedelta(hours=2), RECORDATORIO_24H),
            (timedelta(hours=1), RECORDATORIO_1H),
            (timedelta(minutes=5), RECORDATORIO_1H),
            (timedelta(0), None),
            (-timedelta(minutes=5), None),
        ],
    )
    def test_ventanas(self, antes: timedelta, esperado: str | None) -> None:
        assert recordatorio_pendiente(self._llamada(), self.INICIO - antes) == esperado

    def test_no_repite(self) -> None:
        llamada = self._llamada(reminder_24h_sent_at=AHORA)
        assert recordatorio_pendiente(llamada, self.INICIO - timedelta(hours=10)) is None
        llamada.reminder_1h_sent_at = AHORA
        assert recordatorio_pendiente(llamada, self.INICIO - timedelta(minutes=30)) is None

    def test_agendada_con_poco_tiempo_no_recibe_el_de_24h(self) -> None:
        llamada = self._llamada(created_at=self.INICIO - timedelta(hours=5))
        assert recordatorio_pendiente(llamada, self.INICIO - timedelta(hours=4)) is None
        assert recordatorio_pendiente(llamada, self.INICIO - timedelta(minutes=50)) == (
            RECORDATORIO_1H
        )

    @pytest.mark.parametrize("estado", ["cancelled", "completed", "rescheduled", "in_progress"])
    def test_solo_las_que_siguen_en_pie(self, estado: str) -> None:
        llamada = self._llamada(status=estado)
        assert recordatorio_pendiente(llamada, self.INICIO - timedelta(minutes=30)) is None

    def test_marcar(self) -> None:
        llamada = self._llamada()
        marcar_recordatorio(llamada, RECORDATORIO_24H, AHORA)
        marcar_recordatorio(llamada, RECORDATORIO_1H, AHORA)
        assert llamada.reminder_24h_sent_at == llamada.reminder_1h_sent_at == AHORA
        with pytest.raises(ValueError, match="desconocido"):
            marcar_recordatorio(llamada, "15min", AHORA)


# ─── Proveedores de voz ─────────────────────────────────────────────────────────────────────


class ProveedorFalso(VoiceCallProvider):
    name: ClassVar[str] = "falso-voz"

    async def start_call(self, request: OutboundCallRequest) -> ProviderCallRef:
        return ProviderCallRef(provider=self.name, external_id="ext-1")

    async def cancel_call(self, ref: ProviderCallRef) -> bool:
        return True


class TestProveedoresDeVoz:
    def test_registro(self) -> None:
        register_voice_provider(ProveedorFalso)
        try:
            assert "falso-voz" in available_voice_providers()
            assert isinstance(build_voice_provider("falso-voz"), ProveedorFalso)
            register_voice_provider(ProveedorFalso)  # la misma clase otra vez: vale
        finally:
            unregister_voice_provider("falso-voz")
        with pytest.raises(UnknownVoiceProviderError):
            build_voice_provider("falso-voz")

    def test_nombre_obligatorio_y_que_quepa(self) -> None:
        class SinNombre(ProveedorFalso):
            name: ClassVar[str] = ""

        class Largo(ProveedorFalso):
            name: ClassVar[str] = "x" * 31

        with pytest.raises(ValueError, match="name"):
            register_voice_provider(SinNombre)
        with pytest.raises(ValueError, match="30"):
            register_voice_provider(Largo)

    def test_nombre_tomado_por_otra_clase(self) -> None:
        class Otro(ProveedorFalso):
            name: ClassVar[str] = "falso-voz"

        register_voice_provider(ProveedorFalso)
        try:
            with pytest.raises(ValueError, match="Ya hay"):
                register_voice_provider(Otro)
        finally:
            unregister_voice_provider("falso-voz")

    def test_el_repr_no_lleva_el_telefono(self) -> None:
        pedido = OutboundCallRequest(
            scheduled_call_id=uuid.uuid4(), client_id=uuid.uuid4(), to_phone="+573001112233"
        )
        assert "3001112233" not in repr(pedido)
        assert "3001112233" not in str(pedido)


# ─── Coherencia migracion / modelos / schemas ───────────────────────────────────────────────


def _migracion() -> Any:
    ruta = Path(__file__).parents[2] / "migrations" / "versions" / "029_deals_scheduled_calls.py"
    spec = importlib.util.spec_from_file_location("m029", ruta)
    assert spec is not None
    assert spec.loader is not None
    migracion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migracion)
    return migracion


def test_valores_iguales_en_modelos_y_migracion() -> None:
    migracion = _migracion()
    assert modelo_deal.DEAL_STAGES == migracion.DEAL_STAGES
    assert modelo_llamada.CALL_TYPES == migracion.CALL_TYPES
    assert modelo_llamada.CALL_STATUSES == migracion.CALL_STATUSES


def test_literales_del_schema_iguales_que_los_modelos() -> None:
    from typing import get_args

    from app.schemas.deal import CallStatus, CallType, DealStage

    assert get_args(DealStage) == modelo_deal.DEAL_STAGES
    assert get_args(CallType) == modelo_llamada.CALL_TYPES
    assert get_args(CallStatus) == modelo_llamada.CALL_STATUSES
