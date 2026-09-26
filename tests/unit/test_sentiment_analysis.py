"""Tests del nodo de analisis de sentimiento (Sprint 10, Dev B)."""

from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.agents.nodes import sentiment as nodo
from app.agents.nodes._tenant import AgentSettings
from app.schemas.sentiment import SentimentLevel, SentimentResult
from tests.unit.agent_doubles import (
    FakeChatModel,
    FakeSession,
    RespuestaLLM,
    estado,
    parchear_agent_settings,
    parchear_chat_model,
    parchear_tenant_session,
)

CON_SENTIMIENTO = AgentSettings(enabled_agents=("rag", "sentiment"))


def _resultado(nivel: SentimentLevel, score: float = 0.9) -> dict[str, Any]:
    """Salida de `with_structured_output(..., include_raw=True)`."""
    return {
        "parsed": SentimentResult(sentiment=nivel, score=score, reasoning="prueba"),
        "raw": RespuestaLLM(input_tokens=40, output_tokens=12),
    }


@pytest.fixture
def consumo(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Registra lo que el nodo anota en el presupuesto de tokens."""
    registrado: list[dict[str, Any]] = []

    async def _record(**kwargs: Any) -> None:
        registrado.append(kwargs)

    monkeypatch.setattr(nodo.TokenBudgetGuard, "record_usage", _record)
    return registrado


@pytest.fixture
def sesion(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    """Sesion falsa: historial vacio y registro del UPDATE del mensaje."""
    return parchear_tenant_session(monkeypatch, nodo, FakeSession())


def _preparar(
    monkeypatch: pytest.MonkeyPatch, nivel: SentimentLevel, ajustes: AgentSettings = CON_SENTIMIENTO
) -> FakeChatModel:
    modelo = FakeChatModel(_resultado(nivel))
    parchear_chat_model(monkeypatch, nodo, modelo)
    parchear_agent_settings(monkeypatch, nodo, ajustes)
    return modelo


class TestClasificacion:
    async def test_positivo_no_afecta_el_flujo(
        self, monkeypatch: pytest.MonkeyPatch, sesion: FakeSession, consumo: list
    ) -> None:
        """Criterio 7: positivo/neutral no escalan."""
        _preparar(monkeypatch, SentimentLevel.POSITIVE)

        salida = await nodo.sentiment_analysis_node(
            estado(message={"text": "Muchas gracias!", "external_message_id": "m-1"})
        )

        assert salida["current_sentiment"] == "positive"
        assert salida["consecutive_very_negative"] == 0
        assert "requires_handoff" not in salida

    async def test_usa_el_modelo_barato_con_structured_output(
        self, monkeypatch: pytest.MonkeyPatch, sesion: FakeSession, consumo: list
    ) -> None:
        modelo = _preparar(monkeypatch, SentimentLevel.NEUTRAL)
        pedidos = parchear_chat_model(monkeypatch, nodo, modelo)

        await nodo.sentiment_analysis_node(estado(message={"text": "Hola"}))

        assert pedidos[0] == (nodo.get_settings().OPENAI_FALLBACK_MODEL, 0.0)
        assert modelo.structured_con == (SentimentResult, True)

    async def test_registra_el_consumo_en_el_presupuesto(
        self, monkeypatch: pytest.MonkeyPatch, sesion: FakeSession, consumo: list
    ) -> None:
        _preparar(monkeypatch, SentimentLevel.NEUTRAL)

        await nodo.sentiment_analysis_node(estado(message={"text": "Hola"}))

        assert consumo[0]["operation"] == "sentiment"
        assert (consumo[0]["prompt_tokens"], consumo[0]["completion_tokens"]) == (40, 12)


class TestEscalamiento:
    async def test_un_solo_muy_negativo_no_escala(
        self, monkeypatch: pytest.MonkeyPatch, sesion: FakeSession, consumo: list
    ) -> None:
        _preparar(monkeypatch, SentimentLevel.VERY_NEGATIVE)

        salida = await nodo.sentiment_analysis_node(
            estado(message={"text": "Esto es inaceptable!"}, consecutive_very_negative=0)
        )

        assert salida["consecutive_very_negative"] == 1
        assert "requires_handoff" not in salida

    async def test_dos_muy_negativos_seguidos_escalan(
        self, monkeypatch: pytest.MonkeyPatch, sesion: FakeSession, consumo: list
    ) -> None:
        """Criterio 8, con el contador que el checkpointer trae del turno anterior."""
        _preparar(monkeypatch, SentimentLevel.VERY_NEGATIVE)

        salida = await nodo.sentiment_analysis_node(
            estado(message={"text": "Son unos incompetentes!"}, consecutive_very_negative=1)
        )

        assert salida["requires_handoff"] is True
        assert salida["handoff_reason"] == "negative_sentiment"
        # Se reinicia al escalar: cuando vuelva al bot no escala al primer enojo.
        assert salida["consecutive_very_negative"] == 0

    @pytest.mark.parametrize(
        "nivel", [SentimentLevel.NEUTRAL, SentimentLevel.NEGATIVE, SentimentLevel.POSITIVE]
    )
    async def test_cualquier_otro_nivel_reinicia_el_contador(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sesion: FakeSession,
        consumo: list,
        nivel: SentimentLevel,
    ) -> None:
        _preparar(monkeypatch, nivel)

        salida = await nodo.sentiment_analysis_node(
            estado(message={"text": "Bueno, ok"}, consecutive_very_negative=1)
        )

        assert salida["consecutive_very_negative"] == 0
        assert "requires_handoff" not in salida


class TestCuandoNoAplica:
    async def test_tenant_sin_el_agente_no_llama_al_llm(
        self, monkeypatch: pytest.MonkeyPatch, sesion: FakeSession, consumo: list
    ) -> None:
        """CLAUDE.md, restriccion 4: no todos los tenants quieren todos los agentes."""
        modelo = _preparar(
            monkeypatch, SentimentLevel.VERY_NEGATIVE, AgentSettings(enabled_agents=("rag",))
        )

        assert await nodo.sentiment_analysis_node(estado()) == {}
        assert modelo.llamadas == []

    async def test_mensaje_sin_texto(
        self, monkeypatch: pytest.MonkeyPatch, sesion: FakeSession, consumo: list
    ) -> None:
        modelo = _preparar(monkeypatch, SentimentLevel.VERY_NEGATIVE)

        assert await nodo.sentiment_analysis_node(estado(message={"media_url": "x"})) == {}
        assert modelo.llamadas == []

    async def test_un_fallo_del_llm_no_tumba_el_turno(
        self, monkeypatch: pytest.MonkeyPatch, sesion: FakeSession, consumo: list
    ) -> None:
        """Es una senal secundaria: sin ella el mensaje sigue su camino."""

        class _Caido(FakeChatModel):
            async def ainvoke(self, messages: Any) -> Any:
                raise RuntimeError("OpenAI 503")

        parchear_chat_model(monkeypatch, nodo, _Caido(None))
        parchear_agent_settings(monkeypatch, nodo, CON_SENTIMIENTO)

        assert await nodo.sentiment_analysis_node(estado(consecutive_very_negative=1)) == {}

    async def test_respuesta_no_parseable(
        self, monkeypatch: pytest.MonkeyPatch, sesion: FakeSession, consumo: list
    ) -> None:
        parchear_chat_model(monkeypatch, nodo, FakeChatModel({"parsed": None, "raw": None}))
        parchear_agent_settings(monkeypatch, nodo, CON_SENTIMIENTO)

        assert await nodo.sentiment_analysis_node(estado()) == {}


class TestPersistencia:
    async def test_guarda_el_sentimiento_en_la_metadata_del_mensaje(
        self, monkeypatch: pytest.MonkeyPatch, sesion: FakeSession, consumo: list
    ) -> None:
        """Criterio 10: `sentiment` en `messages.metadata`, sin read-modify-write."""
        _preparar(monkeypatch, SentimentLevel.NEGATIVE)

        await nodo.sentiment_analysis_node(
            estado(message={"text": "No me gusto", "external_message_id": "wamid.1"})
        )

        update = sesion.executed[-1]
        sql = str(update.compile(dialect=postgresql.dialect()))
        assert sql.startswith("UPDATE messages")
        assert "messages.metadata ||" in sql
        assert "messages.direction = " in sql
        assert "messages.external_message_id = " in sql

    async def test_sin_external_id_no_intenta_guardar(
        self, monkeypatch: pytest.MonkeyPatch, sesion: FakeSession, consumo: list
    ) -> None:
        _preparar(monkeypatch, SentimentLevel.NEGATIVE)

        salida = await nodo.sentiment_analysis_node(estado(message={"text": "No me gusto"}))

        assert salida["current_sentiment"] == "negative"
        # Solo la consulta del contexto, ningun UPDATE.
        assert len(sesion.executed) == 1
