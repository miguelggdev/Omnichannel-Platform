"""Tests del CRUD de conversaciones y de su ciclo de vida via API.

La base se sustituye por `CrmSession`: corren sin `--run-db`. Las reglas de la
maquina de estados se prueban aparte en `test_conversation_lifecycle.py`; aqui
se comprueba que el endpoint las aplique y traduzca a los codigos HTTP.
"""

import uuid
from typing import Any

import pytest

from app.api.v1 import conversations as conversations_module
from tests.unit.agent_doubles import fake_tenant_session
from tests.unit.crm_doubles import CrmSession, FakeConversation, FakeMessage, FakeUser

URL = "/api/v1/conversations"


def _usa_sesion(monkeypatch: pytest.MonkeyPatch, session: CrmSession) -> CrmSession:
    """Hace que los endpoints de conversaciones usen la sesion falsa indicada."""
    monkeypatch.setattr(conversations_module, "tenant_session", fake_tenant_session(session))
    return session


# ─── GET /conversations ──────────────────────────────────────────────────────


class TestListado:
    """Listado paginado de conversaciones."""

    async def test_devuelve_la_pagina_y_el_total(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La respuesta trae las conversaciones y el total."""
        _usa_sesion(
            monkeypatch,
            CrmSession(resultados=[[FakeConversation(), FakeConversation()]], escalares=[2]),
        )

        response = await authenticated_client.get(URL)

        assert response.status_code == 200
        assert response.json()["total"] == 2
        assert len(response.json()["items"]) == 2

    @pytest.mark.parametrize(
        "status",
        [
            "new",
            "bot_active",
            "human_active",
            "waiting_human",
            "waiting_client",
            "resolved",
            "archived",
        ],
    )
    async def test_acepta_los_siete_estados(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, status: str
    ) -> None:
        """Los 7 estados del modelo son filtros validos."""
        _usa_sesion(monkeypatch, CrmSession(resultados=[[]], escalares=[0]))

        response = await authenticated_client.get(URL, params={"status": status})

        assert response.status_code == 200

    async def test_un_status_inventado_es_400(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un filtro por un estado que no existe se rechaza antes de consultar."""
        _usa_sesion(monkeypatch, CrmSession())

        response = await authenticated_client.get(URL, params={"status": "zombie"})

        assert response.status_code == 400
        assert response.json()["error_code"] == "VALIDATION_ERROR"

    async def test_filtra_por_canal_y_por_agente(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Los tres filtros se combinan en el mismo WHERE."""
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[[]], escalares=[0]))
        agente = uuid.uuid4()

        await authenticated_client.get(
            URL,
            params={"status": "bot_active", "channel": "whatsapp", "assigned_user_id": str(agente)},
        )

        sql = str(session.executed[0]).lower()
        assert "conversations.status =" in sql
        assert "conversations.channel =" in sql
        assert "conversations.assigned_user_id =" in sql

    async def test_el_client_id_va_explicito_en_el_where(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El filtro de tenant no queda delegado solo a la RLS (CLAUDE.md §2)."""
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[[]], escalares=[0]))

        await authenticated_client.get(URL)

        assert "conversations.client_id =" in str(session.executed[0])


# ─── GET /conversations/{id} ─────────────────────────────────────────────────


class TestDetalle:
    """Detalle con mensajes paginados."""

    async def test_devuelve_los_mensajes_y_el_total(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El detalle incluye la pagina de mensajes y cuantos hay en total."""
        conv = FakeConversation()
        mensajes = [FakeMessage(content="hola"), FakeMessage(content="que tal")]
        _usa_sesion(monkeypatch, CrmSession(resultados=[conv, mensajes], escalares=[7]))

        response = await authenticated_client.get(f"{URL}/{conv.id}")

        assert response.status_code == 200
        cuerpo = response.json()
        assert cuerpo["total_messages"] == 7
        assert [m["content"] for m in cuerpo["messages"]] == ["hola", "que tal"]

    async def test_los_mensajes_salen_en_orden_cronologico(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Una bandeja se lee de arriba hacia abajo: created_at ASC."""
        conv = FakeConversation()
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[conv, []], escalares=[0]))

        await authenticated_client.get(f"{URL}/{conv.id}")

        sql = str(session.executed[-1]).lower()
        assert "order by messages.created_at asc" in sql

    async def test_conversacion_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un id que no existe para este tenant responde 404."""
        _usa_sesion(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.get(f"{URL}/{uuid.uuid4()}")

        assert response.status_code == 404

    async def test_page_size_de_mensajes_llega_a_200(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Los mensajes admiten paginas mas grandes que el resto de listados."""
        conv = FakeConversation()
        _usa_sesion(monkeypatch, CrmSession(resultados=[conv, [], conv, []], escalares=[0, 0]))

        ok = await authenticated_client.get(f"{URL}/{conv.id}", params={"page_size": 200})
        pasado = await authenticated_client.get(f"{URL}/{conv.id}", params={"page_size": 201})

        assert ok.status_code == 200
        assert pasado.status_code == 422


# ─── PUT /conversations/{id}/assign ──────────────────────────────────────────


class TestAsignacion:
    """Asignacion a un agente humano."""

    async def test_asignar_transiciona_a_human_active(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, tenant_a_id: uuid.UUID
    ) -> None:
        """Asignar una conversacion del bot la pone en manos de la persona."""
        conv = FakeConversation(status="bot_active", client_id=tenant_a_id)
        agente = FakeUser(client_id=tenant_a_id)
        _usa_sesion(monkeypatch, CrmSession(resultados=[conv, agente]))

        response = await authenticated_client.put(
            f"{URL}/{conv.id}/assign", json={"user_id": str(agente.id)}
        )

        assert response.status_code == 200
        assert conv.status == "human_active"
        assert conv.assigned_user_id == agente.id

    async def test_reasignar_una_ya_humana_no_cambia_el_estado(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, tenant_a_id: uuid.UUID
    ) -> None:
        """Pasar la conversacion a otro companero no es una transicion de estado.

        `human_active -> human_active` no esta en la tabla; si el endpoint
        llamara a transition() sin este caso aparte, reasignar daria un 400.
        """
        conv = FakeConversation(status="human_active", client_id=tenant_a_id)
        nuevo = FakeUser(client_id=tenant_a_id)
        _usa_sesion(monkeypatch, CrmSession(resultados=[conv, nuevo]))

        response = await authenticated_client.put(
            f"{URL}/{conv.id}/assign", json={"user_id": str(nuevo.id)}
        )

        assert response.status_code == 200
        assert conv.status == "human_active"
        assert conv.assigned_user_id == nuevo.id

    async def test_no_se_puede_asignar_una_archivada(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, tenant_a_id: uuid.UUID
    ) -> None:
        """Desde archived no hay transicion posible: 400."""
        conv = FakeConversation(status="archived", client_id=tenant_a_id)
        agente = FakeUser(client_id=tenant_a_id)
        _usa_sesion(monkeypatch, CrmSession(resultados=[conv, agente]))

        response = await authenticated_client.put(
            f"{URL}/{conv.id}/assign", json={"user_id": str(agente.id)}
        )

        assert response.status_code == 400
        assert conv.status == "archived"

    async def test_agente_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No se asigna a un usuario que no existe en este tenant."""
        conv = FakeConversation(status="bot_active")
        _usa_sesion(monkeypatch, CrmSession(resultados=[conv, None]))

        response = await authenticated_client.put(
            f"{URL}/{conv.id}/assign", json={"user_id": str(uuid.uuid4())}
        )

        assert response.status_code == 404
        assert "Agente" in response.json()["message"]

    async def test_el_agente_se_busca_dentro_del_tenant(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin el client_id en el WHERE, un admin podria asignar a otro tenant."""
        conv = FakeConversation(status="bot_active")
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[conv, FakeUser()]))

        await authenticated_client.put(
            f"{URL}/{conv.id}/assign", json={"user_id": str(uuid.uuid4())}
        )

        sql_agente = str(session.executed[1]).lower()
        assert "users.client_id =" in sql_agente

    async def test_un_agente_desactivado_es_400(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, tenant_a_id: uuid.UUID
    ) -> None:
        """No tiene sentido mandarle trabajo a una cuenta dada de baja."""
        conv = FakeConversation(status="bot_active", client_id=tenant_a_id)
        agente = FakeUser(client_id=tenant_a_id, is_active=False)
        _usa_sesion(monkeypatch, CrmSession(resultados=[conv, agente]))

        response = await authenticated_client.put(
            f"{URL}/{conv.id}/assign", json={"user_id": str(agente.id)}
        )

        assert response.status_code == 400

    async def test_el_rol_agent_no_puede_asignar(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repartir trabajo es de admin, supervisor o super_admin."""
        _usa_sesion(monkeypatch, CrmSession())
        client = authenticated_client_factory(role="agent")

        response = await client.put(
            f"{URL}/{uuid.uuid4()}/assign", json={"user_id": str(uuid.uuid4())}
        )

        assert response.status_code == 403


# ─── PUT /conversations/{id}/status ──────────────────────────────────────────


class TestCambioDeEstado:
    """Cambio manual de estado."""

    async def test_transicion_valida_responde_200(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un salto permitido aplica el estado nuevo."""
        conv = FakeConversation(status="bot_active")
        _usa_sesion(monkeypatch, CrmSession(resultados=[conv]))

        response = await authenticated_client.put(
            f"{URL}/{conv.id}/status", json={"status": "resolved"}
        )

        assert response.status_code == 200
        assert conv.status == "resolved"
        assert conv.resolved_at is not None

    async def test_la_respuesta_trae_el_estado_anterior_de_verdad(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`previous_status` es el de antes, no el nuevo.

        Leerlo del objeto despues de transition() devolveria el estado nuevo en
        los dos campos.
        """
        conv = FakeConversation(status="bot_active")
        _usa_sesion(monkeypatch, CrmSession(resultados=[conv]))

        response = await authenticated_client.put(
            f"{URL}/{conv.id}/status", json={"status": "waiting_human"}
        )

        cuerpo = response.json()
        assert cuerpo["previous_status"] == "bot_active"
        assert cuerpo["status"] == "waiting_human"

    async def test_la_respuesta_lista_los_saltos_siguientes(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El cliente sabe a donde puede ir despues sin consultar la spec."""
        conv = FakeConversation(status="bot_active")
        _usa_sesion(monkeypatch, CrmSession(resultados=[conv]))

        response = await authenticated_client.put(
            f"{URL}/{conv.id}/status", json={"status": "resolved"}
        )

        assert response.json()["valid_next_transitions"] == ["archived"]

    async def test_transicion_invalida_es_400(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un salto fuera de la tabla no toca la conversacion."""
        conv = FakeConversation(status="new")
        _usa_sesion(monkeypatch, CrmSession(resultados=[conv]))

        response = await authenticated_client.put(
            f"{URL}/{conv.id}/status", json={"status": "archived"}
        )

        assert response.status_code == 400
        assert conv.status == "new"

    async def test_un_status_inventado_es_400(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un estado que la base no acepta se corta antes de tocar el objeto."""
        conv = FakeConversation(status="new")
        _usa_sesion(monkeypatch, CrmSession(resultados=[conv]))

        response = await authenticated_client.put(
            f"{URL}/{conv.id}/status", json={"status": "zombie"}
        )

        assert response.status_code == 400
        assert conv.status == "new"

    async def test_tomarla_como_humano_autoasigna_al_solicitante(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pasar a human_active deja la conversacion a nombre de quien la pidio."""
        conv = FakeConversation(status="bot_active", assigned_user_id=None)
        _usa_sesion(monkeypatch, CrmSession(resultados=[conv]))

        response = await authenticated_client.put(
            f"{URL}/{conv.id}/status", json={"status": "human_active"}
        )

        assert response.status_code == 200
        assert conv.assigned_user_id is not None

    async def test_conversacion_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cambiar el estado de lo que no existe responde 404."""
        _usa_sesion(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.put(
            f"{URL}/{uuid.uuid4()}/status", json={"status": "resolved"}
        )

        assert response.status_code == 404


# ─── Autenticacion ───────────────────────────────────────────────────────────


class TestAutenticacion:
    """Todos los endpoints exigen token."""

    async def test_listar_sin_token_es_401(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin Authorization no se llega ni a abrir la sesion."""
        _usa_sesion(monkeypatch, CrmSession())

        response = await api_client.get(URL)

        assert response.status_code == 401

    async def test_cambiar_estado_sin_token_es_401(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tampoco se puede mover una conversacion sin identificarse."""
        _usa_sesion(monkeypatch, CrmSession())

        response = await api_client.put(f"{URL}/{uuid.uuid4()}/status", json={"status": "resolved"})

        assert response.status_code == 401
