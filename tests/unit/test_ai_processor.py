"""Tests de la tarea de Celery que hace correr el grafo (Sprint 6).

Contrato: `specs/sprint-06-langgraph.md` §11. El grafo es entrega de Dev A y
todavia no esta en `main`: estos tests fijan el contrato de la tarea (estado
inicial, thread_id, reintentos y handoff de emergencia) para que cuando llegue
solo haya que enchufarlo.
"""

import sys
from types import ModuleType
from typing import Any

import pytest

from app.tasks import ai_processor as tarea
from tests.unit.agent_doubles import estado

try:  # El grafo es entrega de Dev A: puede estar o no estar en `main`.
    import app.agents.graph  # noqa: F401

    GRAFO_ENTREGADO = True
except ImportError:
    GRAFO_ENTREGADO = False

ARGS = {
    "client_id": "11111111-1111-1111-1111-111111111111",
    "conversation_id": "22222222-2222-2222-2222-222222222222",
    "contact_id": "33333333-3333-3333-3333-333333333333",
    "channel": "whatsapp",
    "message_data": {"text": "Hola"},
}


class FakeTaskSelf:
    """Sustituto del `self` de una tarea Celery con bind=True."""

    def __init__(self, retries: int = 0, max_retries: int = 2) -> None:
        """Prepara el doble.

        Args:
            retries: Reintentos ya consumidos.
            max_retries: Maximo configurado en la tarea.
        """
        self.request = type("Request", (), {"retries": retries})()
        self.max_retries = max_retries
        self.retry_calls = 0
        self.last_countdown: float | None = None

    def retry(self, exc: Exception | None = None, countdown: float | None = None) -> Exception:
        """Devuelve la excepcion que el codigo relanza como reintento."""
        self.retry_calls += 1
        self.last_countdown = countdown
        return RuntimeError("retry solicitado")


class FakeCompiledGraph:
    """Grafo compilado que devuelve un estado final prefijado.

    Attributes:
        invocaciones: Tuplas `(estado, config)` con las que se invoco.
    """

    def __init__(self, resultado: dict[str, Any] | None = None) -> None:
        """Prepara el doble.

        Args:
            resultado: Estado final que devuelve `ainvoke()`.
        """
        self.resultado = resultado or {"intent": "rag_query", "requires_handoff": False}
        self.invocaciones: list[tuple[Any, Any]] = []

    async def ainvoke(self, state: Any, config: Any = None) -> dict[str, Any]:
        """Registra la invocacion y devuelve el estado final."""
        self.invocaciones.append((state, config))
        return self.resultado


def _capturar_handoff(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Sustituye el handoff de emergencia y captura sus llamadas.

    Args:
        monkeypatch: Fixture de pytest.

    Returns:
        Lista donde se acumulan los handoffs pedidos.
    """
    llamadas: list[dict[str, Any]] = []

    async def _handoff(
        client_id: str, conversation_id: str, contact_id: str, channel: str, reason: str
    ) -> None:
        llamadas.append(
            {
                "client_id": client_id,
                "conversation_id": conversation_id,
                "channel": channel,
                "reason": reason,
            }
        )

    monkeypatch.setattr(tarea, "_emergency_handoff", _handoff)
    return llamadas


class TestEstadoInicial:
    """El estado con el que entra el mensaje al grafo."""

    def test_trae_todos_los_campos_del_contrato(self) -> None:
        """Ningun nodo debe encontrarse una clave ausente."""
        state = tarea._initial_state(
            ARGS["client_id"],
            ARGS["conversation_id"],
            ARGS["contact_id"],
            ARGS["channel"],
            ARGS["message_data"],
        )

        assert set(state) == set(estado())
        assert state["requires_handoff"] is False
        assert state["budget_status"] == "ok"
        assert state["message"] == {"text": "Hola"}


class TestInvocacion:
    """La tarea invoca el grafo con el hilo correcto."""

    async def test_el_thread_id_lleva_tenant_y_conversacion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El checkpointer agrupa por conversacion, sin cruzar tenants."""
        grafo = FakeCompiledGraph()

        async def _compile() -> FakeCompiledGraph:
            return grafo

        monkeypatch.setattr(tarea, "_compile_graph", _compile)

        await tarea._invoke_graph(**ARGS)

        _, config = grafo.invocaciones[0]
        assert config["configurable"]["thread_id"] == (
            f"{ARGS['client_id']}:{ARGS['conversation_id']}"
        )

    def test_exito_devuelve_processed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un recorrido sin errores responde processed."""

        async def _ok(**kwargs: Any) -> dict[str, Any]:
            return {"intent": "greeting"}

        monkeypatch.setattr(tarea, "_invoke_graph", _ok)

        assert tarea.process_ai_response.run(**ARGS) == {"status": "processed"}


class TestGrafoAusente:
    """Mientras Dev A no entregue el grafo, cada mensaje va a un humano."""

    @pytest.mark.skipif(GRAFO_ENTREGADO, reason="Dev A ya entrego app/agents/graph.py")
    async def test_sin_modulo_el_compilado_falla_con_error_propio(self) -> None:
        """El ImportError se traduce a un error que la tarea sabe no reintentar."""
        with pytest.raises(tarea.GraphUnavailableError):
            await tarea._compile_graph()

    async def test_modulo_sin_constructor_tambien_falla(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un `graph.py` que no expone nada conocido no pasa por bueno."""
        modulo = ModuleType("app.agents.graph")
        monkeypatch.setitem(sys.modules, "app.agents.graph", modulo)

        with pytest.raises(tarea.GraphUnavailableError, match="no expone"):
            await tarea._compile_graph()

    async def test_sin_checkpointer_compila_igual(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Si Dev A entrega solo `build_conversation_graph`, la tarea sigue."""
        grafo = FakeCompiledGraph()
        modulo = ModuleType("app.agents.graph")
        modulo.build_conversation_graph = lambda: type(  # type: ignore[attr-defined]
            "G", (), {"compile": lambda self: grafo}
        )()
        monkeypatch.setitem(sys.modules, "app.agents.graph", modulo)

        assert await tarea._compile_graph() is grafo

    def test_la_tarea_escala_sin_reintentar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reintentar no va a hacer aparecer el modulo: se transfiere y punto."""

        async def _sin_grafo(**kwargs: Any) -> None:
            raise tarea.GraphUnavailableError("no esta")

        monkeypatch.setattr(tarea, "_invoke_graph", _sin_grafo)
        llamadas = _capturar_handoff(monkeypatch)
        task_self = FakeTaskSelf()
        cuerpo = tarea.process_ai_response.__wrapped__.__func__

        resultado = cuerpo(task_self, **ARGS)

        assert resultado == {"status": "handoff"}
        assert task_self.retry_calls == 0
        assert llamadas[0]["reason"] == tarea.GRAPH_ERROR_REASON
        assert llamadas[0]["conversation_id"] == ARGS["conversation_id"]


class TestReintentos:
    """Politica de reintentos y degradacion final."""

    cuerpo = staticmethod(tarea.process_ai_response.__wrapped__.__func__)

    def test_fallo_transitorio_reintenta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Con reintentos disponibles se reintenta, sin escalar todavia."""

        async def _boom(**kwargs: Any) -> None:
            raise RuntimeError("openai caido")

        monkeypatch.setattr(tarea, "_invoke_graph", _boom)
        llamadas = _capturar_handoff(monkeypatch)
        task_self = FakeTaskSelf(retries=0)

        with pytest.raises(RuntimeError, match="retry solicitado"):
            self.cuerpo(task_self, **ARGS)

        assert task_self.retry_calls == 1
        assert task_self.last_countdown == tarea.RETRY_COUNTDOWN_SECONDS
        assert llamadas == []

    def test_reintentos_agotados_escalan_a_humano(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Agotados los intentos, el contacto no se queda sin respuesta."""

        async def _boom(**kwargs: Any) -> None:
            raise RuntimeError("openai caido")

        monkeypatch.setattr(tarea, "_invoke_graph", _boom)
        llamadas = _capturar_handoff(monkeypatch)
        task_self = FakeTaskSelf(retries=2)

        resultado = self.cuerpo(task_self, **ARGS)

        assert resultado == {"status": "handoff"}
        assert task_self.retry_calls == 0
        assert llamadas[0]["reason"] == tarea.GRAPH_ERROR_REASON

    def test_timeout_blando_no_se_reintenta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un grafo que agoto sus 100s volveria a agotarlos: se escala."""
        from celery.exceptions import SoftTimeLimitExceeded

        async def _lento(**kwargs: Any) -> None:
            raise SoftTimeLimitExceeded

        monkeypatch.setattr(tarea, "_invoke_graph", _lento)
        llamadas = _capturar_handoff(monkeypatch)
        task_self = FakeTaskSelf(retries=0)

        resultado = self.cuerpo(task_self, **ARGS)

        assert resultado == {"status": "handoff"}
        assert task_self.retry_calls == 0
        assert len(llamadas) == 1


class TestHandoffDeEmergencia:
    """El handoff de emergencia reusa el nodo, no duplica su logica."""

    async def test_llama_al_nodo_de_handoff_con_el_motivo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La conversacion se escala con `graph_error` como motivo."""
        recibidos: list[dict[str, Any]] = []

        async def _nodo(state: dict[str, Any]) -> dict[str, Any]:
            recibidos.append(state)
            return {}

        from app.agents.nodes import human_handoff as handoff_module

        monkeypatch.setattr(handoff_module, "human_handoff_node", _nodo)

        await tarea._emergency_handoff(
            ARGS["client_id"],
            ARGS["conversation_id"],
            ARGS["contact_id"],
            ARGS["channel"],
            tarea.GRAPH_ERROR_REASON,
        )

        assert recibidos[0]["handoff_reason"] == tarea.GRAPH_ERROR_REASON
        assert recibidos[0]["conversation_id"] == ARGS["conversation_id"]
