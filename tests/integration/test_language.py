"""Multi-idioma contra PostgreSQL real (Sprint 14b, ADR-077).

Lo que los dobles de sesion no pueden probar: que el idioma queda en
`conversations.metadata` sin pisar el resto, que RLS lo aisla entre tenants, que
dos mensajes simultaneos no parten la conversacion en dos idiomas, y que las
preferencias de usuario (`users.settings`, migracion 021) se mezclan y se aislan.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.agents.nodes import human_handoff as handoff
from app.agents.nodes import language_detect as nodo
from app.core.database import engine, tenant_session
from app.services.i18n import get_system_message

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

ES = "Hola, quiero agendar una cita para mañana por la tarde"
EN = "Hello, I would like to book an appointment for tomorrow"
FR = "Bonjour, je voudrais prendre rendez-vous pour demain"


class Datos:
    """Ids de un tenant sembrado."""

    def __init__(self, client_id: uuid.UUID, contact_id: uuid.UUID, conversation_id: uuid.UUID):
        self.client_id = client_id
        self.contact_id = contact_id
        self.conversation_id = conversation_id

    def estado(self, texto: str | None, **extra: Any) -> dict[str, Any]:
        return {
            "client_id": str(self.client_id),
            "conversation_id": str(self.conversation_id),
            "contact_id": str(self.contact_id),
            "channel": "whatsapp",
            "message": {"text": texto},
            "model_to_use": "gpt-4o",
            **extra,
        }


async def _sembrar(
    client_settings: dict[str, Any] | None = None, metadata: dict[str, Any] | None = None
) -> Datos:
    await engine.dispose()
    client_id, contact_id, conversation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with tenant_session(client_id) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active, settings) "
                "VALUES (:id, 'Clinica Sol', :slug, 'free', true, CAST(:settings AS jsonb))"
            ),
            {
                "id": str(client_id),
                "slug": f"idioma-{client_id.hex[:8]}",
                "settings": json.dumps(client_settings or {}),
            },
        )
        await session.execute(
            text(
                "INSERT INTO contacts (id, client_id, display_name) VALUES (:id, :cid, 'Contacto')"
            ),
            {"id": str(contact_id), "cid": str(client_id)},
        )
        await session.execute(
            text(
                "INSERT INTO conversations (id, client_id, contact_id, channel, status, metadata) "
                "VALUES (:id, :cid, :contact, 'whatsapp', 'bot_active', CAST(:meta AS jsonb))"
            ),
            {
                "id": str(conversation_id),
                "cid": str(client_id),
                "contact": str(contact_id),
                "meta": json.dumps(metadata or {}),
            },
        )
        await session.execute(
            text(
                "INSERT INTO agent_configs (client_id, name, config) "
                "VALUES (:cid, 'Asistente', CAST(:cfg AS jsonb))"
            ),
            {"cid": str(client_id), "cfg": json.dumps({"enabled_agents": ["rag"]})},
        )
    return Datos(client_id, contact_id, conversation_id)


async def _borrar(*tenants: uuid.UUID) -> None:
    for client_id in tenants:
        async with tenant_session(client_id) as session:
            for tabla in ("messages", "conversations", "users", "agent_configs", "contacts"):
                await session.execute(
                    text(f"DELETE FROM {tabla} WHERE client_id = :cid"),  # noqa: S608
                    {"cid": str(client_id)},
                )
            await session.execute(
                text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)}
            )


@pytest_asyncio.fixture
async def datos() -> AsyncGenerator[Datos, None]:
    d = await _sembrar()
    yield d
    await _borrar(d.client_id)


async def _metadata(d: Datos) -> dict[str, Any]:
    async with tenant_session(d.client_id) as session:
        fila = (
            await session.execute(
                text("SELECT metadata FROM conversations WHERE id = :id"),
                {"id": str(d.conversation_id)},
            )
        ).one()
    return dict(fila[0])


@pytest.fixture
def sin_llm(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Hace fallar cualquier llamada al LLM: estos tests no deben necesitarlo."""
    llamadas: list[str] = []

    async def _no(state: Any, texto: str) -> str | None:
        llamadas.append(texto)
        raise AssertionError("no deberia llamar al LLM")

    monkeypatch.setattr(nodo, "detect_with_llm", _no)
    return llamadas


async def test_detecta_el_idioma_y_lo_guarda_sin_pisar_el_resto_de_la_metadata(
    sin_llm: list[str],
) -> None:
    d = await _sembrar(metadata={"handoff": {"reason": "complaint"}, "clinical": False})
    try:
        resultado = await nodo.language_detect_node(d.estado(EN))  # type: ignore[arg-type]

        assert resultado == {"detected_language": "en"}
        assert await _metadata(d) == {
            "handoff": {"reason": "complaint"},
            "clinical": False,
            "detected_language": "en",
        }
    finally:
        await _borrar(d.client_id)


async def test_el_idioma_no_cambia_de_un_mensaje_a_otro(datos: Datos, sin_llm: list[str]) -> None:
    primero = await nodo.language_detect_node(datos.estado(ES))  # type: ignore[arg-type]
    despues = await nodo.language_detect_node(datos.estado(EN))  # type: ignore[arg-type]

    assert primero == despues == {"detected_language": "es"}
    assert (await _metadata(datos))["detected_language"] == "es"


async def test_dos_mensajes_simultaneos_no_parten_la_conversacion_en_dos_idiomas(
    datos: Datos, sin_llm: list[str]
) -> None:
    """Gana el primero en escribir y el otro responde en lo que quedo guardado."""
    resultados = await asyncio.gather(
        nodo.language_detect_node(datos.estado(ES)),  # type: ignore[arg-type]
        nodo.language_detect_node(datos.estado(FR)),  # type: ignore[arg-type]
    )

    guardado = (await _metadata(datos))["detected_language"]
    assert guardado in {"es", "fr"}
    assert resultados[0] == resultados[1] == {"detected_language": guardado}


async def _llm_que_no_sabe(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sabe(state: Any, texto: str) -> str | None:
        return None

    monkeypatch.setattr(nodo, "detect_with_llm", _no_sabe)


async def test_un_texto_corto_que_nadie_decide_usa_el_idioma_del_tenant_y_no_guarda_nada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = await _sembrar(client_settings={"default_language": "pt"})
    await _llm_que_no_sabe(monkeypatch)
    try:
        resultado = await nodo.language_detect_node(d.estado("ok"))  # type: ignore[arg-type]

        assert resultado == {"detected_language": "pt"}
        # Sin conjetura guardada: el mensaje siguiente puede volver a intentarlo.
        assert "detected_language" not in await _metadata(d)
    finally:
        await _borrar(d.client_id)


async def test_el_mensaje_siguiente_si_puede_fijar_el_idioma_tras_un_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = await _sembrar(client_settings={"default_language": "pt"})
    await _llm_que_no_sabe(monkeypatch)
    try:
        await nodo.language_detect_node(d.estado("ok"))  # type: ignore[arg-type]
        resultado = await nodo.language_detect_node(d.estado(FR))  # type: ignore[arg-type]

        assert resultado == {"detected_language": "fr"}
        assert (await _metadata(d))["detected_language"] == "fr"
    finally:
        await _borrar(d.client_id)


async def test_una_conversacion_clinica_nunca_llega_al_llm(sin_llm: list[str]) -> None:
    d = await _sembrar(metadata={"clinical": True})
    try:
        resultado = await nodo.language_detect_node(d.estado("ok gracias"))  # type: ignore[arg-type]

        assert resultado == {"detected_language": "es"}
        assert sin_llm == []
        assert "detected_language" not in await _metadata(d)
    finally:
        await _borrar(d.client_id)


async def test_otro_tenant_no_puede_fijar_el_idioma_de_una_conversacion_ajena() -> None:
    """RLS: desde el contexto de B la conversacion de A no existe."""
    a = await _sembrar()
    b = await _sembrar()
    try:
        await nodo._guardar_idioma(b.client_id, str(a.conversation_id), "en")

        assert "detected_language" not in await _metadata(a)
    finally:
        await _borrar(a.client_id, b.client_id)


async def test_un_mensaje_en_ingles_escala_a_humano_en_ingles(
    datos: Datos, sin_llm: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """De punta a punta con los nodos reales: detecta, guarda y responde en el idioma."""
    enviados: list[str] = []

    async def _capturar(**kwargs: Any) -> None:
        enviados.append(kwargs["text"])

    monkeypatch.setattr(handoff, "deliver_message", _capturar)
    estado = datos.estado("I would like to speak with a human agent about my appointment")
    estado.update(await nodo.language_detect_node(estado))  # type: ignore[arg-type]
    estado["handoff_reason"] = "human_request"

    await handoff.human_handoff_node(estado)  # type: ignore[arg-type]

    assert estado["detected_language"] == "en"
    assert enviados == [get_system_message("handoff_human_request", "en")]


# ─── Preferencias de usuario (`users.settings`, migracion 021) ───────────────


_INSERTAR_USUARIO = text(
    "INSERT INTO users (id, client_id, email, password_hash, first_name, last_name, role) "
    "VALUES (:id, :cid, :email, 'x', 'Ana', 'Perez', 'agent')"
)
_INSERTAR_USUARIO_CON_SETTINGS = text(
    "INSERT INTO users (id, client_id, email, password_hash, first_name, last_name, role, "
    "settings) VALUES (:id, :cid, :email, 'x', 'Ana', 'Perez', 'agent', "
    "CAST(:settings AS jsonb))"
)


async def _usuario(d: Datos, settings: dict[str, Any] | None = None) -> uuid.UUID:
    """Crea un usuario; sin `settings` no se toca la columna, para probar su default."""
    usuario = uuid.uuid4()
    params: dict[str, Any] = {
        "id": str(usuario),
        "cid": str(d.client_id),
        "email": f"{usuario.hex[:8]}@idioma.test",
    }
    async with tenant_session(d.client_id) as session:
        if settings is None:
            await session.execute(_INSERTAR_USUARIO, params)
        else:
            await session.execute(
                _INSERTAR_USUARIO_CON_SETTINGS, {**params, "settings": json.dumps(settings)}
            )
    return usuario


async def _ajustes(d: Datos, usuario: uuid.UUID) -> dict[str, Any]:
    async with tenant_session(d.client_id) as session:
        fila = (
            await session.execute(
                text("SELECT settings FROM users WHERE id = :id"), {"id": str(usuario)}
            )
        ).one()
    return dict(fila[0])


async def test_un_usuario_nuevo_nace_con_settings_vacio_y_la_api_da_los_defaults(
    datos: Datos, authenticated_client_factory: Any
) -> None:
    usuario = await _usuario(datos)
    cliente = authenticated_client_factory(role="agent", client_id=datos.client_id, user_id=usuario)

    respuesta = await cliente.get("/api/v1/settings/preferences")

    assert await _ajustes(datos, usuario) == {}
    assert respuesta.json() == {"ui_language": "es", "theme": "system"}


async def test_cambiar_preferencias_conserva_las_otras_claves(
    datos: Datos, authenticated_client_factory: Any
) -> None:
    usuario = await _usuario(datos, {"notificaciones": {"email": True}, "theme": "light"})
    cliente = authenticated_client_factory(role="agent", client_id=datos.client_id, user_id=usuario)

    put = await cliente.put("/api/v1/settings/preferences", json={"ui_language": "de"})
    get = await cliente.get("/api/v1/settings/preferences")

    assert put.status_code == 200, put.text
    assert put.json() == {"updated": {"ui_language": "de"}}
    assert await _ajustes(datos, usuario) == {
        "notificaciones": {"email": True},
        "theme": "light",
        "ui_language": "de",
    }
    assert get.json() == {"ui_language": "de", "theme": "light"}


async def test_las_preferencias_de_un_usuario_no_tocan_las_de_otro(
    datos: Datos, authenticated_client_factory: Any
) -> None:
    yo = await _usuario(datos)
    otro = await _usuario(datos, {"ui_language": "fr"})
    cliente = authenticated_client_factory(role="agent", client_id=datos.client_id, user_id=yo)

    await cliente.put("/api/v1/settings/preferences", json={"ui_language": "it"})

    assert (await _ajustes(datos, yo))["ui_language"] == "it"
    assert await _ajustes(datos, otro) == {"ui_language": "fr"}


async def test_un_token_de_otro_tenant_no_puede_cambiar_al_usuario_ajeno(
    datos: Datos, authenticated_client_factory: Any
) -> None:
    """RLS: el `user_id` existe, pero en otro tenant; desde este no hay fila."""
    ajeno = await _sembrar()
    try:
        usuario_ajeno = await _usuario(ajeno, {"ui_language": "fr"})
        cliente = authenticated_client_factory(
            role="agent", client_id=datos.client_id, user_id=usuario_ajeno
        )

        put = await cliente.put("/api/v1/settings/preferences", json={"ui_language": "en"})

        assert put.status_code == 404
        assert await _ajustes(ajeno, usuario_ajeno) == {"ui_language": "fr"}
    finally:
        await _borrar(ajeno.client_id)
