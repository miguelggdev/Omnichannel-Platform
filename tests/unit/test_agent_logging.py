"""Tests de los endpoints de consulta de actividad de agentes.

El modelo `AgentActionLog` y el servicio `AgentLogger` son entrega de Dev A
(`specs/sprint-07-addendum-agent-logging.md` §2 y §3) y todavia no existen. Estos
tests cubren las dos mitades:

  - el camino degradado, que es el real hoy: sin el modelo, los tres endpoints
    responden 503 y la API arranca igual;
  - el camino completo, con el modelo sustituido por un doble, para que el dia
    que Dev A lo entregue se sepa si los endpoints hacen lo que prometen.
"""

import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase

from app.api.v1 import agent_logs as agent_logs_module
from tests.unit.agent_doubles import fake_tenant_session
from tests.unit.crm_doubles import CrmSession, FakeAgentLog

URL = "/api/v1/agent-logs"


class _Base(DeclarativeBase):
    """Base propia: el modelo doble no debe entrar en el metadata de la app."""


class AgentActionLogDoble(_Base):
    """Copia minima del modelo de Dev A, suficiente para compilar las queries."""

    __tablename__ = "agent_action_logs"

    id = Column(PG_UUID(as_uuid=True), primary_key=True)
    client_id = Column(PG_UUID(as_uuid=True), nullable=False)
    conversation_id = Column(PG_UUID(as_uuid=True), nullable=False)
    message_id = Column(PG_UUID(as_uuid=True))
    node_name = Column(String(100), nullable=False)
    action_type = Column(String(50), nullable=False)
    input_summary = Column(Text)
    output_summary = Column(Text)
    details = Column(JSONB)
    duration_ms = Column(Integer)
    tokens_used = Column(Integer)
    model_used = Column(String(100))
    status = Column(String(20))
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True))


@pytest.fixture
def modelo_entregado(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Simula que Dev A ya entrego `app/models/agent_action_log.py`."""
    modulo = types.ModuleType("app.models.agent_action_log")
    modulo.AgentActionLog = AgentActionLogDoble  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.models.agent_action_log", modulo)
    return AgentActionLogDoble


def _usa_sesion(monkeypatch: pytest.MonkeyPatch, session: CrmSession) -> CrmSession:
    """Hace que los endpoints de agent-logs usen la sesion falsa indicada."""
    monkeypatch.setattr(agent_logs_module, "tenant_session", fake_tenant_session(session))
    return session


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
        """Copia las cinco columnas del SELECT agregado."""
        self.node_name = node_name
        self.total_actions = total_actions
        self.avg_duration_ms = avg_duration_ms
        self.total_tokens = total_tokens
        self.error_count = error_count


# ─── Sin la entrega de Dev A ─────────────────────────────────────────────────


class TestDegradacion:
    """Mientras el modelo no exista, los endpoints degradan en vez de romper."""

    @pytest.mark.parametrize(
        "ruta",
        [f"{URL}/conversations/{uuid.uuid4()}", f"{URL}/stats", f"{URL}/errors"],
    )
    async def test_sin_el_modelo_responde_503(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, ruta: str
    ) -> None:
        """Los tres endpoints avisan con 503, no con un 500 ni un traceback."""
        _usa_sesion(monkeypatch, CrmSession())

        response = await authenticated_client.get(ruta)

        assert response.status_code == 503
        assert response.json()["error_code"] == "AGENT_LOGGING_UNAVAILABLE"

    async def test_la_api_arranca_igual(self) -> None:
        """El router se registra aunque el modelo no este: el import es perezoso.

        Es la razon de ser del guard: un `from app.models.agent_action_log import ...`
        en la cabecera del router tumbaria `create_app()` entero.

        Se mira el esquema de OpenAPI y no `app.routes` porque lo segundo es
        estructura interna: entre FastAPI 0.136 y 0.141 cambio donde acaban las
        rutas incluidas y `app.routes` paso a devolver solo las de la
        documentacion. `openapi()["paths"]` es el contrato publico y no depende
        de esa version.
        """
        from app.main import create_app

        rutas = create_app().openapi()["paths"]
        assert f"{URL}/stats" in rutas
        assert f"{URL}/errors" in rutas
        assert f"{URL}/conversations/{{conversation_id}}" in rutas


# ─── Traza de una conversacion ───────────────────────────────────────────────


class TestTrazaDeConversacion:
    """GET /agent-logs/conversations/{id}."""

    async def test_devuelve_los_registros(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, modelo_entregado: Any
    ) -> None:
        """Con el modelo entregado, la traza sale completa."""
        conv = uuid.uuid4()
        registros = [
            FakeAgentLog(conversation_id=conv, node_name="intent_routing"),
            FakeAgentLog(conversation_id=conv, node_name="rag_query"),
        ]
        _usa_sesion(monkeypatch, CrmSession(resultados=[registros]))

        response = await authenticated_client.get(f"{URL}/conversations/{conv}")

        assert response.status_code == 200
        assert [r["node_name"] for r in response.json()] == ["intent_routing", "rag_query"]

    async def test_filtra_por_nodo_y_estado(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, modelo_entregado: Any
    ) -> None:
        """Los dos filtros opcionales llegan al WHERE."""
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[[]]))

        response = await authenticated_client.get(
            f"{URL}/conversations/{uuid.uuid4()}",
            params={"node_name": "rag_query", "status": "error"},
        )

        assert response.status_code == 200
        sql = str(session.executed[0]).lower()
        assert "agent_action_logs.node_name =" in sql
        assert "agent_action_logs.status =" in sql

    async def test_un_status_inventado_es_400(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, modelo_entregado: Any
    ) -> None:
        """Solo los 4 estados que escribe AgentLogger son filtros validos."""
        _usa_sesion(monkeypatch, CrmSession())

        response = await authenticated_client.get(
            f"{URL}/conversations/{uuid.uuid4()}", params={"status": "zombie"}
        )

        assert response.status_code == 400

    async def test_el_client_id_va_explicito_en_el_where(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, modelo_entregado: Any
    ) -> None:
        """El filtro de tenant no queda delegado solo a la RLS (CLAUDE.md §2)."""
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[[]]))

        await authenticated_client.get(f"{URL}/conversations/{uuid.uuid4()}")

        assert "agent_action_logs.client_id =" in str(session.executed[0])

    async def test_el_rol_agent_no_ve_las_trazas(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        modelo_entregado: Any,
    ) -> None:
        """La traza interna del grafo es para supervisor para arriba."""
        _usa_sesion(monkeypatch, CrmSession())
        client = authenticated_client_factory(role="agent")

        response = await client.get(f"{URL}/conversations/{uuid.uuid4()}")

        assert response.status_code == 403

    @pytest.mark.parametrize("limit", [0, 501])
    async def test_limit_fuera_de_rango_es_422(
        self,
        authenticated_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        modelo_entregado: Any,
        limit: int,
    ) -> None:
        """El limite esta acotado entre 1 y 500."""
        _usa_sesion(monkeypatch, CrmSession())

        response = await authenticated_client.get(
            f"{URL}/conversations/{uuid.uuid4()}", params={"limit": limit}
        )

        assert response.status_code == 422


# ─── Estadisticas por nodo ───────────────────────────────────────────────────


class TestStats:
    """GET /agent-logs/stats."""

    async def test_calcula_la_tasa_de_error(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, modelo_entregado: Any
    ) -> None:
        """La tasa es el porcentaje de acciones con status error."""
        _usa_sesion(
            monkeypatch,
            CrmSession(resultados=[[_FilaStats("rag_query", 8, 250.0, 1200, 2)]]),
        )

        response = await authenticated_client.get(f"{URL}/stats")

        assert response.status_code == 200
        fila = response.json()[0]
        assert fila["node_name"] == "rag_query"
        assert fila["error_rate"] == 25.0
        assert fila["avg_duration_ms"] == 250.0
        assert fila["total_tokens"] == 1200

    async def test_un_nodo_sin_acciones_no_divide_entre_cero(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, modelo_entregado: Any
    ) -> None:
        """Con total_actions=0 la tasa es 0, no una division por cero."""
        _usa_sesion(monkeypatch, CrmSession(resultados=[[_FilaStats("respond", 0, None, None, 0)]]))

        response = await authenticated_client.get(f"{URL}/stats")

        assert response.status_code == 200
        assert response.json()[0]["error_rate"] == 0.0
        assert response.json()[0]["avg_duration_ms"] == 0.0

    async def test_la_ventana_de_tiempo_llega_a_30_dias(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, modelo_entregado: Any
    ) -> None:
        """720 horas es el maximo; mas se rechaza."""
        _usa_sesion(monkeypatch, CrmSession(resultados=[[], []]))

        ok = await authenticated_client.get(f"{URL}/stats", params={"hours": 720})
        pasado = await authenticated_client.get(f"{URL}/stats", params={"hours": 721})

        assert ok.status_code == 200
        assert pasado.status_code == 422

    async def test_el_corte_lleva_timezone(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, modelo_entregado: Any
    ) -> None:
        """`created_at` es TIMESTAMPTZ: compararlo con un naive corre la ventana."""
        registrado: dict[str, Any] = {}

        class _Sesion(CrmSession):
            async def execute(self, stmt: Any = None, params: Any = None) -> Any:
                registrado["params"] = list(stmt.compile().params.values())
                return await super().execute(stmt, params)

        _usa_sesion(monkeypatch, _Sesion(resultados=[[]]))

        await authenticated_client.get(f"{URL}/stats", params={"hours": 24})

        cortes = [p for p in registrado["params"] if isinstance(p, datetime)]
        assert cortes, "la query tiene que llevar un corte de fecha"
        assert all(c.tzinfo is not None for c in cortes)

    async def test_el_rol_supervisor_no_ve_las_stats(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        modelo_entregado: Any,
    ) -> None:
        """Las metricas agregadas del tenant son de admin para arriba."""
        _usa_sesion(monkeypatch, CrmSession())
        client = authenticated_client_factory(role="supervisor")

        response = await client.get(f"{URL}/stats")

        assert response.status_code == 403


# ─── Errores recientes ───────────────────────────────────────────────────────


class TestErrores:
    """GET /agent-logs/errors."""

    async def test_devuelve_solo_los_errores(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, modelo_entregado: Any
    ) -> None:
        """El WHERE fija status='error' sin depender de lo que pida el cliente."""
        fallo = FakeAgentLog(status="error", error_message="timeout del LLM")
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[[fallo]]))

        response = await authenticated_client.get(f"{URL}/errors")

        assert response.status_code == 200
        assert response.json()[0]["error_message"] == "timeout del LLM"
        assert "agent_action_logs.status =" in str(session.executed[0])

    async def test_los_mas_recientes_primero(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, modelo_entregado: Any
    ) -> None:
        """Para diagnosticar interesa lo ultimo que rompio."""
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[[]]))

        await authenticated_client.get(f"{URL}/errors")

        assert "order by agent_action_logs.created_at desc" in str(session.executed[0]).lower()

    async def test_la_ventana_por_defecto_es_de_un_dia(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, modelo_entregado: Any
    ) -> None:
        """Sin `hours`, se miran las ultimas 24 horas."""
        registrado: dict[str, Any] = {}

        class _Sesion(CrmSession):
            async def execute(self, stmt: Any = None, params: Any = None) -> Any:
                registrado["params"] = list(stmt.compile().params.values())
                return await super().execute(stmt, params)

        _usa_sesion(monkeypatch, _Sesion(resultados=[[]]))

        await authenticated_client.get(f"{URL}/errors")

        corte = next(p for p in registrado["params"] if isinstance(p, datetime))
        esperado = datetime.now(timezone.utc) - timedelta(hours=24)
        assert abs((corte - esperado).total_seconds()) < 60

    async def test_sin_token_es_401(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch, modelo_entregado: Any
    ) -> None:
        """La traza del grafo no se consulta sin identificarse."""
        _usa_sesion(monkeypatch, CrmSession())

        response = await api_client.get(f"{URL}/errors")

        assert response.status_code == 401
