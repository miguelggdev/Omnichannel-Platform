"""Tests del nodo `language_detect` (Sprint 14b, ADR-077)."""

from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.nodes import language_detect as nodo
from app.agents.nodes.language_detect import (
    IdiomaDetectado,
    detect_with_llm,
    language_detect_node,
)
from tests.unit.agent_doubles import (
    FakeChatModel,
    FakeSession,
    estado,
    parchear_chat_model,
    parchear_tenant_session,
)
from tests.unit.test_i18n import FRASES


def _conversacion(idioma: str | None = None, clinica: bool = False) -> SimpleNamespace:
    """La fila que lee `_leer_conversacion`."""
    return SimpleNamespace(idioma=idioma, clinica=clinica)


def _preparar(
    monkeypatch: pytest.MonkeyPatch,
    *,
    conversacion: SimpleNamespace | None,
    tenant: dict[str, Any] | None = None,
    llm: str | Exception | None = None,
    gana: bool = True,
    guardado: SimpleNamespace | None = None,
) -> tuple[FakeSession, list[str]]:
    """Sesion falsa con lo que devuelven, en orden, las consultas del nodo.

    Orden: conversacion, ajustes del tenant, el UPDATE (`RETURNING`: algo si este
    llamante gano, `None` si otro mensaje se le adelanto) y, si perdio, la
    relectura de lo que quedo guardado.
    """
    sesion = parchear_tenant_session(
        monkeypatch,
        nodo,
        FakeSession(resultados=[conversacion, tenant or {}, "ganador" if gana else None, guardado]),
    )
    consultas_al_llm: list[str] = []

    async def _llm(state: Any, texto: str) -> str | None:
        consultas_al_llm.append(texto)
        if isinstance(llm, Exception):
            raise llm
        return llm

    monkeypatch.setattr(nodo, "detect_with_llm", _llm)
    return sesion, consultas_al_llm


def _con(texto: str | None) -> dict[str, Any]:
    return estado(message={"text": texto})


class TestDeteccion:
    @pytest.mark.parametrize(("idioma", "frase"), list(FRASES.items()))
    async def test_detecta_el_idioma_sin_llamar_al_llm_y_lo_guarda(
        self, monkeypatch: pytest.MonkeyPatch, idioma: str, frase: str
    ) -> None:
        sesion, al_llm = _preparar(monkeypatch, conversacion=_conversacion())

        resultado = await language_detect_node(_con(frase))  # type: ignore[arg-type]

        assert resultado == {"detected_language": idioma}
        assert al_llm == []
        # leer la conversacion, leer el tenant y guardar el idioma
        assert len(sesion.executed) == 3

    async def test_el_idioma_ya_guardado_no_se_vuelve_a_detectar(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Consistencia: no cambia de un mensaje a otro dentro de la conversacion."""
        sesion, al_llm = _preparar(monkeypatch, conversacion=_conversacion(idioma="pt"))

        resultado = await language_detect_node(_con(FRASES["en"]))  # type: ignore[arg-type]

        assert resultado == {"detected_language": "pt"}
        assert al_llm == []
        assert len(sesion.executed) == 1

    async def test_un_texto_corto_pregunta_al_llm_y_guarda_lo_que_responda(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sesion, al_llm = _preparar(monkeypatch, conversacion=_conversacion(), llm="fr")

        resultado = await language_detect_node(_con("merci beaucoup"))  # type: ignore[arg-type]

        assert resultado == {"detected_language": "fr"}
        assert al_llm == ["merci beaucoup"]
        assert len(sesion.executed) == 3

    async def test_si_nadie_decide_se_usa_el_del_tenant_y_no_se_guarda_nada(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un 'ok' no debe fijar el idioma de toda la conversacion."""
        sesion, _ = _preparar(
            monkeypatch,
            conversacion=_conversacion(),
            tenant={"default_language": "pt"},
            llm=None,
        )

        resultado = await language_detect_node(_con("ok"))  # type: ignore[arg-type]

        assert resultado == {"detected_language": "pt"}
        assert len(sesion.executed) == 2  # sin el UPDATE

    async def test_el_idioma_del_tenant_invalido_o_ausente_es_espanol(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _preparar(monkeypatch, conversacion=_conversacion(), tenant={"default_language": "nl"})

        assert await language_detect_node(_con("ok")) == {"detected_language": "es"}  # type: ignore[arg-type]

    async def test_sin_texto_usa_el_del_tenant_y_no_guarda(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un audio sin transcripcion o una imagen no dicen nada del idioma."""
        sesion, al_llm = _preparar(
            monkeypatch, conversacion=_conversacion(), tenant={"default_language": "it"}
        )

        resultado = await language_detect_node(_con(None))  # type: ignore[arg-type]

        assert resultado == {"detected_language": "it"}
        assert al_llm == []
        assert len(sesion.executed) == 2

    async def test_una_conversacion_clinica_nunca_llega_al_llm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lo dictado es dato de salud: ADR-072 lo mantiene fuera de otros modelos."""
        sesion, al_llm = _preparar(monkeypatch, conversacion=_conversacion(clinica=True), llm="en")

        resultado = await language_detect_node(_con("ok gracias"))  # type: ignore[arg-type]

        assert resultado == {"detected_language": "es"}
        assert al_llm == []
        assert len(sesion.executed) == 2

    async def test_una_conversacion_clinica_si_usa_langdetect_que_es_local(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, al_llm = _preparar(monkeypatch, conversacion=_conversacion(clinica=True))

        resultado = await language_detect_node(_con(FRASES["de"]))  # type: ignore[arg-type]

        assert resultado == {"detected_language": "de"}
        assert al_llm == []


class TestMensajesSimultaneos:
    async def test_el_que_pierde_la_carrera_responde_en_el_idioma_que_quedo_guardado(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dos mensajes a la vez en idiomas distintos: la conversacion no se parte en dos."""
        sesion, _ = _preparar(
            monkeypatch,
            conversacion=_conversacion(),
            gana=False,
            guardado=_conversacion(idioma="en"),
        )

        resultado = await language_detect_node(_con(FRASES["es"]))  # type: ignore[arg-type]

        assert resultado == {"detected_language": "en"}
        assert len(sesion.executed) == 4

    async def test_si_perdio_y_no_hay_nada_guardado_usa_lo_que_detecto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _preparar(monkeypatch, conversacion=_conversacion(), gana=False, guardado=None)

        resultado = await language_detect_node(_con(FRASES["fr"]))  # type: ignore[arg-type]

        assert resultado == {"detected_language": "fr"}


class TestResistenciaAFallos:
    async def test_si_el_llm_falla_se_usa_el_del_tenant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sesion, _ = _preparar(
            monkeypatch,
            conversacion=_conversacion(),
            tenant={"default_language": "de"},
            llm=RuntimeError("openai caido"),
        )

        resultado = await language_detect_node(_con("ok"))  # type: ignore[arg-type]

        assert resultado == {"detected_language": "de"}
        assert len(sesion.executed) == 2

    async def test_si_la_base_falla_el_nodo_no_rompe_la_conversacion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _revienta(*_: Any, **__: Any) -> None:
            raise ConnectionError("base caida")

        monkeypatch.setattr(nodo, "tenant_session", _revienta)

        resultado = await language_detect_node(_con(FRASES["en"]))  # type: ignore[arg-type]

        assert resultado == {"detected_language": "es"}


class TestFallbackConLlm:
    async def test_usa_el_modelo_barato_y_registra_el_consumo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crudo = SimpleNamespace(usage_metadata={"input_tokens": 30, "output_tokens": 2})
        modelo = FakeChatModel({"parsed": IdiomaDetectado(language="it"), "raw": crudo})
        pedidos = parchear_chat_model(monkeypatch, nodo, modelo)
        usos: list[dict[str, Any]] = []

        async def _registrar(**kwargs: Any) -> None:
            usos.append(kwargs)

        monkeypatch.setattr(nodo.TokenBudgetGuard, "record_usage", _registrar)
        state = estado()

        idioma = await detect_with_llm(state, "va bene")  # type: ignore[arg-type]

        assert idioma == "it"
        assert pedidos == [(nodo.get_settings().OPENAI_FALLBACK_MODEL, 0.0)]
        assert modelo.structured_con == (IdiomaDetectado, True)
        assert usos[0]["operation"] == nodo.OPERATION
        assert (usos[0]["prompt_tokens"], usos[0]["completion_tokens"]) == (30, 2)
        assert usos[0]["client_id"] == state["client_id"]

    async def test_unknown_es_no_saber(self, monkeypatch: pytest.MonkeyPatch) -> None:
        parchear_chat_model(
            monkeypatch, nodo, FakeChatModel({"parsed": IdiomaDetectado(language="unknown")})
        )

        assert await detect_with_llm(estado(), "???") is None  # type: ignore[arg-type]

    async def test_una_salida_que_no_se_pudo_parsear_es_no_saber(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parchear_chat_model(monkeypatch, nodo, FakeChatModel({"parsed": None}))

        assert await detect_with_llm(estado(), "???") is None  # type: ignore[arg-type]

    def test_el_prompt_lista_los_seis_idiomas(self) -> None:
        for codigo in ("es", "en", "pt", "it", "de", "fr"):
            assert codigo in nodo.LLM_PROMPT


def test_el_update_solo_escribe_si_la_conversacion_no_tenia_idioma() -> None:
    """Dos mensajes simultaneos no se pisan: el `IS NULL` hace que gane el primero."""
    assert "IS NULL" in str(nodo._GUARDAR_IDIOMA)
