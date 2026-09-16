"""Tests de `app/agents/nodes/_tenant.py::_as_agents()` y `get_agent_settings()`.

Ningun otro test de Sprint 6 ejercita `_as_agents()` directamente: los tests de
los nodos sustituyen `get_agent_settings()` entero por un `AgentSettings` fijo
(`agent_doubles.parchear_agent_settings`), asi que nunca pasan por la
normalizacion de `config.enabled_agents`.
"""

from typing import Any
from uuid import uuid4

import pytest

from app.agents.nodes import _tenant as modulo
from app.agents.nodes._tenant import DEFAULT_ENABLED_AGENTS, _as_agents, get_agent_settings
from tests.unit.agent_doubles import FakeSession, parchear_tenant_session


class _FakeAgentConfig:
    """Sustituto minimo de `AgentConfig` con lo que lee `get_agent_settings()`."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Prepara el doble con un `config` JSONB dado; el resto son defaults razonables."""
        self.name = "Asistente"
        self.model = "gpt-4o"
        self.temperature = 0.3
        self.system_prompt = None
        self.welcome_message = None
        self.handoff_message = None
        self.training_mode = False
        self.similarity_threshold = 0.80
        self.config = config


class TestAsAgents:
    """`_as_agents()`: normaliza `config.enabled_agents`."""

    def test_ausente_cae_al_default(self) -> None:
        """Sin la clave `enabled_agents`, se usa el default (solo RAG)."""
        assert _as_agents(None) == DEFAULT_ENABLED_AGENTS

    def test_tipo_invalido_cae_al_default(self) -> None:
        """Un valor que no es lista ni tupla (dato corrupto) tambien cae al default."""
        assert _as_agents("rag") == DEFAULT_ENABLED_AGENTS
        assert _as_agents(123) == DEFAULT_ENABLED_AGENTS

    def test_lista_vacia_se_respeta(self) -> None:
        """Un tenant puede deshabilitar todos los agentes a proposito.

        `[]` es una configuracion valida (todo a un humano), no "ausente": no
        debe caer al default.
        """
        assert _as_agents([]) == ()

    def test_lista_con_agentes_se_devuelve_tal_cual(self) -> None:
        """Una lista de agentes validos se devuelve como tupla, en orden."""
        assert _as_agents(["rag", "scheduling"]) == ("rag", "scheduling")

    def test_filtra_items_que_no_son_string(self) -> None:
        """Un item corrupto en la lista no invalida a los demas."""
        assert _as_agents(["rag", 123, None]) == ("rag",)

    def test_todos_los_items_invalidos_da_tupla_vacia(self) -> None:
        """Si ningun item es valido, el resultado es vacio, no el default.

        Es mas seguro tratar una lista corrupta como "sin agentes" (todo a un
        humano) que como "no configurado" (que habilitaria RAG por default).
        """
        assert _as_agents([123, None]) == ()


class TestGetAgentSettings:
    """`get_agent_settings()`: la fila de `agent_configs` llega completa a `AgentSettings`."""

    async def test_enabled_agents_vacio_llega_vacio_a_agent_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un `config.enabled_agents: []` real en la fila se respeta de punta a punta."""
        fila = _FakeAgentConfig({"enabled_agents": []})
        sesion = FakeSession(resultados=[fila])
        parchear_tenant_session(monkeypatch, modulo, sesion)

        settings = await get_agent_settings(uuid4())

        assert settings.enabled_agents == ()

    async def test_sin_config_usa_el_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un tenant que no toco `config` en absoluto sigue con RAG habilitado."""
        fila = _FakeAgentConfig({})
        sesion = FakeSession(resultados=[fila])
        parchear_tenant_session(monkeypatch, modulo, sesion)

        settings = await get_agent_settings(uuid4())

        assert settings.enabled_agents == DEFAULT_ENABLED_AGENTS
