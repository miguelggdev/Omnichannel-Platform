"""Tests de `app/services/agent_logger.py::AgentLogger`."""

import uuid

from app.services.agent_logger import AgentLogger
from tests.unit.agent_doubles import FakeSession


class _FilaStats:
    """Fila agregada tal como la devuelve el GROUP BY por nodo."""

    def __init__(
        self,
        node_name: str,
        total_actions: int,
        avg_duration_ms: float | None,
        total_tokens: int | None,
        error_count: int,
    ) -> None:
        self.node_name = node_name
        self.total_actions = total_actions
        self.avg_duration_ms = avg_duration_ms
        self.total_tokens = total_tokens
        self.error_count = error_count


class TestTruncate:
    """`_truncate()` corta los resúmenes largos."""

    def test_texto_corto_no_se_toca(self) -> None:
        """Un texto dentro del límite se devuelve tal cual."""
        logger_ = AgentLogger(FakeSession(), uuid.uuid4())

        assert logger_._truncate("hola") == "hola"

    def test_none_se_respeta(self) -> None:
        """Sin texto, no hay nada que truncar."""
        logger_ = AgentLogger(FakeSession(), uuid.uuid4())

        assert logger_._truncate(None) is None

    def test_texto_largo_se_trunca_con_puntos_suspensivos(self) -> None:
        """Pasado el límite, se corta y se marca con `...`."""
        logger_ = AgentLogger(FakeSession(), uuid.uuid4())
        largo = "x" * 600

        resultado = logger_._truncate(largo)

        assert resultado is not None
        assert len(resultado) == AgentLogger.MAX_SUMMARY_LENGTH + 3
        assert resultado.endswith("...")


class TestLogAction:
    """`log_action()` agrega la entrada a la sesión con los campos dados."""

    async def test_agrega_la_entrada_con_el_client_id_del_logger(self) -> None:
        """El `client_id` sale del logger, no de un parámetro de la llamada."""
        sesion = FakeSession()
        client_id = uuid.uuid4()
        conversation_id = uuid.uuid4()
        logger_ = AgentLogger(sesion, client_id)

        entrada = await logger_.log_action(
            conversation_id=conversation_id,
            node_name="intent_routing",
            action_type="decision",
            duration_ms=42,
            tokens_used=10,
            model_used="gpt-4o-mini",
        )

        assert entrada in sesion.added
        assert entrada.client_id == client_id
        assert entrada.conversation_id == conversation_id
        assert entrada.duration_ms == 42
        assert entrada.tokens_used == 10

    async def test_trunca_los_resumenes_largos(self) -> None:
        """`input_summary`/`output_summary` pasan por `_truncate()`."""
        logger_ = AgentLogger(FakeSession(), uuid.uuid4())
        largo = "y" * 600

        entrada = await logger_.log_action(
            conversation_id=uuid.uuid4(),
            node_name="rag_query",
            action_type="query",
            input_summary=largo,
            output_summary=largo,
        )

        assert entrada.input_summary is not None
        assert len(entrada.input_summary) == AgentLogger.MAX_SUMMARY_LENGTH + 3

    async def test_details_ausente_se_guarda_como_dict_vacio(self) -> None:
        """Sin `details`, se guarda `{}` en vez de `None` (columna JSONB)."""
        logger_ = AgentLogger(FakeSession(), uuid.uuid4())

        entrada = await logger_.log_action(
            conversation_id=uuid.uuid4(), node_name="respond", action_type="response"
        )

        assert entrada.details == {}


class TestGetConversationLog:
    """`get_conversation_log()` filtra por conversación, nodo y estado."""

    async def test_filtra_por_client_id_y_conversacion(self) -> None:
        """La consulta siempre incluye `client_id` (Regla 1 de CLAUDE.md)."""
        sesion = FakeSession(resultados=[[]])
        logger_ = AgentLogger(sesion, uuid.uuid4())

        resultado = await logger_.get_conversation_log(uuid.uuid4())

        assert resultado == []
        assert len(sesion.executed) == 1

    async def test_devuelve_las_entradas_encontradas(self) -> None:
        """Con resultados, se devuelven tal cual (ya en orden por `created_at`)."""
        entradas = [object(), object()]
        sesion = FakeSession(resultados=[entradas])
        logger_ = AgentLogger(sesion, uuid.uuid4())

        resultado = await logger_.get_conversation_log(uuid.uuid4())

        assert resultado == entradas


class TestGetNodeStats:
    """`get_node_stats()` calcula duración promedio y tasa de error por nodo."""

    async def test_sin_registros_devuelve_lista_vacia(self) -> None:
        """Sin actividad en la ventana, no hay nada que agregar."""
        sesion = FakeSession(resultados=[[]])
        logger_ = AgentLogger(sesion, uuid.uuid4())

        assert await logger_.get_node_stats() == []

    async def test_calcula_promedio_y_tasa_de_error(self) -> None:
        """`avg_duration_ms` y `error_rate` se redondean a 2 decimales."""
        fila = _FilaStats(
            node_name="rag_query",
            total_actions=3,
            avg_duration_ms=333.333,
            total_tokens=900,
            error_count=1,
        )
        sesion = FakeSession(resultados=[[fila]])
        logger_ = AgentLogger(sesion, uuid.uuid4())

        stats = await logger_.get_node_stats(hours=24)

        assert stats == [
            {
                "node_name": "rag_query",
                "total_actions": 3,
                "avg_duration_ms": 333.33,
                "total_tokens": 900,
                "error_count": 1,
                "error_rate": 33.33,
            }
        ]

    async def test_sin_duracion_ni_tokens_no_rompe_con_none(self) -> None:
        """`avg()`/`sum()` sobre cero filas dan `NULL`: se tratan como 0."""
        fila = _FilaStats(
            node_name="scheduling",
            total_actions=1,
            avg_duration_ms=None,
            total_tokens=None,
            error_count=0,
        )
        sesion = FakeSession(resultados=[[fila]])
        logger_ = AgentLogger(sesion, uuid.uuid4())

        stats = await logger_.get_node_stats()

        assert stats[0]["avg_duration_ms"] == 0.0
        assert stats[0]["total_tokens"] == 0
        assert stats[0]["error_rate"] == 0
