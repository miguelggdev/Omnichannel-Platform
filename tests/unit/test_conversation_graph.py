"""Tests de `app/agents/graph.py`: routing puro y ensamblado del grafo.

No hace falta base de datos ni LLM: el routing son funciones puras sobre el
estado, y `build_conversation_graph().compile()` sin checkpointer no abre
ninguna conexion (el checkpointer solo se crea dentro de
`_CheckpointedGraph.ainvoke()`, ver el modulo). Ese camino con Postgres real
queda para un test de integracion aparte, porque necesita `--run-db`.
"""

import pytest

from app.agents import graph as graph_module
from app.agents.graph import (
    build_conversation_graph,
    get_graph_with_checkpointer,
    route_after_budget_check,
    route_after_intent,
    route_after_rag,
)
from tests.unit.agent_doubles import estado


class TestRouteAfterBudgetCheck:
    """`route_after_budget_check`: presupuesto agotado corta antes del routing."""

    def test_ok_continua(self) -> None:
        """Presupuesto en verde sigue al routing de intents."""
        assert route_after_budget_check(estado(budget_status="ok")) == "continue"

    def test_degraded_continua(self) -> None:
        """Degradado tambien sigue: solo cambia el modelo, no la ruta."""
        assert route_after_budget_check(estado(budget_status="degraded")) == "continue"

    def test_exceeded_corta(self) -> None:
        """Agotado corta directo a handoff, sin pasar por intent routing."""
        assert route_after_budget_check(estado(budget_status="exceeded")) == "exceeded"


class TestRouteAfterIntent:
    """`route_after_intent`: mapea el intent ya resuelto a la siguiente rama."""

    @pytest.mark.parametrize("intent", ["greeting", "farewell"])
    def test_intents_directos_van_a_respond(self, intent: str) -> None:
        """Saludo y despedida no necesitan RAG ni humano."""
        assert route_after_intent(estado(intent=intent)) == "respond"

    @pytest.mark.parametrize("intent", ["human_request", "complaint"])
    def test_intents_de_escalado_van_a_human_handoff(self, intent: str) -> None:
        """Pedido explicito de humano y quejas escalan directo."""
        assert route_after_intent(estado(intent=intent)) == "human_handoff"

    @pytest.mark.parametrize("intent", ["rag_query", "unknown", "scheduling", None])
    def test_el_resto_intenta_rag_primero(self, intent: str | None) -> None:
        """rag_query, unknown y scheduling (Sprint 7 aun no existe) van a RAG."""
        assert route_after_intent(estado(intent=intent)) == "rag_query"


class TestRouteAfterRag:
    """`route_after_rag`: decide respuesta, escalado o aprobacion."""

    def test_requiere_handoff_escala(self) -> None:
        """Sin contexto suficiente, `rag_query_node` ya marco el handoff."""
        assert route_after_rag(estado(requires_handoff=True)) == "human_handoff"

    def test_training_mode_retiene_la_respuesta(self) -> None:
        """Con contexto y modo entrenamiento, la respuesta va a aprobacion."""
        assert (
            route_after_rag(estado(requires_handoff=False, training_mode=True)) == "training_mode"
        )

    def test_caso_normal_responde(self) -> None:
        """Con contexto y sin modo entrenamiento, se envia la respuesta."""
        assert route_after_rag(estado(requires_handoff=False, training_mode=False)) == "respond"


class TestBuildConversationGraph:
    """El grafo se arma y compila con los 6 nodos del sprint."""

    def test_compila_sin_checkpointer(self) -> None:
        """Compilar sin checkpointer no debe fallar ni abrir ninguna conexion."""
        compiled = build_conversation_graph().compile()

        assert compiled is not None

    def test_tiene_los_seis_nodos(self) -> None:
        """Los 6 nodos de la spec quedan registrados con su nombre."""
        graph = build_conversation_graph()

        assert set(graph.nodes.keys()) == {
            "token_budget_check",
            "intent_routing",
            "rag_query",
            "respond",
            "human_handoff",
            "training_mode_approval",
        }


class TestGetGraphWithCheckpointer:
    """`get_graph_with_checkpointer()`: contrato que espera `ai_processor.py`."""

    async def test_devuelve_un_objeto_con_ainvoke(self) -> None:
        """`ai_processor._compile_graph()` solo necesita un `.ainvoke()` awaitable.

        No abre ninguna conexion: `_CheckpointedGraph` no toca `psycopg` hasta
        que se llama `ainvoke()` (ver el modulo).
        """
        compilado = await get_graph_with_checkpointer()

        assert hasattr(compilado, "ainvoke")
        assert callable(compilado.ainvoke)

    def test_checkpointer_conninfo_prefiere_database_url_direct(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`DATABASE_URL_DIRECT`, cuando esta configurada, gana sobre el pooler."""
        settings = graph_module.get_settings()
        monkeypatch.setattr(settings, "DATABASE_URL_DIRECT", "postgresql+asyncpg://direct/db")
        monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+asyncpg://pooler/db")

        assert graph_module._checkpointer_conninfo() == "postgresql://direct/db"

    def test_checkpointer_conninfo_cae_a_database_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin `DATABASE_URL_DIRECT` (como en CI), se usa `DATABASE_URL`."""
        settings = graph_module.get_settings()
        monkeypatch.setattr(settings, "DATABASE_URL_DIRECT", "")
        monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+asyncpg://ci/db")

        assert graph_module._checkpointer_conninfo() == "postgresql://ci/db"

    def test_checkpointer_conninfo_no_es_un_driver_de_sqlalchemy(self) -> None:
        """`psycopg` no entiende el sufijo `+asyncpg` que usa SQLAlchemy."""
        conninfo: str = graph_module._checkpointer_conninfo()

        assert "+asyncpg" not in conninfo
        assert conninfo.startswith("postgresql://")
