"""`POST /api/v1/conversations/{id}/messages`: una persona contesta desde el panel.

La base y el envio se sustituyen por dobles: corren sin `--run-db`. Lo que se prueba
es la politica (quien puede contestar, que estados lo admiten, que pasa con el
estado y la asignacion) y los codigos HTTP. El envio real contra PostgreSQL esta en
`tests/integration/test_conversation_reply.py`.
"""

import uuid
from typing import Any

import pytest

from app.agents.nodes._tenant import ChannelNotConfiguredError, ContactIdentifierNotFoundError
from app.api.v1 import conversations as conversations_module
from tests.unit.agent_doubles import fake_tenant_session
from tests.unit.crm_doubles import CrmSession, FakeConversation, FakeMessage

URL = "/api/v1/conversations"


class Envios:
    """Registra las llamadas a `deliver_message` y, si se pide, falla."""

    def __init__(self, conversation: FakeConversation, error: Exception | None = None) -> None:
        self.llamadas: list[dict[str, Any]] = []
        self.estado_al_enviar: list[str] = []
        self._conversation = conversation
        self._error = error

    async def __call__(self, *args: Any, **kwargs: Any) -> str | None:
        # El estado al enviar prueba que la persona toma la conversacion ANTES.
        self.estado_al_enviar.append(self._conversation.status)
        self.llamadas.append({"args": args, **kwargs})
        if self._error is not None:
            raise self._error
        return "ext-1"


def _preparar(
    monkeypatch: pytest.MonkeyPatch,
    conversation: FakeConversation,
    error: Exception | None = None,
) -> tuple[CrmSession, Envios]:
    mensaje = FakeMessage(
        direction="outbound", content="Hola, soy una persona", sender_type="agent"
    )
    sesion = CrmSession(resultados=[conversation, mensaje])
    envios = Envios(conversation, error)
    monkeypatch.setattr(conversations_module, "tenant_session", fake_tenant_session(sesion))
    monkeypatch.setattr(conversations_module, "deliver_message", envios)
    return sesion, envios


async def _post(cliente: Any, texto: str = "Hola, soy una persona") -> Any:
    return await cliente.post(f"{URL}/{uuid.uuid4()}/messages", json={"text": texto})


class TestContestar:
    async def test_envia_como_agent_y_toma_la_conversacion(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        yo = uuid.uuid4()
        conv = FakeConversation(status="bot_active")
        _, envios = _preparar(monkeypatch, conv)
        cliente = authenticated_client_factory(role="agent", user_id=yo)

        response = await _post(cliente)

        assert response.status_code == 201, response.text
        assert response.json()["sender_type"] == "agent"
        assert response.json()["direction"] == "outbound"
        llamada = envios.llamadas[0]
        assert llamada["sender_type"] == "agent"
        assert llamada["sender_id"] == yo
        assert llamada["args"][4] == "Hola, soy una persona"
        assert conv.status == "human_active"
        assert conv.assigned_user_id == yo

    async def test_la_conversacion_se_toma_antes_de_enviar(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si no, el bot podria contestar a la vez que la persona."""
        conv = FakeConversation(status="waiting_human")
        _, envios = _preparar(monkeypatch, conv)

        await _post(authenticated_client_factory(role="agent"))

        assert envios.estado_al_enviar == ["human_active"]

    async def test_en_human_active_sin_dueno_la_asigna_a_quien_contesta(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        yo = uuid.uuid4()
        conv = FakeConversation(status="human_active", assigned_user_id=None)
        _preparar(monkeypatch, conv)

        response = await _post(authenticated_client_factory(role="agent", user_id=yo))

        assert response.status_code == 201
        assert conv.assigned_user_id == yo

    async def test_si_ya_es_mia_no_cambia_nada(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        yo = uuid.uuid4()
        conv = FakeConversation(status="human_active", assigned_user_id=yo)
        _preparar(monkeypatch, conv)

        response = await _post(authenticated_client_factory(role="agent", user_id=yo))

        assert response.status_code == 201
        assert (conv.status, conv.assigned_user_id) == ("human_active", yo)

    @pytest.mark.parametrize("estado", ["new", "bot_active", "waiting_human", "waiting_client"])
    async def test_desde_cualquier_estado_abierto_se_puede_contestar(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch, estado: str
    ) -> None:
        conv = FakeConversation(status=estado)
        _preparar(monkeypatch, conv)

        response = await _post(authenticated_client_factory(role="agent"))

        assert response.status_code == 201
        assert conv.status == "human_active"


class TestQuienPuede:
    async def test_un_agent_no_contesta_lo_de_otra_persona(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conv = FakeConversation(status="human_active", assigned_user_id=uuid.uuid4())
        _, envios = _preparar(monkeypatch, conv)

        response = await _post(authenticated_client_factory(role="agent", user_id=uuid.uuid4()))

        assert response.status_code == 403
        assert response.json()["error_code"] == "FORBIDDEN"
        assert envios.llamadas == []

    @pytest.mark.parametrize("rol", ["supervisor", "admin", "super_admin"])
    async def test_supervisor_y_admin_si_pueden_contestar_lo_de_otra_persona(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch, rol: str
    ) -> None:
        otra = uuid.uuid4()
        conv = FakeConversation(status="human_active", assigned_user_id=otra)
        _, envios = _preparar(monkeypatch, conv)

        response = await _post(authenticated_client_factory(role=rol))

        assert response.status_code == 201
        assert len(envios.llamadas) == 1

    async def test_el_rol_medical_no_contesta_conversaciones(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conv = FakeConversation()
        _, envios = _preparar(monkeypatch, conv)

        response = await _post(authenticated_client_factory(role="medical"))

        assert response.status_code == 403
        assert envios.llamadas == []


class TestEstadosCerrados:
    @pytest.mark.parametrize("estado", ["resolved", "archived"])
    async def test_una_conversacion_cerrada_no_admite_respuesta(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch, estado: str
    ) -> None:
        conv = FakeConversation(status=estado)
        _, envios = _preparar(monkeypatch, conv)

        response = await _post(authenticated_client_factory(role="admin"))

        assert response.status_code == 409
        assert response.json()["error_code"] == "CONFLICT"
        assert envios.llamadas == []
        assert conv.status == estado

    async def test_una_conversacion_inexistente_es_404(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sesion = CrmSession(resultados=[None])
        monkeypatch.setattr(conversations_module, "tenant_session", fake_tenant_session(sesion))
        envios = Envios(FakeConversation())
        monkeypatch.setattr(conversations_module, "deliver_message", envios)

        response = await _post(authenticated_client_factory(role="admin"))

        assert response.status_code == 404
        assert envios.llamadas == []


class TestFalloDelEnvio:
    @pytest.mark.parametrize(
        "error",
        [
            ContactIdentifierNotFoundError("sin identificador"),
            ChannelNotConfiguredError("sin canal"),
        ],
    )
    async def test_contacto_o_canal_no_alcanzable_es_409(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        _preparar(monkeypatch, FakeConversation(), error=error)

        response = await _post(authenticated_client_factory(role="admin"))

        assert response.status_code == 409
        assert response.json()["error_code"] == "CONFLICT"

    async def test_el_rechazo_del_proveedor_es_502_sin_filtrar_detalles(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secreto = "https://api.ycloud.example/v2?token=SECRETO"
        _preparar(monkeypatch, FakeConversation(), error=RuntimeError(secreto))

        response = await _post(authenticated_client_factory(role="admin"))

        assert response.status_code == 502
        assert response.json()["error_code"] == "DELIVERY_FAILED"
        assert "SECRETO" not in response.text


class TestValidacion:
    @pytest.mark.parametrize("cuerpo", [{"text": ""}, {"text": "   \n "}, {}, {"text": "x" * 4001}])
    async def test_texto_vacio_o_enorme_es_422(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        cuerpo: dict[str, Any],
    ) -> None:
        _, envios = _preparar(monkeypatch, FakeConversation())

        response = await authenticated_client_factory(role="admin").post(
            f"{URL}/{uuid.uuid4()}/messages", json=cuerpo
        )

        assert response.status_code == 422
        assert envios.llamadas == []

    async def test_recorta_los_espacios_de_los_extremos(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, envios = _preparar(monkeypatch, FakeConversation())

        await _post(authenticated_client_factory(role="admin"), texto="  hola  ")

        assert envios.llamadas[0]["args"][4] == "hola"
