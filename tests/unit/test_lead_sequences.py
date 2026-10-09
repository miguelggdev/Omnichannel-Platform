"""Secuencias de follow-up sin base de datos (Sprint 18, slice Dev A).

Cubre los schemas de pasos y su grafo, el motor `decidir()`, el renderizado de plantillas, el
smart timing (horario, zona horaria, cambio de hora), las plantillas predefinidas, los
disparadores y que la migracion 028 y los modelos usen los mismos valores.
"""

import importlib.util
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.models import lead_sequence as modelo
from app.models.lead import Lead
from app.schemas.lead_sequence import (
    Branch,
    ConditionStep,
    MessageStep,
    SequenceDefinition,
    TaskStep,
    TriggerConditions,
    WaitStep,
    paso_a_fila,
    paso_desde_fila,
    validar_grafo,
    variables_de_plantilla,
)
from app.services.lead_sequence_engine import (
    MAX_STEPS_PER_ENROLLMENT,
    Complete,
    CreateTask,
    Decision,
    Exit,
    LeadSnapshot,
    SendMessage,
    Wait,
    decidir,
    elegir_canal,
    evaluar_condicion,
    renderizar_plantilla,
)
from app.services.lead_sequence_templates import PLANTILLAS
from app.services.lead_sequence_timing import (
    MIN_RESPUESTAS,
    hora_preferida,
    programar_siguiente,
    siguiente_apertura,
    ventana_desde_perfil,
)
from app.services.lead_sequences import (
    DatosDisparo,
    SecuenciaError,
    cumple_disparador,
    motivo_para_no_inscribir,
    pausar,
    reanudar,
    registrar_avance,
)

BOGOTA = ZoneInfo("America/Bogota")
MADRID = ZoneInfo("Europe/Madrid")
PRIORIDAD = ("whatsapp", "email", "instagram")
LEAD = LeadSnapshot(channels=frozenset({"whatsapp", "email"}))

MSG = MessageStep(body="Hola {{first_name}}")
ESPERA = WaitStep(amount=1, unit="days")


def _decidir(pasos: list[Any], lead: LeadSnapshot = LEAD, **kw: Any) -> Decision:
    kw.setdefault("current_step", 1)
    kw.setdefault("steps_executed", 0)
    kw.setdefault("channel_priority", PRIORIDAD)
    return decidir(pasos=pasos, lead=lead, **kw)


# ─── Schemas ────────────────────────────────────────────────────────────────────────────────


class TestPlantillas:
    def test_extrae_variables_con_espacios(self) -> None:
        assert variables_de_plantilla("Hola {{ first_name }} de {{company_name}}") == {
            "first_name",
            "company_name",
        }

    def test_rechaza_variable_desconocida(self) -> None:
        with pytest.raises(ValidationError, match="Variables desconocidas"):
            MessageStep(body="Hola {{email}}")

    def test_rechaza_llaves_sueltas(self) -> None:
        with pytest.raises(ValidationError, match="llaves"):
            MessageStep(body="Hola {{first_name}} }}")

    def test_template_sin_body_no_vale(self) -> None:
        with pytest.raises(ValidationError, match="necesita body"):
            MessageStep(mode="template")

    def test_ai_sin_body_vale(self) -> None:
        assert MessageStep(mode="ai", ai_instructions="Presentate").body is None

    def test_subject_solo_en_email(self) -> None:
        with pytest.raises(ValidationError, match="subject"):
            MessageStep(channel="whatsapp", body="x", subject="Asunto")
        assert MessageStep(channel="email", body="x", subject="Asunto").subject == "Asunto"

    def test_subject_valida_variables(self) -> None:
        with pytest.raises(ValidationError):
            MessageStep(channel="email", body="x", subject="{{phone}}")


class TestPasos:
    def test_espera_maxima(self) -> None:
        assert WaitStep(amount=90, unit="days").delta == timedelta(days=90)
        with pytest.raises(ValidationError, match="90"):
            WaitStep(amount=91, unit="days")

    def test_goto_necesita_destino(self) -> None:
        with pytest.raises(ValidationError):
            Branch(action="goto")
        with pytest.raises(ValidationError):
            Branch(action="exit", goto_position=2)

    @pytest.mark.parametrize(
        ("check", "value"),
        [
            ("score_at_least", None),
            ("score_at_least", 101),
            ("score_at_least", True),
            ("score_at_least", "50"),
            ("stage_is", None),
            ("stage_is", "Etapa Mala"),
            ("has_channel", "webchat"),
            ("replied", "x"),
        ],
    )
    def test_condicion_con_valor_invalido(self, check: str, value: Any) -> None:
        with pytest.raises(ValidationError):
            ConditionStep(check=check, value=value)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("check", "value"),
        [
            ("score_at_least", 0),
            ("stage_is", "qualified"),
            ("has_channel", "email"),
            ("replied", None),
        ],
    )
    def test_condicion_valida(self, check: str, value: Any) -> None:
        assert ConditionStep(check=check, value=value).value == value  # type: ignore[arg-type]

    def test_campos_extra_rechazados(self) -> None:
        with pytest.raises(ValidationError):
            TaskStep(title="x", sorpresa=1)  # type: ignore[call-arg]

    @pytest.mark.parametrize(
        "paso",
        [
            MSG,
            ESPERA,
            ConditionStep(check="replied", if_true=Branch(action="exit")),
            TaskStep(title="Llamar", pause_until_done=True),
        ],
    )
    def test_ida_y_vuelta_a_fila(self, paso: Any) -> None:
        step_type, config = paso_a_fila(paso)
        assert "type" not in config
        assert paso_desde_fila(step_type, config) == paso

    def test_fila_invalida(self) -> None:
        with pytest.raises(ValidationError):
            paso_desde_fila("wait", {"amount": 0})
        with pytest.raises(ValidationError):
            paso_desde_fila("teleport", {})


class TestGrafo:
    def test_necesita_algo_que_hacer(self) -> None:
        with pytest.raises(ValueError, match="message"):
            validar_grafo([ESPERA])

    def test_salto_a_paso_inexistente(self) -> None:
        cond = ConditionStep(check="replied", if_true=Branch(action="goto", goto_position=5))
        with pytest.raises(ValueError, match="no existe"):
            validar_grafo([MSG, cond])

    def test_salto_a_si_mismo(self) -> None:
        cond = ConditionStep(check="replied", if_false=Branch(action="goto", goto_position=2))
        with pytest.raises(ValueError, match="si mismo"):
            validar_grafo([MSG, cond])

    def test_bucle_sin_espera(self) -> None:
        cond = ConditionStep(check="no_reply", if_true=Branch(action="goto", goto_position=1))
        with pytest.raises(ValueError, match="bucle"):
            validar_grafo([MSG, cond])

    def test_bucle_con_espera_vale(self) -> None:
        cond = ConditionStep(check="no_reply", if_true=Branch(action="goto", goto_position=1))
        validar_grafo([MSG, ESPERA, cond])

    def test_bucle_que_se_salta_la_espera_con_un_goto_adelante(self) -> None:
        # 1 -> 3 salta la espera del paso 2; 4 -> 1 cierra el bucle: mensajes sin parar.
        pasos = [
            ConditionStep(check="no_reply", if_true=Branch(action="goto", goto_position=3)),
            ESPERA,
            MSG,
            ConditionStep(check="no_reply", if_true=Branch(action="goto", goto_position=1)),
        ]
        with pytest.raises(ValueError, match="bucle"):
            validar_grafo(pasos)

    def test_bucle_cuya_unica_salida_sin_espera_es_exit_vale(self) -> None:
        pasos = [
            MSG,
            ConditionStep(check="replied", if_true=Branch(action="exit")),
            ESPERA,
            ConditionStep(check="no_reply", if_true=Branch(action="goto", goto_position=1)),
        ]
        validar_grafo(pasos)

    def test_bucle_por_la_rama_continue(self) -> None:
        # 1 sigue (continue) a 2, que vuelve a 1: ninguna espera en medio.
        pasos = [
            ConditionStep(check="replied", if_true=Branch(action="goto", goto_position=3)),
            ConditionStep(check="no_reply", if_true=Branch(action="goto", goto_position=1)),
            MSG,
        ]
        with pytest.raises(ValueError, match="bucle"):
            validar_grafo(pasos)

    def test_bucle_solo_de_condiciones(self) -> None:
        pasos = [
            MSG,
            ConditionStep(check="replied", if_false=Branch(action="goto", goto_position=3)),
            ConditionStep(check="replied", if_false=Branch(action="goto", goto_position=2)),
        ]
        with pytest.raises(ValueError, match="bucle"):
            validar_grafo(pasos)

    def test_bucle_con_espera_fuera_del_tramo_no_vale(self) -> None:
        cond = ConditionStep(check="no_reply", if_true=Branch(action="goto", goto_position=2))
        with pytest.raises(ValueError, match="bucle"):
            validar_grafo([ESPERA, MSG, cond])

    def test_salto_adelante_sin_espera_vale(self) -> None:
        cond = ConditionStep(check="replied", if_true=Branch(action="goto", goto_position=3))
        validar_grafo([cond, MSG, MSG])

    def test_definicion_aplica_el_grafo(self) -> None:
        with pytest.raises(ValidationError, match="message"):
            SequenceDefinition(name="x", steps=[ESPERA])

    def test_prioridad_sin_repetidos(self) -> None:
        with pytest.raises(ValidationError, match="repetir"):
            SequenceDefinition(name="x", steps=[MSG], channel_priority=["email", "email"])

    def test_prioridad_por_defecto_igual_que_la_base(self) -> None:
        assert SequenceDefinition(name="x", steps=[MSG]).channel_priority == list(PRIORIDAD)

    def test_maximo_de_pasos(self) -> None:
        with pytest.raises(ValidationError):
            SequenceDefinition(name="x", steps=[MSG] * 31)


class TestDisparadores:
    def test_slugs_validos_y_sin_repetir(self) -> None:
        assert TriggerConditions(stages=["new", "new", "qualified"]).stages == ["new", "qualified"]
        with pytest.raises(ValidationError):
            TriggerConditions(stages=["Nueva Etapa"])

    def test_sin_eventos_es_manual(self) -> None:
        assert not TriggerConditions().automatica
        assert TriggerConditions(events=["lead_created"]).automatica

    def _datos(self, **kw: Any) -> DatosDisparo:
        base: dict[str, Any] = {
            "source_type": "web_form",
            "stage_slug": "new",
            "temperature": "warm",
            "total_score": 50,
            "fit_score": 60,
        }
        return DatosDisparo(**{**base, **kw})

    def test_evento_que_no_esta(self) -> None:
        cond = TriggerConditions(events=["stage_changed"])
        assert not cumple_disparador(cond, "lead_created", self._datos())

    def test_sin_eventos_nunca_dispara(self) -> None:
        assert not cumple_disparador(TriggerConditions(), "lead_created", self._datos())

    def test_sin_filtros_dispara(self) -> None:
        assert cumple_disparador(
            TriggerConditions(events=["lead_created"]), "lead_created", self._datos()
        )

    @pytest.mark.parametrize(
        ("filtros", "datos", "esperado"),
        [
            ({"source_types": ["web_form"]}, {}, True),
            ({"source_types": ["import"]}, {}, False),
            ({"source_types": ["web_form"]}, {"source_type": None}, False),
            ({"stages": ["new"]}, {}, True),
            ({"stages": ["won"]}, {}, False),
            ({"temperatures": ["hot"]}, {}, False),
            ({"min_total_score": 50}, {}, True),
            ({"min_total_score": 51}, {}, False),
            ({"min_fit_score": 60}, {}, True),
            ({"min_fit_score": 61}, {}, False),
        ],
    )
    def test_filtros(self, filtros: dict[str, Any], datos: dict[str, Any], esperado: bool) -> None:
        cond = TriggerConditions(events=["lead_created"], **filtros)
        assert cumple_disparador(cond, "lead_created", self._datos(**datos)) is esperado


# ─── Motor ──────────────────────────────────────────────────────────────────────────────────


class TestCondicionesYCanales:
    @pytest.mark.parametrize(
        ("check", "value", "lead", "esperado"),
        [
            ("replied", None, LeadSnapshot(replied=True), True),
            ("no_reply", None, LeadSnapshot(replied=True), False),
            ("email_opened", None, LeadSnapshot(email_opened=True), True),
            ("link_clicked", None, LeadSnapshot(), False),
            ("score_at_least", 40, LeadSnapshot(total_score=40), True),
            ("score_at_least", 41, LeadSnapshot(total_score=40), False),
            ("stage_is", "qualified", LeadSnapshot(stage_slug="qualified"), True),
            ("stage_is", "qualified", LeadSnapshot(), False),
            ("has_channel", "email", LEAD, True),
            ("has_channel", "telegram", LEAD, False),
        ],
    )
    def test_evaluar(self, check: str, value: Any, lead: LeadSnapshot, esperado: bool) -> None:
        paso = ConditionStep(check=check, value=value)  # type: ignore[arg-type]
        assert evaluar_condicion(paso, lead) is esperado

    def test_canal_auto_respeta_prioridad(self) -> None:
        assert elegir_canal(MSG, ("email", "whatsapp"), LEAD.channels) == "email"
        assert elegir_canal(MSG, ("instagram",), LEAD.channels) is None

    def test_canal_fijo(self) -> None:
        paso = MessageStep(channel="instagram", body="x")
        assert elegir_canal(paso, PRIORIDAD, frozenset({"instagram"})) == "instagram"
        assert elegir_canal(paso, PRIORIDAD, LEAD.channels) is None


class TestDecidir:
    @pytest.mark.parametrize(
        ("lead", "activa", "motivo"),
        [
            (LEAD, False, modelo.EXIT_SEQUENCE_DISABLED),
            (LeadSnapshot(deleted=True, anonymized=True), True, modelo.EXIT_LEAD_DELETED),
            (LeadSnapshot(anonymized=True, status="won"), True, modelo.EXIT_GDPR),
            (LeadSnapshot(status="won"), True, modelo.EXIT_LEAD_CLOSED),
            (LeadSnapshot(status="disqualified"), True, modelo.EXIT_LEAD_CLOSED),
        ],
    )
    def test_guardas(self, lead: LeadSnapshot, activa: bool, motivo: str) -> None:
        d = _decidir([MSG], lead, secuencia_activa=activa, current_step=1)
        assert d == Decision(Exit(motivo), next_step=1, steps_consumed=0)

    def test_mensaje(self) -> None:
        d = _decidir([MSG, ESPERA])
        assert d.action == SendMessage(1, MSG, "whatsapp")
        assert (d.next_step, d.steps_consumed) == (2, 1)

    def test_espera(self) -> None:
        d = _decidir([MSG, ESPERA], current_step=2)
        assert d.action == Wait(2, ESPERA)
        assert d.next_step == 3

    def test_tarea(self) -> None:
        tarea = TaskStep(title="Llamar")
        assert _decidir([tarea]).action == CreateTask(1, tarea)

    def test_fin(self) -> None:
        d = _decidir([MSG], current_step=2)
        assert d.action == Complete()
        assert d.steps_consumed == 0

    def test_condicion_encadena_sin_esperar(self) -> None:
        cond = ConditionStep(check="replied", if_false=Branch(action="goto", goto_position=3))
        d = _decidir([cond, TaskStep(title="no"), MSG])
        assert d.action == SendMessage(3, MSG, "whatsapp")
        assert (d.next_step, d.steps_consumed) == (4, 2)

    def test_condicion_continue(self) -> None:
        cond = ConditionStep(check="replied")
        d = _decidir([cond, MSG], LeadSnapshot(channels=LEAD.channels, replied=True))
        assert d.action == SendMessage(2, MSG, "whatsapp")

    def test_condicion_exit(self) -> None:
        cond = ConditionStep(check="replied", if_true=Branch(action="exit"))
        d = _decidir([cond, MSG], LeadSnapshot(channels=LEAD.channels, replied=True))
        assert d.action == Exit(modelo.EXIT_CONDITION)
        assert d.steps_consumed == 1

    def test_salta_mensaje_de_canal_que_no_tiene(self) -> None:
        email = MessageStep(channel="email", body="x")
        d = _decidir([email, MSG], LeadSnapshot(channels=frozenset({"whatsapp"})))
        assert d.action == SendMessage(2, MSG, "whatsapp")
        assert d.skipped == (1,)
        assert d.steps_consumed == 2

    def test_auto_sin_canales_sale(self) -> None:
        d = _decidir([MSG], LeadSnapshot())
        assert d.action == Exit(modelo.EXIT_NO_CHANNEL)

    def test_tope_de_pasos(self) -> None:
        d = _decidir([MSG], steps_executed=MAX_STEPS_PER_ENROLLMENT)
        assert d.action == Exit(modelo.EXIT_STEP_LIMIT)

    def test_tope_corta_cadena_de_condiciones(self) -> None:
        conds = [ConditionStep(check="replied") for _ in range(5)]
        d = _decidir([*conds, MSG], steps_executed=MAX_STEPS_PER_ENROLLMENT - 3)
        assert d.action == Exit(modelo.EXIT_STEP_LIMIT)
        assert (d.next_step, d.steps_consumed) == (4, 3)


class TestRenderizar:
    def test_sustituye(self) -> None:
        texto = "Hola {{ first_name }}, de {{company_name}}"
        assert renderizar_plantilla(texto, {"first_name": "Ana", "company_name": "Acme"}) == (
            "Hola Ana, de Acme"
        )

    def test_variable_vacia_limpia_espacios(self) -> None:
        assert renderizar_plantilla("Hola {{first_name}}, ¿que tal?", {}) == "Hola, ¿que tal?"
        assert renderizar_plantilla("{{first_name}} hola", {}) == "hola"

    def test_no_reescribe_el_texto_del_tenant(self) -> None:
        texto = "Bonjour {{first_name}} ! Des questions ? Rendez-vous a 10 : 30."
        assert renderizar_plantilla(texto, {"first_name": "Marie"}) == (
            "Bonjour Marie ! Des questions ? Rendez-vous a 10 : 30."
        )
        assert renderizar_plantilla("Bonjour {{first_name}} !", {}) == "Bonjour !"
        assert renderizar_plantilla("A  B {{first_name}}", {"first_name": "C"}) == "A  B C"

    def test_no_interpreta_format(self) -> None:
        texto = "{0.__class__} {first_name} {{first_name}}"
        assert (
            renderizar_plantilla(texto, {"first_name": "Ana"}) == "{0.__class__} {first_name} Ana"
        )

    def test_el_valor_no_se_reinterpreta(self) -> None:
        valor = "{{company_name}}"
        assert renderizar_plantilla(
            "{{first_name}}", {"first_name": valor, "company_name": "X"}
        ) == (valor)

    def test_conserva_saltos_de_linea(self) -> None:
        assert renderizar_plantilla("Hola {{first_name}}\n\nChao", {"first_name": "Ana"}) == (
            "Hola Ana\n\nChao"
        )


# ─── Timing ─────────────────────────────────────────────────────────────────────────────────


def _bog(*args: int) -> datetime:
    return datetime(*args, tzinfo=BOGOTA)  # type: ignore[misc]


VENTANA = ventana_desde_perfil(None)


class TestVentana:
    def test_por_defecto_bogota_lunes_a_viernes(self) -> None:
        assert VENTANA.tz == BOGOTA
        assert VENTANA.horario[0] == (time(8), time(18))
        assert VENTANA.horario[5] is None
        assert VENTANA.horario[6] is None

    def test_desde_perfil(self) -> None:
        perfil = {
            "timezone": "Europe/Madrid",
            "operating_hours": {
                "saturday": {"is_open": True, "open_time": "10:00", "close_time": "14:00"},
                "monday": {"is_open": False},
            },
        }
        v = ventana_desde_perfil(perfil)
        assert v.tz == MADRID
        assert v.horario[5] == (time(10), time(14))
        assert v.horario[0] is None
        assert v.horario[1] == (time(8), time(18))

    def test_perfil_roto_no_rompe(self) -> None:
        perfil = {
            "timezone": "Marte/Olimpo",
            "operating_hours": {
                "monday": {"is_open": True, "open_time": "18:00", "close_time": "08:00"},
                "tuesday": "basura",
            },
        }
        v = ventana_desde_perfil(perfil)
        assert v.tz == BOGOTA
        assert v.horario[0] == (time(8), time(18))
        assert v.horario[1] == (time(8), time(18))

    def test_horario_que_no_es_dict(self) -> None:
        assert ventana_desde_perfil({"operating_hours": ["x"]}).horario[2] == (time(8), time(18))

    def test_abierto_limites(self) -> None:
        assert VENTANA.abierto(_bog(2026, 10, 5, 8, 0))  # lunes
        assert not VENTANA.abierto(_bog(2026, 10, 5, 18, 0))
        assert not VENTANA.abierto(_bog(2026, 10, 5, 7, 59))
        assert not VENTANA.abierto(_bog(2026, 10, 10, 12, 0))  # sabado

    def test_abierto_convierte_zona(self) -> None:
        # 13:00 UTC = 08:00 en Bogota.
        assert VENTANA.abierto(datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc))
        assert not VENTANA.abierto(datetime(2026, 10, 5, 12, 59, tzinfo=timezone.utc))


class TestSiguienteApertura:
    def test_abierto_no_mueve(self) -> None:
        momento = _bog(2026, 10, 5, 10, 0)
        assert siguiente_apertura(momento, VENTANA) == momento

    def test_antes_de_abrir(self) -> None:
        assert siguiente_apertura(_bog(2026, 10, 5, 3, 0), VENTANA) == _bog(2026, 10, 5, 8, 0)

    def test_tras_cerrar_pasa_al_dia_siguiente(self) -> None:
        assert siguiente_apertura(_bog(2026, 10, 5, 19, 0), VENTANA) == _bog(2026, 10, 6, 8, 0)

    def test_viernes_noche_al_lunes(self) -> None:
        assert siguiente_apertura(_bog(2026, 10, 9, 20, 0), VENTANA) == _bog(2026, 10, 12, 8, 0)

    def test_negocio_siempre_cerrado(self) -> None:
        cerrado = ventana_desde_perfil({"operating_hours": {d: {"is_open": False} for d in _DIAS}})
        momento = _bog(2026, 10, 5, 3, 0)
        assert siguiente_apertura(momento, cerrado) == momento

    def test_cambio_de_hora(self) -> None:
        # Madrid pasa a horario de invierno el domingo 25-10-2026: el lunes abre a las 08:00
        # locales, que son las 07:00 UTC (no las 06:00 del horario de verano).
        v = ventana_desde_perfil({"timezone": "Europe/Madrid"})
        sabado = datetime(2026, 10, 24, 12, 0, tzinfo=MADRID)
        apertura = siguiente_apertura(sabado, v)
        assert apertura.astimezone(timezone.utc) == datetime(
            2026, 10, 26, 7, 0, tzinfo=timezone.utc
        )


_DIAS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class TestHoraPreferida:
    def test_poca_historia(self) -> None:
        assert hora_preferida([10] * (MIN_RESPUESTAS - 1)) is None

    def test_moda(self) -> None:
        assert hora_preferida([9, 15, 15, 20]) == 15

    def test_empate_la_mas_temprana(self) -> None:
        assert hora_preferida([20, 9, 20, 9]) == 9

    def test_ignora_horas_invalidas(self) -> None:
        assert hora_preferida([10, 10, 99]) is None


class TestProgramarSiguiente:
    def test_espera_simple_en_horario(self) -> None:
        ahora = _bog(2026, 10, 5, 10, 0)
        r = programar_siguiente(ahora, WaitStep(amount=1, unit="days"), VENTANA)
        assert r == _bog(2026, 10, 6, 10, 0)
        assert r.tzinfo == ZoneInfo("UTC")

    def test_respeta_horario(self) -> None:
        ahora = _bog(2026, 10, 5, 17, 0)
        r = programar_siguiente(ahora, WaitStep(amount=2, unit="hours"), VENTANA)
        assert r == _bog(2026, 10, 6, 8, 0)

    def test_sin_horario(self) -> None:
        ahora = _bog(2026, 10, 5, 17, 0)
        espera = WaitStep(amount=2, unit="hours", business_hours_only=False)
        assert programar_siguiente(ahora, espera, VENTANA) == _bog(2026, 10, 5, 19, 0)

    def test_smart_timing_mueve_a_su_hora(self) -> None:
        ahora = _bog(2026, 10, 5, 9, 0)
        espera = WaitStep(amount=1, unit="days", smart_timing=True)
        r = programar_siguiente(ahora, espera, VENTANA, horas_respuesta=[15, 15, 15])
        assert r == _bog(2026, 10, 6, 15, 0)

    def test_smart_timing_nunca_antes_de_la_espera(self) -> None:
        ahora = _bog(2026, 10, 5, 16, 0)
        espera = WaitStep(amount=1, unit="days", smart_timing=True)
        r = programar_siguiente(ahora, espera, VENTANA, horas_respuesta=[9, 9, 9])
        assert r == _bog(2026, 10, 7, 9, 0)

    def test_smart_timing_fuera_de_horario_lo_corrige_el_horario(self) -> None:
        ahora = _bog(2026, 10, 5, 9, 0)
        espera = WaitStep(amount=1, unit="days", smart_timing=True)
        r = programar_siguiente(ahora, espera, VENTANA, horas_respuesta=[22, 22, 22])
        assert r == _bog(2026, 10, 7, 8, 0)

    def test_smart_timing_sin_historia(self) -> None:
        ahora = _bog(2026, 10, 5, 9, 30)
        espera = WaitStep(amount=1, unit="days", smart_timing=True)
        assert programar_siguiente(ahora, espera, VENTANA, [15]) == _bog(2026, 10, 6, 9, 30)


# ─── Avance (sin base: la sesion es un doble) ───────────────────────────────────────────────


def _inscripcion(**kw: Any) -> modelo.LeadSequenceEnrollment:
    base: dict[str, Any] = {
        "id": uuid4(),
        "client_id": uuid4(),
        "lead_id": uuid4(),
        "sequence_id": uuid4(),
        "current_step": 1,
        "status": modelo.ENROLLMENT_ACTIVE,
        "steps_executed": 0,
    }
    return modelo.LeadSequenceEnrollment(**{**base, **kw})


AHORA = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)


class TestRegistrarAvance:
    def test_mensaje_avanza_y_toca_ya(self) -> None:
        ins, sesion = _inscripcion(), MagicMock()
        registrar_avance(
            sesion, ins, Decision(SendMessage(1, MSG, "email"), 2, 3), ahora=AHORA, ventana=None
        )
        assert (ins.current_step, ins.steps_executed) == (2, 3)
        assert ins.next_step_at == AHORA
        assert ins.last_step_at == AHORA
        sesion.add.assert_not_called()

    def test_espera_necesita_instante(self) -> None:
        ins = _inscripcion()
        with pytest.raises(ValueError, match="next_step_at"):
            registrar_avance(
                MagicMock(), ins, Decision(Wait(1, ESPERA), 2, 1), ahora=AHORA, ventana=None
            )
        despues = AHORA + timedelta(days=1)
        ins = _inscripcion()
        registrar_avance(
            MagicMock(),
            ins,
            Decision(Wait(1, ESPERA), 2, 1),
            ahora=AHORA,
            ventana=None,
            next_step_at=despues,
        )
        assert ins.next_step_at == despues

    def test_tarea_que_pausa(self) -> None:
        ins = _inscripcion()
        tarea = TaskStep(title="Llamar", pause_until_done=True)
        registrar_avance(
            MagicMock(), ins, Decision(CreateTask(1, tarea), 2, 1), ahora=AHORA, ventana=None
        )
        assert ins.status == modelo.ENROLLMENT_PAUSED
        assert ins.next_step_at is None
        assert ins.current_step == 2

    def test_completa(self) -> None:
        ins, sesion = _inscripcion(current_step=3, steps_executed=4), MagicMock()
        registrar_avance(sesion, ins, Decision(Complete(), 3, 0), ahora=AHORA, ventana=None)
        assert ins.status == modelo.ENROLLMENT_COMPLETED
        assert ins.completed_at == AHORA
        assert ins.next_step_at is None
        assert sesion.add.call_args.args[0].activity_type == "sequence_completed"

    def test_sale(self) -> None:
        ins, sesion = _inscripcion(), MagicMock()
        registrar_avance(
            sesion, ins, Decision(Exit(modelo.EXIT_CONDITION), 1, 2), ahora=AHORA, ventana=None
        )
        assert ins.status == modelo.ENROLLMENT_EXITED
        assert ins.exit_reason == modelo.EXIT_CONDITION
        assert ins.steps_executed == 2
        actividad = sesion.add.call_args.args[0]
        assert actividad.activity_type == "sequence_exited"
        assert actividad.metadata_["exit_code"] == modelo.EXIT_CONDITION

    def test_no_sale_dos_veces(self) -> None:
        ins = _inscripcion(status=modelo.ENROLLMENT_COMPLETED)
        with pytest.raises(SecuenciaError):
            registrar_avance(
                MagicMock(), ins, Decision(Exit("manual"), 1, 0), ahora=AHORA, ventana=None
            )


class TestAvanceEnHorario:
    def test_el_siguiente_mensaje_espera_a_que_abra_el_negocio(self) -> None:
        ins = _inscripcion()
        noche = _bog(2026, 10, 5, 23, 0)
        registrar_avance(
            MagicMock(), ins, Decision(SendMessage(1, MSG, "email"), 2, 1), ahora=noche,
            ventana=VENTANA,
        )  # fmt: skip
        assert ins.next_step_at == _bog(2026, 10, 6, 8, 0)

    def test_en_horario_toca_ya(self) -> None:
        ins = _inscripcion()
        dia = _bog(2026, 10, 5, 11, 0)
        registrar_avance(
            MagicMock(), ins, Decision(SendMessage(1, MSG, "email"), 2, 1), ahora=dia,
            ventana=VENTANA,
        )  # fmt: skip
        assert ins.next_step_at == dia

    def test_reanudar_de_noche_espera_a_que_abra(self) -> None:
        ins = _inscripcion(status=modelo.ENROLLMENT_PAUSED)
        reanudar(ins, ahora=_bog(2026, 10, 5, 23, 0), ventana=VENTANA)
        assert ins.next_step_at == _bog(2026, 10, 6, 8, 0)


class TestMotivoParaNoInscribir:
    @pytest.mark.parametrize(
        ("campos", "activa", "motivo"),
        [
            ({}, True, None),
            ({}, False, modelo.EXIT_SEQUENCE_DISABLED),
            ({"deleted_at": AHORA}, True, modelo.EXIT_LEAD_DELETED),
            ({"first_name": "[ELIMINADO]"}, True, modelo.EXIT_GDPR),
            ({"status": "won"}, True, modelo.EXIT_LEAD_CLOSED),
        ],
    )
    def test_misma_regla_que_el_motor(
        self, campos: dict[str, Any], activa: bool, motivo: str | None
    ) -> None:
        lead = Lead(client_id=uuid4(), **{"status": "active", "first_name": "Ana", **campos})
        secuencia = modelo.LeadSequence(client_id=lead.client_id, name="x", is_active=activa)
        assert motivo_para_no_inscribir(lead, secuencia) == motivo


class TestPausa:
    def test_pausar_y_reanudar(self) -> None:
        ins = _inscripcion(next_step_at=AHORA)
        pausar(ins)
        assert (ins.status, ins.next_step_at) == (modelo.ENROLLMENT_PAUSED, None)
        with pytest.raises(SecuenciaError):
            pausar(ins)
        reanudar(ins, ahora=AHORA, ventana=None)
        assert (ins.status, ins.next_step_at) == (modelo.ENROLLMENT_ACTIVE, AHORA)
        with pytest.raises(SecuenciaError):
            reanudar(ins, ahora=AHORA, ventana=None)


# ─── Plantillas predefinidas y coherencia ───────────────────────────────────────────────────


class TestPlantillasPredefinidas:
    def test_son_tres_y_validas(self) -> None:
        assert set(PLANTILLAS) == {"inbound_nurturing", "outbound_cold", "reengagement"}
        for definicion in PLANTILLAS.values():
            SequenceDefinition.model_validate(definicion.model_dump())

    def test_solo_entrantes_se_dispara_sola(self) -> None:
        automaticas = {k for k, v in PLANTILLAS.items() if v.trigger_conditions.automatica}
        assert automaticas == {"inbound_nurturing"}

    def test_salen_si_el_lead_responde(self) -> None:
        pasos = PLANTILLAS["inbound_nurturing"].steps
        lead = LeadSnapshot(channels=frozenset({"whatsapp"}), replied=True)
        d = decidir(
            pasos=pasos, current_step=3, steps_executed=2, lead=lead, channel_priority=PRIORIDAD
        )
        assert d.action == Exit(modelo.EXIT_CONDITION)

    def test_solo_variables_que_nunca_faltan(self) -> None:
        for definicion in PLANTILLAS.values():
            for paso in definicion.steps:
                if isinstance(paso, MessageStep):
                    usadas = variables_de_plantilla(paso.body or "")
                    usadas |= variables_de_plantilla(paso.subject or "")
                    assert usadas <= {"first_name", "business_name"}, usadas

    def test_se_leen_bien_sin_datos_del_lead(self) -> None:
        for definicion in PLANTILLAS.values():
            for paso in definicion.steps:
                if isinstance(paso, MessageStep):
                    texto = renderizar_plantilla(paso.body or "", {"business_name": "Sol"})
                    assert " ," not in texto
                    assert "  " not in texto
                    assert not texto.startswith(",")

    def test_nombres_unicos(self) -> None:
        nombres = [d.name for d in PLANTILLAS.values()]
        assert len(set(nombres)) == len(nombres)


def _migracion() -> Any:
    ruta = Path(__file__).parents[2] / "migrations" / "versions" / "028_lead_sequences.py"
    spec = importlib.util.spec_from_file_location("m028", ruta)
    assert spec is not None
    assert spec.loader is not None
    migracion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migracion)
    return migracion


def test_tipos_y_estados_iguales_en_modelo_y_migracion() -> None:
    migracion = _migracion()
    assert modelo.STEP_TYPES == migracion.STEP_TYPES
    assert modelo.ENROLLMENT_STATUSES == migracion.ENROLLMENT_STATUSES


def test_tipos_de_paso_del_schema_iguales_que_el_modelo() -> None:
    tipos = {
        cls.model_fields["type"].default for cls in (MessageStep, WaitStep, ConditionStep, TaskStep)
    }
    assert tipos == set(modelo.STEP_TYPES)


# ─── Correcciones de la segunda revision ────────────────────────────────────────────────────


class TestCorreccionesSegundaRevision:
    def test_un_bucle_que_escribe_necesita_esperar_al_menos_un_dia(self) -> None:
        def bucle(espera: WaitStep) -> list[Any]:
            return [
                MessageStep(body="Hola"),
                espera,
                ConditionStep(check="no_reply", if_true=Branch(action="goto", goto_position=1)),
            ]

        with pytest.raises(ValueError, match="al menos 1 dia"):
            validar_grafo(bucle(WaitStep(amount=1, unit="minutes", business_hours_only=False)))
        with pytest.raises(ValueError, match="al menos 1 dia"):
            validar_grafo(bucle(WaitStep(amount=23, unit="hours")))
        validar_grafo(bucle(WaitStep(amount=1, unit="days")))
        # Una espera corta fuera de un bucle sigue permitida.
        validar_grafo([MessageStep(body="Hola"), WaitStep(amount=5, unit="minutes")])

    def test_un_bucle_corto_sin_mensajes_ni_tareas_esta_permitido(self) -> None:
        validar_grafo(
            [
                WaitStep(amount=1, unit="hours"),
                ConditionStep(
                    check="replied",
                    if_true=Branch(action="continue"),
                    if_false=Branch(action="goto", goto_position=1),
                ),
                TaskStep(title="Llamar"),
            ]
        )

    def test_quien_se_dio_de_baja_no_recibe_nada_de_ninguna_secuencia(self) -> None:
        decision = decidir(
            pasos=[MessageStep(body="Hola")],
            current_step=1,
            steps_executed=0,
            lead=LeadSnapshot(channels=frozenset({"email"}), opted_out=True),
            channel_priority=["email"],
        )
        assert decision.action == Exit(modelo.EXIT_UNSUBSCRIBED)

    def test_las_plantillas_prefieren_el_email_y_no_dan_por_hecho_un_correo(self) -> None:
        for clave in ("inbound_nurturing", "reengagement"):
            assert PLANTILLAS[clave].channel_priority[0] == "email"
        textos = [
            p.body or ""
            for plantilla in PLANTILLAS.values()
            for p in plantilla.steps
            if isinstance(p, MessageStep)
        ]
        assert not any("por correo" in t for t in textos)

    def test_la_migracion_exige_next_step_at_en_las_activas(self) -> None:
        ruta = Path(__file__).parents[2] / "migrations" / "versions" / "028_lead_sequences.py"
        assert "ck_lead_enrollment_due" in ruta.read_text(encoding="utf-8")
        nombres = {c.name for c in modelo.LeadSequenceEnrollment.__table__.constraints if c.name}
        assert "ck_lead_enrollment_due" in nombres
