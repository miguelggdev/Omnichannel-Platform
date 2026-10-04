"""Tests del servicio de internacionalizacion (Sprint 14b, ADR-077)."""

import re
import uuid

import pytest

from app.agents.nodes import human_handoff as handoff
from app.agents.nodes import respond
from app.services.i18n import (
    DEFAULT_LANGUAGE,
    LANGUAGE_NAMES,
    MIN_CHARS_LANGDETECT,
    SUPPORTED_LANGUAGES,
    SYSTEM_MESSAGES,
    con_idioma,
    detectar_idioma,
    get_system_message,
    instruccion_de_idioma,
    normalizar_idioma,
)
from tests.unit.agent_doubles import FakeSession, parchear_agent_settings, parchear_tenant_session

#: Una frase por idioma, de las que el spec pide detectar.
FRASES = {
    "es": "Hola, quiero agendar una cita para mañana por la tarde",
    "en": "Hello, I would like to book an appointment for tomorrow",
    "pt": "Olá, gostaria de marcar uma consulta para amanhã",
    "it": "Buongiorno, vorrei prenotare un appuntamento per domani",
    "de": "Guten Tag, ich möchte einen Termin für morgen buchen",
    "fr": "Bonjour, je voudrais prendre rendez-vous pour demain",
}


class TestNormalizar:
    @pytest.mark.parametrize(
        ("valor", "esperado"),
        [
            ("es", "es"),
            ("EN", "en"),
            (" pt-BR ", "pt"),
            ("fr_CA", "fr"),
            ("de", "de"),
            ("nl", None),
            ("", None),
            (None, None),
            (5, None),
        ],
    )
    def test_reduce_a_un_idioma_soportado(self, valor: object, esperado: str | None) -> None:
        assert normalizar_idioma(valor) == esperado


class TestDetectar:
    @pytest.mark.parametrize(("idioma", "frase"), list(FRASES.items()))
    def test_detecta_los_seis_idiomas(self, idioma: str, frase: str) -> None:
        assert detectar_idioma(frase) == idioma

    @pytest.mark.parametrize("texto", ["ok perfecto", "merci beaucoup", "d'accord", "alles klar"])
    def test_un_texto_corto_no_se_decide_aqui(self, texto: str) -> None:
        """`langdetect` falla con estas frases (en, ro, it, et): se pasa al fallback."""
        assert len(texto) < MIN_CHARS_LANGDETECT
        assert detectar_idioma(texto) is None

    def test_un_idioma_no_soportado_no_se_decide(self) -> None:
        assert detectar_idioma("Goedemorgen, ik wil graag een afspraak maken voor morgen") is None

    @pytest.mark.parametrize("texto", [None, "", "   ", "12345 67890 12345 67890 123"])
    def test_sin_texto_utilizable(self, texto: str | None) -> None:
        assert detectar_idioma(texto) is None

    def test_es_determinista(self) -> None:
        """Sin semilla fija, `langdetect` daria resultados distintos entre ejecuciones."""
        resultados = {detectar_idioma(FRASES["pt"]) for _ in range(20)}

        assert resultados == {"pt"}


class TestMensajes:
    def test_hay_seis_idiomas_y_el_espanol_es_el_de_referencia(self) -> None:
        assert set(SYSTEM_MESSAGES) == SUPPORTED_LANGUAGES
        assert set(LANGUAGE_NAMES) == SUPPORTED_LANGUAGES
        assert DEFAULT_LANGUAGE == "es"

    @pytest.mark.parametrize("idioma", sorted(SUPPORTED_LANGUAGES - {"es"}))
    def test_ningun_idioma_le_falta_una_clave_al_espanol(self, idioma: str) -> None:
        assert set(SYSTEM_MESSAGES[idioma]) == set(SYSTEM_MESSAGES["es"])

    @pytest.mark.parametrize("idioma", sorted(SUPPORTED_LANGUAGES))
    def test_las_variables_de_cada_plantilla_son_las_mismas_en_todos_los_idiomas(
        self, idioma: str
    ) -> None:
        """Una traduccion que pierde `{hours}` o inventa `{nombre}` rompe el envio."""
        for clave, plantilla in SYSTEM_MESSAGES[idioma].items():
            esperadas = set(re.findall(r"{(\w+)}", SYSTEM_MESSAGES["es"][clave]))
            assert set(re.findall(r"{(\w+)}", plantilla)) == esperadas, (idioma, clave)

    @pytest.mark.parametrize("idioma", sorted(SUPPORTED_LANGUAGES))
    def test_ninguna_traduccion_esta_vacia(self, idioma: str) -> None:
        assert all(texto.strip() for texto in SYSTEM_MESSAGES[idioma].values())

    def test_interpola_las_variables(self) -> None:
        assert "Clinica Sol" in get_system_message("welcome", "en", business_name="Clinica Sol")
        assert "9-18" in get_system_message("out_of_hours", "fr", hours="9-18")

    def test_un_idioma_no_soportado_o_ausente_cae_al_espanol(self) -> None:
        esperado = SYSTEM_MESSAGES["es"]["handoff_complaint"]

        assert get_system_message("handoff_complaint", "nl") == esperado
        assert get_system_message("handoff_complaint", None) == esperado

    def test_una_clave_inexistente_devuelve_la_clave(self) -> None:
        assert get_system_message("no_existe", "en") == "no_existe"

    def test_la_variable_que_falta_falla_a_proposito(self) -> None:
        with pytest.raises(KeyError):
            get_system_message("welcome", "es", otra="x")

    def test_cada_idioma_dice_lo_suyo(self) -> None:
        textos = {get_system_message("farewell", i) for i in SUPPORTED_LANGUAGES}

        assert len(textos) == 6


class TestInstruccion:
    def test_nombra_el_idioma(self) -> None:
        assert instruccion_de_idioma("en") == "Responde siempre en ingles (en)."
        assert instruccion_de_idioma("pt-BR") == "Responde siempre en portugues (pt)."

    @pytest.mark.parametrize("idioma", [None, "", "nl", "xx"])
    def test_sin_idioma_valido_no_hay_instruccion(self, idioma: str | None) -> None:
        assert instruccion_de_idioma(idioma) == ""

    def test_con_idioma_la_agrega_al_final_del_prompt(self) -> None:
        assert con_idioma("Eres un asistente.", "de") == (
            "Eres un asistente.\n\nResponde siempre en aleman (de)."
        )

    def test_sin_prompt_devuelve_solo_la_instruccion(self) -> None:
        assert con_idioma(None, "fr") == "Responde siempre en frances (fr)."

    def test_sin_idioma_deja_el_prompt_intacto(self) -> None:
        assert con_idioma("Eres un asistente.", None) == "Eres un asistente."
        assert con_idioma(None, None) == ""


class TestHandoff:
    def test_los_textos_en_espanol_siguen_siendo_los_de_siempre(self) -> None:
        """No hay regresion: el espanol que ya recibian los contactos no cambia."""
        assert handoff.HANDOFF_MESSAGES["human_request"] == (
            "Entendido, te transfiero con un agente humano ahora mismo."
        )
        assert set(handoff.HANDOFF_MESSAGES) == set(handoff.HANDOFF_REASONS)

    def test_el_mensaje_del_tenant_manda_sobre_cualquier_idioma(self) -> None:
        assert handoff._handoff_text("complaint", "Un momento por favor", "en") == (
            "Un momento por favor"
        )

    @pytest.mark.parametrize("motivo", handoff.HANDOFF_REASONS)
    def test_cada_motivo_sale_en_el_idioma_del_contacto(self, motivo: str) -> None:
        for idioma in sorted(SUPPORTED_LANGUAGES - {"es"}):
            assert handoff._handoff_text(motivo, None, idioma) == get_system_message(
                f"handoff_{motivo}", idioma
            )
            assert handoff._handoff_text(motivo, None, idioma) != handoff.HANDOFF_MESSAGES[motivo]

    def test_sin_idioma_es_espanol(self) -> None:
        assert handoff._handoff_text("complaint", None) == handoff.HANDOFF_MESSAGES["complaint"]
        assert (
            handoff._handoff_text("complaint", None, "es")
            == (handoff.HANDOFF_MESSAGES["complaint"])
        )

    def test_un_motivo_desconocido_cae_al_de_rescate(self) -> None:
        assert handoff._handoff_text("inventado", None, "en") == get_system_message(
            "handoff_insufficient_context", "en"
        )


class TestRespond:
    @staticmethod
    def _ajustes(monkeypatch: pytest.MonkeyPatch, welcome: str | None = None) -> None:
        from app.agents.nodes._tenant import AgentSettings

        parchear_agent_settings(
            monkeypatch, respond, AgentSettings(model="m", welcome_message=welcome)
        )

    @pytest.mark.parametrize("idioma", [None, "es"])
    async def test_el_espanol_conserva_los_textos_de_siempre(
        self, monkeypatch: pytest.MonkeyPatch, idioma: str | None
    ) -> None:
        self._ajustes(monkeypatch)
        cliente = uuid.uuid4()

        assert await respond._texto_por_intent(cliente, "greeting", idioma) == (
            respond.DEFAULT_GREETING
        )
        assert await respond._texto_por_intent(cliente, "farewell", idioma) == (
            respond.DEFAULT_FAREWELL
        )
        assert await respond._texto_por_intent(cliente, "otro", idioma) == (
            respond.DEFAULT_FALLBACK
        )

    async def test_otro_idioma_sale_del_servicio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._ajustes(monkeypatch)
        parchear_tenant_session(monkeypatch, respond, FakeSession(resultados=["Clinica Sol"]))
        cliente = uuid.uuid4()

        saludo = await respond._texto_por_intent(cliente, "greeting", "en")

        assert saludo == get_system_message("welcome", "en", business_name="Clinica Sol")
        assert await respond._texto_por_intent(cliente, "farewell", "fr") == (
            get_system_message("farewell", "fr")
        )
        assert await respond._texto_por_intent(cliente, "otro", "de") == (
            get_system_message("fallback", "de")
        )

    async def test_el_saludo_que_configuro_el_tenant_manda(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._ajustes(monkeypatch, welcome="Bienvenido a mi clinica")

        assert await respond._texto_por_intent(uuid.uuid4(), "greeting", "en") == (
            "Bienvenido a mi clinica"
        )
