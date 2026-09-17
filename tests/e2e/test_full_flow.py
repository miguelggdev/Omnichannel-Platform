"""Recorrido completo: webhook entrante -> dedup -> grafo -> respuesta enviada.

Requiere base de datos: `pytest tests/e2e/ --run-db`.

Es el criterio de aceptacion #7 del Sprint 8 y la unica prueba que atraviesa
todas las capas de una vez. Cada pieza ya tiene sus tests propios; lo que esto
verifica es que encajen: que el mensaje que entra por HTTP acabe convertido en
una respuesta guardada y enviada por el mismo canal, con su rastro de auditoria.

Que es real y que esta sustituido
---------------------------------
Real: el endpoint HTTP con su firma HMAC, la deduplicacion, PostgreSQL con RLS,
el worker de webhooks, el grafo de LangGraph con su checkpointer, los nodos y el
rastro de auditoria.

Sustituido, porque sale de la maquina: el LLM de OpenAI, el retrieval vectorial
(los embeddings los calcularia el mismo LLM) y el envio al proveedor de
mensajeria. Lo enviado se captura para poder afirmar que se envio por el canal
correcto y con el texto correcto.

Celery tampoco corre: el job de CI no levanta ni worker ni broker. El `.delay()`
del endpoint se sustituye por un doble que captura el encolado, y las dos tareas
se invocan como funciones en el mismo orden en que las encadenaria el broker. Eso
mantiene el recorrido intacto salvo el transporte, que ya cubren los tests de
`tests/unit/test_celery_app.py`.
"""

import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.agents.nodes import _delivery as delivery_module
from app.agents.nodes import intent_router as intent_module
from app.agents.nodes import rag_query as rag_module
from app.agents.nodes.intent_router import IntentClassification
from app.core.config import get_settings
from app.core.database import engine, tenant_session
from app.main import create_app
from app.services.rag import RetrievalResult
from app.tasks import webhook_processor as webhook_module
from tests.unit.agent_doubles import FakeChatModel, RespuestaLLM, parchear_chat_model

pytestmark = pytest.mark.db

PREGUNTA = "Cual es el horario de atencion?"
RESPUESTA_DEL_BOT = "Atendemos de lunes a viernes, de 9 a 18."
TELEFONO = "573001234567"


class Escenario:
    """Tenant sembrado y listo para recibir un mensaje.

    Attributes:
        client_id: Tenant.
        enviados: Mensajes que el sistema intento enviar por el canal.
    """

    def __init__(self, client_id: uuid.UUID, enviados: list[dict[str, Any]]) -> None:
        """Guarda el tenant y el buzon de salida capturado."""
        self.client_id = client_id
        self.enviados = enviados


async def _sembrar(client_id: uuid.UUID, slug: str) -> None:
    """Crea el tenant con presupuesto de tokens y un agente RAG activo.

    Args:
        client_id: Tenant a crear.
        slug: Slug unico.
    """
    from datetime import datetime, timezone

    mes = datetime.now(timezone.utc).strftime("%Y-%m")

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
                "INSERT INTO agent_configs (client_id, name, model, temperature, "
                "training_mode, similarity_threshold, config, is_active) "
                "VALUES (:cid, 'Asistente', 'gpt-4o', 0.3, false, 0.80, "
                '\'{"enabled_agents": ["rag"]}\'::jsonb, true)'
            ),
            {"cid": str(client_id)},
        )
        await session.execute(
            text(
                "INSERT INTO token_budgets (client_id, month, total_budget, used_tokens, "
                "model_default) VALUES (:cid, :mes, 1000000, 0, 'gpt-4o')"
            ),
            {"cid": str(client_id), "mes": mes},
        )


async def _limpiar(client_id: uuid.UUID) -> None:
    """Borra el tenant y todo lo que cuelga de el, en orden de FKs.

    Args:
        client_id: Tenant a borrar.
    """
    async with tenant_session(client_id) as session:
        for tabla in (
            "audit_logs",
            "token_usage_logs",
            "token_budgets",
            "pending_responses",
            "messages",
            "conversations",
            "contact_identifiers",
            "contacts",
            "agent_configs",
            "webhook_dedup",
        ):
            await session.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :cid"),  # noqa: S608
                {"cid": str(client_id)},
            )
        await session.execute(
            text("DELETE FROM audit_logs WHERE client_id = :cid"), {"cid": str(client_id)}
        )
        await session.execute(text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)})


def _payload_ycloud(external_id: str, texto: str = PREGUNTA) -> dict[str, Any]:
    """Arma el cuerpo de un webhook de YCloud con un mensaje de texto.

    Args:
        external_id: Id del mensaje en el proveedor (lo que usa el dedup).
        texto: Contenido del mensaje.

    Returns:
        El payload tal como lo manda YCloud.
    """
    return {
        "type": "whatsapp.inbound_message.received",
        "whatsappInboundMessage": {
            "id": external_id,
            "from": TELEFONO,
            "type": "text",
            "text": {"body": texto},
            "timestamp": "2026-09-17T10:00:00Z",
        },
    }


def _firmar(cuerpo: bytes) -> str:
    """Calcula la firma HMAC-SHA256 que YCloud pone en la cabecera.

    Args:
        cuerpo: Cuerpo exacto de la peticion.

    Returns:
        La firma en hexadecimal, sin prefijo.
    """
    secreto = get_settings().YCLOUD_WEBHOOK_SECRET
    return hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()


@pytest_asyncio.fixture
async def escenario(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Escenario, None]:
    """Siembra el tenant, sustituye lo que sale de la maquina y limpia al final."""
    await engine.dispose()

    client_id = uuid.uuid4()
    # El worker resuelve el tenant por DEFAULT_CLIENT_ID (MVP, Fase 2 lo hara por
    # `channel_configs`), asi que el tenant sembrado tiene que ser ese.
    monkeypatch.setattr(webhook_module, "_resolve_client_id", lambda provider, channel: client_id)
    monkeypatch.setenv("YCLOUD_WEBHOOK_SECRET", "secreto-de-prueba-e2e")
    get_settings.cache_clear()

    await _sembrar(client_id, f"e2e-{uuid.uuid4().hex[:8]}")

    # El LLM, en los DOS nodos que lo usan. Cada nodo importo `get_chat_model`
    # en su propio espacio de nombres, asi que parchear solo uno deja al otro
    # saliendo a OpenAI de verdad.
    parchear_chat_model(
        monkeypatch,
        intent_module,
        FakeChatModel(
            {
                "parsed": IntentClassification(intent="rag_query", confidence=0.95),
                "raw": RespuestaLLM(input_tokens=60, output_tokens=10),
            }
        ),
    )
    parchear_chat_model(
        monkeypatch,
        rag_module,
        FakeChatModel(RespuestaLLM(RESPUESTA_DEL_BOT, input_tokens=120, output_tokens=40)),
    )

    # El retrieval: un chunk por encima del umbral, para que el nodo RAG tenga
    # contexto y no marque `insufficient_context`.
    class RagConContexto:
        """Doble de RAGService con un unico chunk relevante."""

        async def retrieve(self, **kwargs: Any) -> list[RetrievalResult]:
            """Devuelve un chunk por encima del umbral de similitud."""
            return [
                RetrievalResult(
                    chunk_id=uuid.uuid4(),
                    content="El horario de atencion es de lunes a viernes de 9 a 18.",
                    similarity=0.93,
                    metadata={},
                    citation="[Fuente: horarios.pdf]",
                )
            ]

        async def retrieve_few_shot_examples(self, **kwargs: Any) -> list[dict[str, Any]]:
            """Sin ejemplos few-shot: no hacen falta para este recorrido."""
            return []

        def build_grounded_prompt(self, **kwargs: Any) -> str:
            """Prompt minimo: lo que importa aqui es que el nodo tenga contexto."""
            return "Responde solo con el contexto dado."

    monkeypatch.setattr(rag_module, "build_rag_service", lambda: RagConContexto())

    # El encolado: sin broker, `.delay()` reventaria y el endpoint devolveria
    # 503 ("no se pudo encolar") antes de llegar a ninguna otra cosa.
    class FakeTask:
        """Doble de la tarea Celery: registra el encolado en vez de publicarlo."""

        def __init__(self) -> None:
            """Arranca con el buzon vacio."""
            self.encolados: list[dict[str, Any]] = []

        def delay(self, **kwargs: Any) -> None:
            """Captura los argumentos con los que se habria encolado."""
            self.encolados.append(kwargs)

    monkeypatch.setattr(webhook_module, "process_incoming_message", FakeTask())

    # El proveedor de mensajeria: se captura lo que se habria enviado. El resto
    # del envio es real, incluido el guardado del mensaje saliente.
    enviados: list[dict[str, Any]] = []

    class FakeProvider:
        """Doble del proveedor: registra el envio en vez de salir a la red."""

        async def send_message(self, to: str, content: Any, channel_config: Any) -> str:
            """Captura el destinatario y el texto."""
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

    yield Escenario(client_id, enviados)

    await _limpiar(client_id)
    get_settings.cache_clear()


async def _postear_webhook(cuerpo: dict[str, Any]) -> Any:
    """Manda el webhook al endpoint real, firmado.

    Args:
        cuerpo: Payload de YCloud.

    Returns:
        La respuesta HTTP.
    """
    crudo = json.dumps(cuerpo).encode()
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/api/v1/webhooks/ycloud/whatsapp",
            content=crudo,
            headers={
                "Content-Type": "application/json",
                "X-Ycloud-Signature": _firmar(crudo),
            },
        )


async def _contar(client_id: uuid.UUID, sql: str, **params: Any) -> int:
    """Cuenta filas dentro del contexto de tenant.

    Args:
        client_id: Tenant.
        sql: Consulta que devuelve un escalar.
        **params: Parametros de la consulta.

    Returns:
        El escalar como entero.
    """
    async with tenant_session(client_id) as session:
        total = await session.scalar(text(sql), {"cid": str(client_id), **params})
    return int(total or 0)


class TestRecorridoCompleto:
    """De webhook entrante a respuesta guardada y enviada."""

    async def test_el_webhook_responde_rapido_y_sin_procesar(self, escenario: Escenario) -> None:
        """El endpoint acepta y delega: el procesamiento es asincrono.

        CLAUDE.md (regla 4) pide 200 en <100 ms; lo que se comprueba aqui es la
        consecuencia observable de eso — que al responder todavia no hay ningun
        mensaje escrito, porque el trabajo se encolo.
        """
        respuesta = await _postear_webhook(_payload_ycloud(f"wamid.{uuid.uuid4().hex[:16]}"))

        assert respuesta.status_code == 200
        assert (
            await _contar(
                escenario.client_id, "SELECT count(*) FROM messages WHERE client_id = :cid"
            )
            == 0
        )

    async def test_el_mensaje_entrante_acaba_en_una_respuesta_enviada(
        self, escenario: Escenario
    ) -> None:
        """El recorrido entero, de punta a punta."""
        external_id = f"wamid.{uuid.uuid4().hex[:16]}"

        # 1. Entra por HTTP.
        assert (await _postear_webhook(_payload_ycloud(external_id))).status_code == 200

        # 2. El worker de webhooks lo normaliza y lo guarda.
        await webhook_module._process_message(
            "ycloud", "whatsapp", _mensaje_normalizado(external_id)
        )

        # 3. El grafo genera la respuesta (lo que haria la tarea de `ai_inference`).
        conversacion_id, contacto_id = await _ids_de_la_conversacion(escenario.client_id)
        from app.tasks.ai_processor import _invoke_graph

        await _invoke_graph(
            str(escenario.client_id),
            str(conversacion_id),
            str(contacto_id),
            "whatsapp",
            _mensaje_normalizado(external_id),
        )

        # 4. Quedaron los dos mensajes: el del contacto y el del bot.
        async with tenant_session(escenario.client_id) as session:
            filas = (
                await session.execute(
                    text(
                        "SELECT direction, content, sender_type FROM messages "
                        "WHERE client_id = :cid ORDER BY created_at ASC"
                    ),
                    {"cid": str(escenario.client_id)},
                )
            ).all()

        assert [f.direction for f in filas] == ["inbound", "outbound"]
        assert filas[0].content == PREGUNTA
        assert filas[1].content == RESPUESTA_DEL_BOT
        assert filas[1].sender_type == "bot"

        # 5. Y se envio por el mismo canal por el que entro.
        assert len(escenario.enviados) == 1
        assert escenario.enviados[0]["to"] == TELEFONO
        assert escenario.enviados[0]["text"] == RESPUESTA_DEL_BOT

    async def test_el_recorrido_queda_auditado(self, escenario: Escenario) -> None:
        """Contacto, conversacion y los dos mensajes dejan rastro.

        Como el recorrido lo ejecuta el worker y no una persona, el autor de
        todas esas filas es NULL: son acciones del sistema.
        """
        external_id = f"wamid.{uuid.uuid4().hex[:16]}"
        await _postear_webhook(_payload_ycloud(external_id))
        await webhook_module._process_message(
            "ycloud", "whatsapp", _mensaje_normalizado(external_id)
        )

        async with tenant_session(escenario.client_id) as session:
            filas = (
                await session.execute(
                    text(
                        "SELECT table_name, action::text, user_id FROM audit_logs "
                        "WHERE client_id = :cid"
                    ),
                    {"cid": str(escenario.client_id)},
                )
            ).all()

        tablas = {f.table_name for f in filas}
        assert {"contacts", "conversations", "messages"} <= tablas
        assert all(f.user_id is None for f in filas)

    async def test_el_mismo_mensaje_dos_veces_no_se_procesa_dos_veces(
        self, escenario: Escenario
    ) -> None:
        """Los proveedores reenvian por timeouts; el dedup lo corta (PAT-001)."""
        external_id = f"wamid.{uuid.uuid4().hex[:16]}"
        normalizado = _mensaje_normalizado(external_id)

        await _postear_webhook(_payload_ycloud(external_id))
        await webhook_module._process_message("ycloud", "whatsapp", normalizado)

        # El proveedor reintenta con el mismo id.
        segunda = await _postear_webhook(_payload_ycloud(external_id))
        await webhook_module._process_message("ycloud", "whatsapp", normalizado)

        assert segunda.status_code == 200
        assert (
            await _contar(
                escenario.client_id,
                "SELECT count(*) FROM messages WHERE client_id = :cid",
            )
            == 1
        )

    async def test_una_firma_invalida_no_llega_a_la_base(self, escenario: Escenario) -> None:
        """Sin firma valida el webhook se rechaza antes de tocar nada."""
        cuerpo = json.dumps(_payload_ycloud(f"wamid.{uuid.uuid4().hex[:16]}")).encode()
        transport = ASGITransport(app=create_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            respuesta = await client.post(
                "/api/v1/webhooks/ycloud/whatsapp",
                content=cuerpo,
                headers={
                    "Content-Type": "application/json",
                    "X-Ycloud-Signature": "0" * 64,
                },
            )

        assert respuesta.status_code == 401
        assert (
            await _contar(
                escenario.client_id, "SELECT count(*) FROM webhook_dedup WHERE client_id = :cid"
            )
            == 0
        )


def _mensaje_normalizado(external_id: str, texto: str = PREGUNTA) -> dict[str, Any]:
    """`NormalizedMessage` serializado, como el que el endpoint encola.

    Args:
        external_id: Id del mensaje en el proveedor.
        texto: Contenido.

    Returns:
        El diccionario que recibe el worker.
    """
    return {
        "channel": "whatsapp",
        "sender_identifier": TELEFONO,
        "text": texto,
        "media_url": None,
        "media_type": None,
        "timestamp": "2026-09-17T10:00:00+00:00",
        "external_message_id": external_id,
        "raw_payload": {"origen": "e2e"},
    }


async def _ids_de_la_conversacion(client_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Devuelve (conversation_id, contact_id) de la conversacion recien creada.

    Args:
        client_id: Tenant.

    Returns:
        Los dos ids.
    """
    async with tenant_session(client_id) as session:
        fila = (
            await session.execute(
                text(
                    "SELECT id, contact_id FROM conversations WHERE client_id = :cid "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"cid": str(client_id)},
            )
        ).one()
    return fila.id, fila.contact_id
