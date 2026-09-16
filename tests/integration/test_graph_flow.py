"""Recorrido de los nodos del grafo contra PostgreSQL real, con RLS activo.

Requiere base de datos: `pytest tests/ --run-db`.

Cubre lo que los tests unitarios no pueden: que los nodos escriban de verdad en
el schema (estado de la conversacion, respuestas pendientes, contabilidad de
tokens) y que un tenant no vea nada de otro por esas mismas rutas.

Lo unico sustituido es lo que sale de la maquina: el proveedor de mensajeria, el
LLM y el retrieval vectorial. La base, RLS, el presupuesto y los nodos son los
reales.

El grafo en si (`app/agents/graph.py`) es entrega de Dev A: mientras no este, el
recorrido se encadena aqui a mano, en el mismo orden que define la spec
(`token_budget_check -> intent_routing -> rag_query -> respond | human_handoff`).
Cuando el grafo entre, estos tests siguen valiendo como verificacion de los nodos
y se les suma el `ainvoke()` completo.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.agents.nodes import _delivery as delivery_module
from app.agents.nodes import human_handoff as handoff_module
from app.agents.nodes import rag_query as rag_module
from app.agents.nodes import training_approval as training_module
from app.agents.nodes.token_budget import token_budget_check_node
from app.core.database import engine, tenant_session
from app.middleware.token_budget import TokenBudgetGuard
from app.services.rag import RetrievalResult

pytestmark = pytest.mark.db

# Redis no se sustituye a proposito: el job de integracion de CI levanta uno y el
# nodo de presupuesto lo usa como cache. Cada test siembra un tenant con UUID
# nuevo, asi que ninguna clave `token_budget:{client_id}` sobrevive de un test al
# siguiente. Si Redis faltara, el nodo degrada a consultar la base (cubierto en
# tests/unit/test_token_budget.py) y estos tests siguen pasando.

MES_ACTUAL = datetime.now(timezone.utc).strftime("%Y-%m")


class Escenario:
    """Ids de un tenant sembrado, para no andar pasando cinco UUIDs sueltos.

    Attributes:
        client_id: Tenant.
        contact_id: Contacto que escribe.
        conversation_id: Conversacion activa.
    """

    def __init__(self, client_id: uuid.UUID, contact_id: uuid.UUID, conversation_id: uuid.UUID):
        """Guarda los ids del escenario."""
        self.client_id = client_id
        self.contact_id = contact_id
        self.conversation_id = conversation_id

    def estado(self, **overrides: Any) -> dict[str, Any]:
        """Arma un estado de conversacion apuntando a este escenario.

        Args:
            **overrides: Campos a sobreescribir.

        Returns:
            Estado listo para pasarle a un nodo.
        """
        base: dict[str, Any] = {
            "client_id": str(self.client_id),
            "conversation_id": str(self.conversation_id),
            "contact_id": str(self.contact_id),
            "channel": "whatsapp",
            "message": {"text": "Cual es el horario?"},
            "model_to_use": "gpt-4o",
        }
        base.update(overrides)
        return base


async def _sembrar(
    slug: str,
    total_budget: int | None = None,
    used_tokens: int = 0,
    training_mode: bool = False,
) -> Escenario:
    """Crea un tenant completo: cliente, contacto, conversacion y configuracion.

    Args:
        slug: Slug del tenant (unico).
        total_budget: Presupuesto del mes; None = sin fila de presupuesto.
        used_tokens: Tokens ya consumidos.
        training_mode: Si el agente queda en modo entrenamiento.

    Returns:
        Escenario con los ids sembrados.
    """
    client_id = uuid.uuid4()
    contact_id = uuid.uuid4()
    conversation_id = uuid.uuid4()

    async with tenant_session(client_id) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active) "
                "VALUES (:id, :name, :slug, 'free', true)"
            ),
            {"id": str(client_id), "name": f"Tenant {slug}", "slug": slug},
        )
        await session.execute(
            text(
                "INSERT INTO contacts (id, client_id, display_name) "
                "VALUES (:id, :cid, 'Contacto de prueba')"
            ),
            {"id": str(contact_id), "cid": str(client_id)},
        )
        await session.execute(
            text(
                "INSERT INTO contact_identifiers (client_id, contact_id, channel, "
                "identifier_value) VALUES (:cid, :contact, 'whatsapp', :valor)"
            ),
            {
                "cid": str(client_id),
                "contact": str(contact_id),
                "valor": f"57300{uuid.uuid4().int % 10_000_000:07d}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO conversations (id, client_id, contact_id, channel, status) "
                "VALUES (:id, :cid, :contact, 'whatsapp', 'bot_active')"
            ),
            {"id": str(conversation_id), "cid": str(client_id), "contact": str(contact_id)},
        )
        await session.execute(
            text(
                "INSERT INTO agent_configs (client_id, name, model, temperature, "
                "training_mode, similarity_threshold, config, is_active) "
                "VALUES (:cid, 'Asistente', 'gpt-4o', 0.3, :training, 0.80, "
                '\'{"enabled_agents": ["rag"]}\'::jsonb, true)'
            ),
            {"cid": str(client_id), "training": training_mode},
        )
        if total_budget is not None:
            await session.execute(
                text(
                    "INSERT INTO token_budgets (client_id, month, total_budget, used_tokens, "
                    "model_default) VALUES (:cid, :mes, :total, :usados, 'gpt-4o')"
                ),
                {
                    "cid": str(client_id),
                    "mes": MES_ACTUAL,
                    "total": total_budget,
                    "usados": used_tokens,
                },
            )

    return Escenario(client_id, contact_id, conversation_id)


async def _limpiar(client_id: uuid.UUID) -> None:
    """Borra el tenant y todo lo que cuelga de el, en orden de FKs.

    Args:
        client_id: Tenant a borrar.
    """
    async with tenant_session(client_id) as session:
        for tabla in (
            "token_usage_logs",
            "token_budgets",
            "pending_responses",
            "messages",
            "conversations",
            "contact_identifiers",
            "contacts",
            "agent_configs",
        ):
            await session.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :cid"),  # noqa: S608
                {"cid": str(client_id)},
            )
        await session.execute(text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)})


@pytest_asyncio.fixture
async def escenario() -> AsyncGenerator[Escenario, None]:
    """Siembra un tenant con presupuesto holgado y lo limpia al terminar.

    `engine.dispose()` antes de empezar: el engine es un singleton de modulo y
    pytest-asyncio abre un event loop por test, asi que una conexion del pool
    abierta en el loop de otro test revienta con "attached to a different loop".
    """
    await engine.dispose()
    datos = await _sembrar(f"graph-{uuid.uuid4().hex[:8]}", total_budget=100_000)
    yield datos
    await _limpiar(datos.client_id)


@pytest.fixture
def envios(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Sustituye el proveedor de mensajeria; el resto del envio es real.

    El mensaje saliente se sigue guardando en `messages` contra la base real.
    """
    enviados: list[dict[str, Any]] = []

    class FakeProvider:
        async def send_message(self, to: str, content: Any, channel_config: Any) -> str:
            enviados.append({"to": to, "text": content.text})
            return f"ext-{len(enviados)}"

    monkeypatch.setattr(
        delivery_module,
        "get_channel_config",
        lambda channel: ("ycloud", {"api_key": "k", "phone_number_id": "1"}),
    )
    monkeypatch.setattr(
        delivery_module, "get_messaging_provider", lambda nombre, config: FakeProvider()
    )
    return enviados


async def _contar(client_id: uuid.UUID, sql: str, params: dict[str, Any]) -> int:
    """Cuenta filas visibles para un tenant.

    Args:
        client_id: Tenant cuyo contexto RLS se aplica.
        sql: Consulta de conteo.
        params: Parametros de la consulta.

    Returns:
        El numero devuelto por la consulta.
    """
    async with tenant_session(client_id) as session:
        return int((await session.execute(text(sql), params)).scalar_one())


# ─── Presupuesto contra la base real ─────────────────────────────────────────


class TestPresupuesto:
    """El nodo de presupuesto lee `token_budgets` de verdad."""

    async def test_presupuesto_holgado_deja_el_modelo_del_tenant(
        self, escenario: Escenario
    ) -> None:
        """Con margen se sigue con el modelo de `agent_configs`."""
        resultado = await token_budget_check_node(escenario.estado())

        assert resultado["budget_status"] == "ok"
        assert resultado["model_to_use"] == "gpt-4o"

    async def test_presupuesto_agotado_escala(self) -> None:
        """Con el presupuesto consumido, el nodo marca handoff sin LLM."""
        await engine.dispose()
        datos = await _sembrar(
            f"graph-{uuid.uuid4().hex[:8]}", total_budget=1_000, used_tokens=1_000
        )
        try:
            resultado = await token_budget_check_node(datos.estado())

            assert resultado["budget_status"] == "exceeded"
            assert resultado["handoff_reason"] == "budget_exceeded"
        finally:
            await _limpiar(datos.client_id)

    async def test_el_consumo_registrado_suma_al_presupuesto(self, escenario: Escenario) -> None:
        """`record_usage` deja el log y actualiza `token_budgets` en la base."""
        await TokenBudgetGuard.record_usage(
            client_id=escenario.client_id,
            conversation_id=escenario.conversation_id,
            model="gpt-4o",
            prompt_tokens=400,
            completion_tokens=100,
            operation="rag_query",
        )

        logs = await _contar(
            escenario.client_id,
            "SELECT count(*) FROM token_usage_logs WHERE client_id = :cid",
            {"cid": str(escenario.client_id)},
        )
        usados = await _contar(
            escenario.client_id,
            "SELECT used_tokens FROM token_budgets WHERE client_id = :cid AND month = :mes",
            {"cid": str(escenario.client_id), "mes": MES_ACTUAL},
        )

        assert logs == 1
        assert usados == 500


# ─── Handoff y modo entrenamiento ────────────────────────────────────────────


class TestHandoff:
    """El escalado deja rastro consultable en la conversacion."""

    async def test_deja_la_conversacion_en_waiting_human_con_el_motivo(
        self, escenario: Escenario, envios: list[dict[str, Any]]
    ) -> None:
        """Estado, metadata del handoff y mensaje saliente, todos en la base."""
        await handoff_module.human_handoff_node(
            escenario.estado(handoff_reason="insufficient_context", rag_confidence=0.3)
        )

        async with tenant_session(escenario.client_id) as session:
            fila = (
                await session.execute(
                    text(
                        "SELECT status, metadata FROM conversations WHERE id = :id",
                    ),
                    {"id": str(escenario.conversation_id)},
                )
            ).one()

        salientes = await _contar(
            escenario.client_id,
            "SELECT count(*) FROM messages WHERE conversation_id = :cid AND direction = 'outbound'",
            {"cid": str(escenario.conversation_id)},
        )

        assert fila.status == "waiting_human"
        assert fila.metadata["handoff"]["reason"] == "insufficient_context"
        assert fila.metadata["handoff"]["rag_confidence"] == pytest.approx(0.3)
        assert salientes == 1
        assert envios[0]["text"] == handoff_module.HANDOFF_MESSAGES["insufficient_context"]


class TestModoEntrenamiento:
    """La respuesta candidata queda pendiente y no se envia."""

    async def test_guarda_la_respuesta_en_pending_responses(
        self, envios: list[dict[str, Any]]
    ) -> None:
        """La respuesta real no sale; queda a la espera de aprobacion."""
        await engine.dispose()
        datos = await _sembrar(
            f"graph-{uuid.uuid4().hex[:8]}", total_budget=100_000, training_mode=True
        )
        try:
            await training_module.training_approval_node(
                datos.estado(response_text="Atendemos de 8 a 18.")
            )

            async with tenant_session(datos.client_id) as session:
                fila = (
                    await session.execute(
                        text(
                            "SELECT question, generated_response, status FROM pending_responses "
                            "WHERE conversation_id = :cid"
                        ),
                        {"cid": str(datos.conversation_id)},
                    )
                ).one()

            assert fila.generated_response == "Atendemos de 8 a 18."
            assert fila.status == "pending"
            assert envios[0]["text"] == training_module.WAITING_MESSAGE
        finally:
            await _limpiar(datos.client_id)


# ─── Aislamiento entre tenants ───────────────────────────────────────────────


class TestAislamiento:
    """Lo que escriben los nodos de un tenant no lo ve el otro."""

    async def test_un_tenant_no_ve_el_consumo_ni_lo_pendiente_del_otro(
        self, envios: list[dict[str, Any]]
    ) -> None:
        """RLS corta el acceso cruzado en las tablas que tocan los nodos."""
        await engine.dispose()
        tenant_a = await _sembrar(f"graph-a-{uuid.uuid4().hex[:8]}", total_budget=50_000)
        tenant_b = await _sembrar(f"graph-b-{uuid.uuid4().hex[:8]}", total_budget=50_000)
        try:
            await TokenBudgetGuard.record_usage(
                client_id=tenant_a.client_id,
                conversation_id=tenant_a.conversation_id,
                model="gpt-4o",
                prompt_tokens=10,
                completion_tokens=10,
                operation="rag_query",
            )
            await training_module.training_approval_node(
                tenant_a.estado(response_text="Respuesta del tenant A")
            )

            propios = await _contar(tenant_a.client_id, "SELECT count(*) FROM token_usage_logs", {})
            ajenos = await _contar(tenant_b.client_id, "SELECT count(*) FROM token_usage_logs", {})
            pendientes_ajenos = await _contar(
                tenant_b.client_id, "SELECT count(*) FROM pending_responses", {}
            )

            assert propios == 1
            assert ajenos == 0
            assert pendientes_ajenos == 0
        finally:
            await _limpiar(tenant_a.client_id)
            await _limpiar(tenant_b.client_id)


# ─── Recorrido completo, con el LLM y el retrieval sustituidos ───────────────


class TestRecorrido:
    """La cadena de nodos, en el orden del grafo, contra la base real."""

    async def test_sin_contexto_el_recorrido_termina_en_un_humano(
        self,
        escenario: Escenario,
        envios: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Presupuesto ok + RAG sin contexto = conversacion escalada."""

        class SinContexto:
            async def retrieve(self, **kwargs: Any) -> list[RetrievalResult]:
                return []

            async def retrieve_few_shot_examples(self, **kwargs: Any) -> list[dict[str, Any]]:
                return []

            def build_grounded_prompt(self, **kwargs: Any) -> str:
                return ""

        monkeypatch.setattr(rag_module, "build_rag_service", lambda: SinContexto())

        state = escenario.estado()
        state.update(await token_budget_check_node(state))
        assert state["budget_status"] == "ok"

        state.update(await rag_module.rag_query_node(state))
        assert state["requires_handoff"] is True

        await handoff_module.human_handoff_node(state)

        estado_final = await _contar(
            escenario.client_id,
            "SELECT count(*) FROM conversations WHERE id = :id AND status = 'waiting_human'",
            {"id": str(escenario.conversation_id)},
        )
        assert estado_final == 1
        assert envios[0]["text"] == handoff_module.HANDOFF_MESSAGES["insufficient_context"]

    async def test_el_presupuesto_agotado_corta_antes_de_buscar(
        self, envios: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con el presupuesto al 100% no se llega ni a consultar el knowledge base."""
        await engine.dispose()
        datos = await _sembrar(f"graph-{uuid.uuid4().hex[:8]}", total_budget=500, used_tokens=600)
        buscado: list[bool] = []

        class NoDeberiaBuscar:
            async def retrieve(self, **kwargs: Any) -> list[RetrievalResult]:
                buscado.append(True)
                return []

        monkeypatch.setattr(rag_module, "build_rag_service", lambda: NoDeberiaBuscar())

        try:
            state = datos.estado()
            state.update(await token_budget_check_node(state))

            assert state["requires_handoff"] is True
            await handoff_module.human_handoff_node(state)

            escalada = await _contar(
                datos.client_id,
                "SELECT count(*) FROM conversations WHERE id = :id AND status = 'waiting_human'",
                {"id": str(datos.conversation_id)},
            )
            assert escalada == 1
            assert buscado == []
            assert envios[0]["text"] == handoff_module.HANDOFF_MESSAGES["budget_exceeded"]
        finally:
            await _limpiar(datos.client_id)
