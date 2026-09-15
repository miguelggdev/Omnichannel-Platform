"""Tests del nodo de RAG con grounding estricto (Sprint 6).

Contrato: `specs/sprint-06-langgraph.md` §5. `RAGService` y el LLM se sustituyen
por dobles: el retrieval real (SQL, pgvector, filtro pre-vectorial) ya esta
cubierto por `tests/unit/test_rag.py` y `tests/integration/test_document_pipeline.py`.

Lo que se verifica aqui es la regla del sprint: **nunca responder sin contexto**.
"""

import uuid
from typing import Any

import pytest

from app.agents.nodes import rag_query as nodo
from app.agents.nodes._tenant import AgentSettings
from app.services.rag import RetrievalResult
from tests.unit.agent_doubles import (
    FakeChatModel,
    RespuestaLLM,
    estado,
    parchear_agent_settings,
    parchear_chat_model,
)


def _chunk(similarity: float, content: str = "Atendemos de 8 a 18.") -> RetrievalResult:
    """Crea un chunk recuperado.

    Args:
        similarity: Similaridad del chunk con la consulta.
        content: Texto del chunk.

    Returns:
        `RetrievalResult` listo para el nodo.
    """
    return RetrievalResult(
        chunk_id=uuid.uuid4(),
        content=content,
        similarity=similarity,
        metadata={"page_number": 1},
        citation="[Fuente: FAQ, pag. 1]",
    )


class FakeRAGService:
    """`RAGService` que devuelve chunks y ejemplos prefijados.

    Attributes:
        chunks: Lo que devuelve `retrieve()`.
        few_shot: Lo que devuelve `retrieve_few_shot_examples()`.
        retrieve_args: Argumentos con los que se llamo a `retrieve()`.
        few_shot_args: Argumentos con los que se pidieron los ejemplos.
        prompt_args: Argumentos con los que se armo el prompt.
    """

    def __init__(
        self,
        chunks: list[RetrievalResult] | None = None,
        few_shot: list[dict[str, Any]] | None = None,
    ) -> None:
        """Prepara el doble.

        Args:
            chunks: Chunks que devolvera el retrieval.
            few_shot: Ejemplos aprobados que devolvera.
        """
        self.chunks = chunks or []
        self.few_shot = few_shot or []
        self.retrieve_args: dict[str, Any] = {}
        self.few_shot_args: dict[str, Any] = {}
        self.prompt_args: dict[str, Any] = {}

    async def retrieve(self, **kwargs: Any) -> list[RetrievalResult]:
        """Registra la llamada y devuelve los chunks prefijados."""
        self.retrieve_args = kwargs
        return self.chunks

    async def retrieve_few_shot_examples(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Registra la llamada y devuelve los ejemplos prefijados."""
        self.few_shot_args = kwargs
        return self.few_shot

    def build_grounded_prompt(self, **kwargs: Any) -> str:
        """Registra la llamada y devuelve un prompt reconocible."""
        self.prompt_args = kwargs
        return "PROMPT CON CONTEXTO"


def _preparar(
    monkeypatch: pytest.MonkeyPatch,
    servicio: FakeRAGService,
    respuesta: Any = None,
    settings: AgentSettings | None = None,
) -> tuple[FakeChatModel, list[dict[str, Any]], list[tuple[str, float]]]:
    """Deja el nodo listo con RAG y LLM falsos.

    Args:
        monkeypatch: Fixture de pytest.
        servicio: Doble de `RAGService`.
        respuesta: Respuesta del LLM; por defecto, una con texto y consumo.
        settings: Configuracion del tenant.

    Returns:
        Tupla `(modelo_falso, registros_de_consumo, modelos_pedidos)`.
    """
    monkeypatch.setattr(nodo, "build_rag_service", lambda: servicio)
    modelo = FakeChatModel(
        respuesta or RespuestaLLM("Atendemos de 8 a 18. [Fuente: FAQ, pag. 1]", 200, 40)
    )
    pedidos = parchear_chat_model(monkeypatch, nodo, modelo)
    parchear_agent_settings(monkeypatch, nodo, settings or AgentSettings(model="gpt-4o"))

    registros: list[dict[str, Any]] = []

    async def _record(**kwargs: Any) -> None:
        registros.append(kwargs)

    monkeypatch.setattr(nodo.TokenBudgetGuard, "record_usage", _record)
    return modelo, registros, pedidos


class TestConContexto:
    """Con contexto suficiente se responde, con citaciones."""

    async def test_genera_respuesta_y_confianza(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """La respuesta del modelo y la confianza promedio llegan al estado."""
        servicio = FakeRAGService(chunks=[_chunk(0.90), _chunk(0.80)])
        _preparar(monkeypatch, servicio)

        resultado = await nodo.rag_query_node(estado())

        assert resultado["response_text"].startswith("Atendemos")
        assert resultado["rag_confidence"] == pytest.approx(0.85)
        assert resultado["requires_handoff"] is False

    async def test_el_contexto_viaja_con_su_citacion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cada chunk queda en el estado con su cita, para auditar la respuesta."""
        servicio = FakeRAGService(chunks=[_chunk(0.9)])
        _preparar(monkeypatch, servicio)

        resultado = await nodo.rag_query_node(estado())

        assert resultado["rag_context"][0]["citation"] == "[Fuente: FAQ, pag. 1]"

    async def test_usa_los_parametros_de_retrieval_del_tenant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`rag_top_k` y `rag_threshold` del tenant llegan al retrieval."""
        servicio = FakeRAGService(chunks=[_chunk(0.9)])
        _preparar(
            monkeypatch,
            servicio,
            settings=AgentSettings(model="gpt-4o", rag_top_k=9, rag_threshold=0.66),
        )

        await nodo.rag_query_node(estado())

        assert servicio.retrieve_args["top_k"] == 9
        assert servicio.retrieve_args["threshold"] == pytest.approx(0.66)

    async def test_los_few_shot_usan_un_umbral_mas_estricto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Los ejemplos aprobados se piden con `similarity_threshold` del tenant."""
        ejemplos = [{"question": "Horario?", "answer": "8 a 18", "similarity": 0.91}]
        servicio = FakeRAGService(chunks=[_chunk(0.9)], few_shot=ejemplos)
        _preparar(
            monkeypatch,
            servicio,
            settings=AgentSettings(model="gpt-4o", rag_threshold=0.70, few_shot_threshold=0.85),
        )

        resultado = await nodo.rag_query_node(estado())

        assert servicio.few_shot_args["threshold"] == pytest.approx(0.85)
        assert servicio.prompt_args["few_shot_examples"] == ejemplos
        assert resultado["approved_examples"] == ejemplos

    async def test_el_system_prompt_del_tenant_se_antepone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si el tenant configuro un system prompt, va como primer mensaje."""
        servicio = FakeRAGService(chunks=[_chunk(0.9)])
        modelo, _, _ = _preparar(
            monkeypatch,
            servicio,
            settings=AgentSettings(model="gpt-4o", system_prompt="Eres formal."),
        )

        await nodo.rag_query_node(estado())

        mensajes = modelo.llamadas[0]
        assert mensajes[0] == {"role": "system", "content": "Eres formal."}
        assert mensajes[1]["content"] == "PROMPT CON CONTEXTO"

    async def test_respeta_el_modelo_degradado_del_estado(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si el presupuesto degrado el modelo, el nodo usa ese, no el del tenant."""
        servicio = FakeRAGService(chunks=[_chunk(0.9)])
        _, registros, pedidos = _preparar(monkeypatch, servicio)

        await nodo.rag_query_node(estado(model_to_use="gpt-4o-mini"))

        assert pedidos[0][0] == "gpt-4o-mini"
        assert registros[0]["model"] == "gpt-4o-mini"
        assert registros[0]["operation"] == "rag_query"

    async def test_propaga_el_modo_entrenamiento_del_tenant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`training_mode` sale de la configuracion, no del estado entrante."""
        servicio = FakeRAGService(chunks=[_chunk(0.9)])
        _preparar(monkeypatch, servicio, settings=AgentSettings(model="gpt-4o", training_mode=True))

        resultado = await nodo.rag_query_node(estado())

        assert resultado["training_mode"] is True


class TestSinContexto:
    """Sin contexto suficiente no se responde: se escala."""

    async def test_sin_chunks_marca_handoff_y_no_llama_al_llm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ningun chunk sobre el umbral significa transferir, no improvisar."""
        servicio = FakeRAGService(chunks=[])
        modelo, registros, _ = _preparar(monkeypatch, servicio)

        resultado = await nodo.rag_query_node(estado())

        assert resultado["requires_handoff"] is True
        assert resultado["handoff_reason"] == "insufficient_context"
        assert resultado["rag_confidence"] == 0.0
        assert modelo.llamadas == []
        assert registros == []

    async def test_mensaje_vacio_escala_sin_buscar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin texto no hay nada que buscar en el knowledge base."""
        servicio = FakeRAGService(chunks=[_chunk(0.9)])
        _preparar(monkeypatch, servicio)

        resultado = await nodo.rag_query_node(estado(message={"text": "   "}))

        assert resultado["requires_handoff"] is True
        assert servicio.retrieve_args == {}

    async def test_respuesta_vacia_del_modelo_escala(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Si el modelo no devuelve texto, el contacto no se queda sin respuesta."""
        servicio = FakeRAGService(chunks=[_chunk(0.9)])
        _, registros, _ = _preparar(monkeypatch, servicio, respuesta=RespuestaLLM("  ", 10, 0))

        resultado = await nodo.rag_query_node(estado())

        assert resultado["requires_handoff"] is True
        assert resultado["handoff_reason"] == "insufficient_context"
        # El consumo igual se registra: la llamada al modelo ya se pago.
        assert registros[0]["prompt_tokens"] == 10
