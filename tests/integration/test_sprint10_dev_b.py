"""Sentimiento y CRUD de templates contra PostgreSQL real (Sprint 10, Dev B).

Lo que los dobles no pueden probar:

- que el sentimiento queda de verdad en `messages.metadata` con la
  concatenacion JSONB y que el scoring lo promedia desde ahi;
- que el CRUD de templates funciona con la RLS real: el tenant origen se
  valida dentro de su propio contexto, `tenant_templates` (sin RLS) se escribe
  sin contexto, y el chequeo de email duplicado pasa por la funcion
  `SECURITY DEFINER` `auth_lookup_user()`.

Corre como `app_user` (NOBYPASSRLS), igual que CI.
"""

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


@dataclass
class Escenario:
    plataforma: uuid.UUID
    super_admin: uuid.UUID
    origen: uuid.UUID
    otro: uuid.UUID
    contacto: uuid.UUID
    conversacion: uuid.UUID
    email_super_admin: str


async def _insertar(client_id: uuid.UUID, sql: str, params: dict[str, Any]) -> None:
    from app.core.database import tenant_session

    async with tenant_session(client_id) as session:
        await session.execute(text(sql), params)


@pytest_asyncio.fixture
async def escenario() -> AsyncGenerator[Escenario, None]:
    """Tres tenants (plataforma, origen, otro), un super_admin y una conversacion."""
    from app.core.database import AsyncSessionLocal, engine, tenant_session
    from app.core.security import hash_password

    await engine.dispose()
    e = Escenario(
        plataforma=uuid.uuid4(),
        super_admin=uuid.uuid4(),
        origen=uuid.uuid4(),
        otro=uuid.uuid4(),
        contacto=uuid.uuid4(),
        conversacion=uuid.uuid4(),
        email_super_admin=f"root-{uuid.uuid4().hex[:8]}@plataforma.co",
    )
    for client_id in (e.plataforma, e.origen, e.otro):
        await _insertar(
            client_id,
            "INSERT INTO clients (id, name, slug, plan, is_active) "
            "VALUES (:id, 'Tenant', :slug, 'free', true)",
            {"id": str(client_id), "slug": f"t-{client_id.hex[:8]}"},
        )
    await _insertar(
        e.plataforma,
        "INSERT INTO users (id, client_id, email, password_hash, first_name, last_name, role) "
        "VALUES (:id, :cid, :email, :hash, 'Root', 'Admin', 'super_admin')",
        {
            "id": str(e.super_admin),
            "cid": str(e.plataforma),
            "email": e.email_super_admin,
            "hash": hash_password("no-importa-123"),
        },
    )
    await _insertar(
        e.origen,
        "INSERT INTO agent_configs (client_id, name, system_prompt, config) "
        "VALUES (:cid, 'asistente', 'Eres la clinica origen', '{}'::jsonb)",
        {"cid": str(e.origen)},
    )
    await _insertar(
        e.origen,
        "INSERT INTO contacts (id, client_id, display_name) VALUES (:id, :cid, 'Ana')",
        {"id": str(e.contacto), "cid": str(e.origen)},
    )
    await _insertar(
        e.origen,
        "INSERT INTO conversations (id, client_id, contact_id, channel, status, last_message_at) "
        "VALUES (:id, :cid, :contacto, 'whatsapp', 'bot_active', now())",
        {"id": str(e.conversacion), "cid": str(e.origen), "contacto": str(e.contacto)},
    )

    yield e

    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(
            text(
                "DELETE FROM template_instantiations WHERE template_id IN "
                "(SELECT id FROM tenant_templates WHERE source_client_id = :cid)"
            ),
            {"cid": str(e.origen)},
        )
        await session.execute(
            text("DELETE FROM tenant_templates WHERE source_client_id = :cid"),
            {"cid": str(e.origen)},
        )
    for tabla in ("messages", "conversations", "contacts", "agent_configs"):
        await _insertar(
            e.origen,
            f"DELETE FROM {tabla} WHERE client_id = :cid",  # noqa: S608
            {"cid": str(e.origen)},
        )
    await _insertar(
        e.plataforma, "DELETE FROM users WHERE client_id = :cid", {"cid": str(e.plataforma)}
    )
    for client_id in (e.plataforma, e.origen, e.otro):
        async with tenant_session(client_id) as session:
            await session.execute(
                text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)}
            )


async def _mensaje(e: Escenario, external_id: str, texto: str) -> None:
    await _insertar(
        e.origen,
        "INSERT INTO messages (client_id, conversation_id, direction, sender_type, content, "
        "external_message_id, metadata) VALUES (:cid, :conv, 'inbound', 'contact', :texto, :ext, "
        '\'{"proveedor": "ycloud"}\'::jsonb)',
        {"cid": str(e.origen), "conv": str(e.conversacion), "texto": texto, "ext": external_id},
    )


# ─── Sentimiento ─────────────────────────────────────────────────────────────


async def test_el_sentimiento_queda_en_el_mensaje_sin_pisar_su_metadata(
    escenario: Escenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.agents.nodes import sentiment as nodo
    from app.agents.nodes._tenant import AgentSettings
    from app.core.database import tenant_session
    from app.schemas.sentiment import SentimentLevel, SentimentResult
    from tests.unit.agent_doubles import (
        FakeChatModel,
        parchear_agent_settings,
        parchear_chat_model,
    )

    await _mensaje(escenario, "wamid.previo", "Hola, tengo una duda")
    await _mensaje(escenario, "wamid.actual", "Nadie me responde, es la tercera vez!")

    modelo = FakeChatModel(
        {
            "parsed": SentimentResult(
                sentiment=SentimentLevel.VERY_NEGATIVE, score=0.9, reasoning="enojo"
            ),
            "raw": None,
        }
    )
    parchear_chat_model(monkeypatch, nodo, modelo)
    parchear_agent_settings(monkeypatch, nodo, AgentSettings(enabled_agents=("sentiment",)))

    salida = await nodo.sentiment_analysis_node(
        {
            "client_id": str(escenario.origen),
            "conversation_id": str(escenario.conversacion),
            "message": {
                "text": "Nadie me responde, es la tercera vez!",
                "external_message_id": "wamid.actual",
            },
            "consecutive_very_negative": 0,
        }
    )

    assert salida["current_sentiment"] == "very_negative"
    # El contexto trae el mensaje previo, no el que se esta clasificando.
    prompt_usuario = modelo.llamadas[0][1]["content"]
    assert "Usuario: Hola, tengo una duda" in prompt_usuario
    assert prompt_usuario.count("tercera vez") == 1

    async with tenant_session(escenario.origen) as session:
        metadata = (
            await session.execute(
                text("SELECT metadata FROM messages WHERE external_message_id = 'wamid.actual'")
            )
        ).scalar_one()
    assert metadata["sentiment"]["level"] == "very_negative"
    assert metadata["proveedor"] == "ycloud", "no puede pisar lo que dejo el proveedor"


async def test_el_scoring_promedia_el_sentimiento_de_los_mensajes(
    escenario: Escenario,
) -> None:
    from app.core.database import tenant_session
    from app.services.contact_scoring import cargar_metricas

    await _mensaje(escenario, "m1", "genial")
    await _mensaje(escenario, "m2", "mal")
    await _mensaje(escenario, "m3", "sin medir")
    async with tenant_session(escenario.origen) as session:
        for ext, nivel in (("m1", "positive"), ("m2", "negative")):
            await session.execute(
                text(
                    "UPDATE messages SET metadata = metadata || "
                    "jsonb_build_object('sentiment', jsonb_build_object('level', CAST(:n AS text))) "
                    "WHERE external_message_id = :ext"
                ),
                {"n": nivel, "ext": ext},
            )

    async with tenant_session(escenario.origen) as session:
        metricas = await cargar_metricas(session, escenario.origen, escenario.contacto)

    # (100 + 20) / 2: el mensaje sin medir no cuenta como neutral.
    assert metricas.sentimiento == pytest.approx(60.0)
    assert metricas.entrantes == 3


# ─── CRUD de templates ───────────────────────────────────────────────────────


def _cliente(api_client: Any, *, role: str, client_id: uuid.UUID, user_id: uuid.UUID) -> Any:
    from app.core.security import create_access_token

    token = create_access_token(
        {
            "user_id": str(user_id),
            "client_id": str(client_id),
            "email": "x@x.co",
            "role": role,
        }
    )
    api_client.headers["Authorization"] = f"Bearer {token}"
    return api_client


async def test_flujo_de_templates_bajo_rls(
    escenario: Escenario, api_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.tasks.tenant_operations as tareas

    encolados: list[dict[str, Any]] = []

    class _Task:
        @staticmethod
        def delay(**kwargs: Any) -> None:
            encolados.append(kwargs)

    monkeypatch.setattr(tareas, "clone_tenant_from_template", _Task)

    root = _cliente(
        api_client,
        role="super_admin",
        client_id=escenario.plataforma,
        user_id=escenario.super_admin,
    )
    creado = await root.post(
        "/api/v1/admin/templates",
        json={"name": "Clinica base", "source_client_id": str(escenario.origen)},
    )
    assert creado.status_code == 201, creado.text
    template_id = creado.json()["id"]
    assert creado.json()["config"]["agent_configs"][0]["system_prompt"] == "Eres la clinica origen"

    # Un admin de otro tenant no ve un template privado ajeno.
    otro_admin = _cliente(api_client, role="admin", client_id=escenario.otro, user_id=uuid.uuid4())
    listado = await otro_admin.get("/api/v1/admin/templates")
    assert template_id not in {t["id"] for t in listado.json()["items"]}

    # El email del super_admin ya existe: auth_lookup_user() lo encuentra bajo RLS.
    root = _cliente(
        api_client,
        role="super_admin",
        client_id=escenario.plataforma,
        user_id=escenario.super_admin,
    )
    duplicado = await root.post(
        f"/api/v1/admin/templates/{template_id}/instantiate",
        json={"tenant_name": "Norte", "admin_email": escenario.email_super_admin},
    )
    assert duplicado.status_code == 409
    assert encolados == []

    aceptado = await root.post(
        f"/api/v1/admin/templates/{template_id}/instantiate",
        json={"tenant_name": "Norte", "admin_email": f"n-{uuid.uuid4().hex[:6]}@norte.co"},
    )
    assert aceptado.status_code == 202, aceptado.text
    instantiation_id = aceptado.json()["instantiation_id"]
    assert encolados[0]["instantiation_id"] == instantiation_id

    avance = await root.get(f"/api/v1/admin/templates/instantiations/{instantiation_id}")
    assert avance.status_code == 200
    assert avance.json()["status"] == "pending"
