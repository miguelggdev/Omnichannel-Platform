"""Tests de los nodos `respond` y `human_handoff`, y del envio compartido.

Contrato: `specs/sprint-06-langgraph.md` §8 y §9. Ni el proveedor de mensajeria
ni la base son reales.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.nodes import _delivery as delivery_module
from app.agents.nodes import _tenant as tenant_module
from app.agents.nodes import human_handoff as handoff_module
from app.agents.nodes import respond as respond_module
from app.agents.nodes._tenant import AgentSettings, ChannelNotConfiguredError
from app.models.message import Message
from tests.unit.agent_doubles import (
    FakeSession,
    estado,
    parchear_agent_settings,
    parchear_tenant_session,
)


class FakeConversation:
    """Fila de `conversations` con lo que tocan los nodos."""

    def __init__(self, status: str = "bot_active") -> None:
        """Prepara la conversacion.

        Args:
            status: Estado inicial.
        """
        self.status = status
        self.metadata_: dict[str, Any] = {"origen": "webhook"}
        self.last_message_at: Any = None


def _capturar_envios(monkeypatch: pytest.MonkeyPatch, modulo: Any) -> list[dict[str, Any]]:
    """Sustituye `deliver_message` en un nodo y captura lo que se envia.

    Args:
        monkeypatch: Fixture de pytest.
        modulo: Modulo del nodo.

    Returns:
        Lista donde se acumulan los envios.
    """
    envios: list[dict[str, Any]] = []

    async def _deliver(**kwargs: Any) -> str:
        envios.append(kwargs)
        return "ext-1"

    monkeypatch.setattr(modulo, "deliver_message", _deliver)
    return envios


class TestRespond:
    """El nodo de respuesta envia lo que haya, o un texto acorde al intent."""

    async def test_envia_la_respuesta_generada(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Si el RAG dejo texto, se envia tal cual."""
        envios = _capturar_envios(monkeypatch, respond_module)

        resultado = await respond_module.respond_node(
            estado(response_text="Atendemos de 8 a 18.", intent="rag_query")
        )

        assert resultado["response_text"] == "Atendemos de 8 a 18."
        assert envios[0]["text"] == "Atendemos de 8 a 18."
        assert envios[0]["channel"] == "whatsapp"

    async def test_saludo_usa_el_mensaje_de_bienvenida_del_tenant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La bienvenida configurada gana al texto por defecto."""
        _capturar_envios(monkeypatch, respond_module)
        parchear_agent_settings(
            monkeypatch,
            respond_module,
            AgentSettings(model="gpt-4o", welcome_message="Hola, soy el bot de la clinica."),
        )

        resultado = await respond_module.respond_node(estado(intent="greeting"))

        assert resultado["response_text"] == "Hola, soy el bot de la clinica."

    async def test_saludo_sin_configuracion_usa_el_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin bienvenida configurada, responde el saludo por defecto."""
        _capturar_envios(monkeypatch, respond_module)
        parchear_agent_settings(monkeypatch, respond_module, AgentSettings(model="gpt-4o"))

        resultado = await respond_module.respond_node(estado(intent="greeting"))

        assert resultado["response_text"] == respond_module.DEFAULT_GREETING

    async def test_despedida_y_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`farewell` y cualquier otro intent sin texto tienen su propia salida."""
        _capturar_envios(monkeypatch, respond_module)
        parchear_agent_settings(monkeypatch, respond_module, AgentSettings(model="gpt-4o"))

        despedida = await respond_module.respond_node(estado(intent="farewell"))
        fallback = await respond_module.respond_node(estado(intent="unknown"))

        assert despedida["response_text"] == respond_module.DEFAULT_FAREWELL
        assert fallback["response_text"] == respond_module.DEFAULT_FALLBACK


class TestHumanHandoff:
    """El handoff deja la conversacion en manos de una persona."""

    def _preparar(
        self, monkeypatch: pytest.MonkeyPatch, conversacion: FakeConversation | None
    ) -> tuple[list[dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
        """Deja el nodo listo con base, envio y notificaciones falsos."""
        conversation_id = uuid.UUID(int=1)
        sesion = FakeSession(objetos={conversation_id: conversacion} if conversacion else {})
        parchear_tenant_session(monkeypatch, handoff_module, sesion)
        parchear_agent_settings(monkeypatch, handoff_module, AgentSettings(model="gpt-4o"))
        envios = _capturar_envios(monkeypatch, handoff_module)

        avisos: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            handoff_module,
            "enqueue_notification",
            lambda task_attr, **kwargs: avisos.append((task_attr, kwargs)) or True,
        )
        return envios, avisos

    async def test_marca_waiting_human_y_registra_el_motivo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La conversacion pasa a `waiting_human` con motivo y metricas."""
        conversacion = FakeConversation()
        self._preparar(monkeypatch, conversacion)

        resultado = await handoff_module.human_handoff_node(
            estado(
                conversation_id=str(uuid.UUID(int=1)),
                handoff_reason="insufficient_context",
                rag_confidence=0.42,
                budget_usage_pct=12.5,
                intent="rag_query",
            )
        )

        assert conversacion.status == "waiting_human"
        registro = conversacion.metadata_["handoff"]
        assert registro["reason"] == "insufficient_context"
        assert registro["rag_confidence"] == pytest.approx(0.42)
        assert registro["budget_usage_pct"] == pytest.approx(12.5)
        assert registro["intent"] == "rag_query"
        # La metadata previa no se pisa.
        assert conversacion.metadata_["origen"] == "webhook"
        assert resultado["requires_handoff"] is True

    @pytest.mark.parametrize(
        "reason",
        ["insufficient_context", "budget_exceeded", "human_request", "complaint"],
    )
    async def test_el_mensaje_al_contacto_depende_del_motivo(
        self, monkeypatch: pytest.MonkeyPatch, reason: str
    ) -> None:
        """Cada motivo tiene su propio aviso al contacto."""
        envios, _ = self._preparar(monkeypatch, FakeConversation())

        resultado = await handoff_module.human_handoff_node(
            estado(conversation_id=str(uuid.UUID(int=1)), handoff_reason=reason)
        )

        assert envios[0]["text"] == handoff_module.HANDOFF_MESSAGES[reason]
        assert resultado["response_text"] == handoff_module.HANDOFF_MESSAGES[reason]

    async def test_el_mensaje_configurado_por_el_tenant_gana(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`agent_configs.handoff_message` sustituye al texto por defecto."""
        envios, _ = self._preparar(monkeypatch, FakeConversation())
        parchear_agent_settings(
            monkeypatch,
            handoff_module,
            AgentSettings(model="gpt-4o", handoff_message="Te paso con Maria."),
        )

        await handoff_module.human_handoff_node(
            estado(conversation_id=str(uuid.UUID(int=1)), handoff_reason="complaint")
        )

        assert envios[0]["text"] == "Te paso con Maria."

    async def test_avisa_al_equipo_humano(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Se encola la notificacion con el motivo del handoff."""
        _, avisos = self._preparar(monkeypatch, FakeConversation())
        state = estado(conversation_id=str(uuid.UUID(int=1)), handoff_reason="human_request")

        await handoff_module.human_handoff_node(state)

        assert avisos[0][0] == "notify_handoff"
        assert avisos[0][1]["reason"] == "human_request"
        assert avisos[0][1]["conversation_id"] == state["conversation_id"]

    async def test_sin_conversacion_en_base_igual_avisa_al_contacto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Una conversacion inexistente se loguea, pero el contacto no queda mudo."""
        envios, _ = self._preparar(monkeypatch, None)

        resultado = await handoff_module.human_handoff_node(
            estado(conversation_id=str(uuid.UUID(int=1)))
        )

        assert envios[0]["text"] == handoff_module.HANDOFF_MESSAGES["insufficient_context"]
        assert resultado["handoff_reason"] == "insufficient_context"


class TestDelivery:
    """El envio compartido: proveedor correcto y mensaje guardado."""

    async def test_envia_por_el_proveedor_del_canal_y_guarda_el_mensaje(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WhatsApp sale por YCloud y queda registrado como saliente del bot."""
        conversation_id = uuid.UUID(int=7)
        conversacion = FakeConversation()
        sesion = FakeSession(objetos={conversation_id: conversacion})
        parchear_tenant_session(monkeypatch, delivery_module, sesion)

        enviados: list[dict[str, Any]] = []

        class FakeProvider:
            async def send_message(
                self, to: str, content: Any, channel_config: dict[str, Any]
            ) -> str:
                enviados.append({"to": to, "text": content.text, "config": channel_config})
                return "wamid.1"

        async def _identifier(client_id: Any, contact_id: Any, channel: str) -> str:
            return "573001112233"

        monkeypatch.setattr(delivery_module, "get_contact_identifier", _identifier)
        monkeypatch.setattr(
            delivery_module,
            "get_channel_config",
            lambda channel: ("ycloud", {"api_key": "k", "phone_number_id": "1"}),
        )
        monkeypatch.setattr(
            delivery_module, "get_messaging_provider", lambda nombre, config: FakeProvider()
        )

        external_id = await delivery_module.deliver_message(
            client_id=uuid.uuid4(),
            conversation_id=conversation_id,
            contact_id=uuid.uuid4(),
            channel="whatsapp",
            text="Hola",
        )

        assert external_id == "wamid.1"
        assert enviados[0]["to"] == "573001112233"
        mensajes = sesion.agregados_de(Message)
        assert mensajes[0].direction == "outbound"
        assert mensajes[0].sender_type == "bot"
        assert mensajes[0].content == "Hola"
        assert mensajes[0].external_message_id == "wamid.1"
        assert conversacion.last_message_at is not None


class TestChannelConfig:
    """Resolucion de canal a proveedor y credenciales."""

    def _settings(self, monkeypatch: pytest.MonkeyPatch, **valores: str) -> None:
        """Sustituye las settings que lee `get_channel_config`."""
        base = {
            "YCLOUD_API_KEY": "api-key",
            "YCLOUD_PHONE_NUMBER_ID": "573009999999",
            "META_PAGE_ACCESS_TOKEN": "page-token",
        }
        base.update(valores)
        monkeypatch.setattr(tenant_module, "get_settings", lambda: SimpleNamespace(**base))

    def test_whatsapp_sale_por_ycloud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """El canal de WhatsApp resuelve al proveedor YCloud con sus credenciales."""
        self._settings(monkeypatch)

        provider, config = tenant_module.get_channel_config("whatsapp")

        assert provider == "ycloud"
        assert config == {"api_key": "api-key", "phone_number_id": "573009999999"}

    @pytest.mark.parametrize("canal", ["instagram", "facebook"])
    def test_canales_de_meta(self, monkeypatch: pytest.MonkeyPatch, canal: str) -> None:
        """Instagram y Facebook resuelven a Meta, con el sub-canal dentro."""
        self._settings(monkeypatch)

        provider, config = tenant_module.get_channel_config(canal)

        assert provider == "meta"
        assert config["channel"] == canal

    def test_canal_desconocido(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un canal sin proveedor registrado falla con un mensaje claro."""
        self._settings(monkeypatch)

        with pytest.raises(ChannelNotConfiguredError, match="telegram"):
            tenant_module.get_channel_config("telegram")

    def test_credenciales_faltantes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin el numero de WhatsApp configurado no se intenta enviar."""
        self._settings(monkeypatch, YCLOUD_PHONE_NUMBER_ID="")

        with pytest.raises(ChannelNotConfiguredError, match="phone_number_id"):
            tenant_module.get_channel_config("whatsapp")
