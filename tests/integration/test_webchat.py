"""Webchat y transcripcion de audio contra PostgreSQL real, con RLS activo.

Lo que se comprueba aqui y no se puede comprobar con dobles:

- Un mensaje de webchat crea contacto, conversacion y mensaje bajo el contexto
  de tenant, igual que cualquier otro canal, con la sesion como identificador.
- La reposicion al reconectar (`mensajes_perdidos`) devuelve **solo** lo que el
  agente mando despues del ancla, **solo** de esa sesion y **solo** de ese
  tenant: el indice ciego es por tenant (ADR-052) y el ancla se busca dentro de
  las conversaciones del propio contacto.
- Una nota de voz se guarda como audio y su transcripcion acaba en
  `messages.content` sin perder el `media_url` original.

Requiere base de datos: `pytest tests/ --run-db`.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from app.api.v1.webchat import mensajes_perdidos
from app.core.database import tenant_session
from app.tasks.audio_transcription import guardar_transcripcion
from app.tasks.webhook_processor import _process_message
from tests.integration.test_webhook_flow import _contar, _normalized

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("ia_encolada")]

CANAL = "webchat"
SESION = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


def _frame(external_id: str, sesion: str = SESION, **extra: Any) -> dict[str, Any]:
    """Un mensaje de webchat ya normalizado, como lo encola el endpoint.

    Args:
        external_id: Id que genero el servidor.
        sesion: Sesion del visitante.
        **extra: Campos que reemplazan a los de base.

    Returns:
        El `NormalizedMessage` serializado.
    """
    return _normalized(
        CANAL,
        sesion,
        external_id,
        "hola, cuanto cuesta el plan pro?",
        media_type="text",
        raw_payload={"type": "message", "session_id": sesion},
        **extra,
    )


async def _responder(
    client_id: uuid.UUID, conversation_id: uuid.UUID, external_id: str, texto: str
) -> None:
    """Escribe un mensaje saliente, como haria `deliver_message()`.

    Args:
        client_id: Tenant.
        conversation_id: Conversacion.
        external_id: Id externo del mensaje.
        texto: Cuerpo.
    """
    async with tenant_session(client_id) as session:
        await session.execute(
            text(
                "INSERT INTO messages (client_id, conversation_id, direction, message_type, "
                "content, external_message_id, sender_type) "
                "VALUES (:cid, :conv, 'outbound', 'text', :texto, :ext, 'bot')"
            ),
            {
                "cid": str(client_id),
                "conv": str(conversation_id),
                "texto": texto,
                "ext": external_id,
            },
        )


async def _conversacion_de(client_id: uuid.UUID) -> uuid.UUID:
    """Devuelve la unica conversacion del tenant.

    Args:
        client_id: Tenant.

    Returns:
        El id de la conversacion.
    """
    async with tenant_session(client_id) as session:
        fila = await session.execute(
            text("SELECT id FROM conversations WHERE client_id = :cid"), {"cid": str(client_id)}
        )
        return uuid.UUID(str(fila.scalar_one()))


# ─── Entrada ─────────────────────────────────────────────────────────────────


async def test_un_mensaje_de_webchat_crea_contacto_conversacion_y_mensaje(
    webhook_tenant: uuid.UUID,
) -> None:
    """La sesion del socket es el identificador del contacto en este canal."""
    await _process_message(CANAL, CANAL, _frame("wc-0001"))

    assert await _contar(webhook_tenant, "contacts") == 1
    assert await _contar(webhook_tenant, "conversations") == 1
    assert await _contar(webhook_tenant, "messages") == 1

    async with tenant_session(webhook_tenant) as session:
        canal, direccion, contenido = (
            await session.execute(
                text(
                    "SELECT c.channel, m.direction, m.content FROM messages m "
                    "JOIN conversations c ON c.id = m.conversation_id "
                    "WHERE m.client_id = :cid"
                ),
                {"cid": str(webhook_tenant)},
            )
        ).one()

    assert canal == CANAL
    assert direccion == "inbound"
    assert contenido == "hola, cuanto cuesta el plan pro?"


async def test_dos_mensajes_de_la_misma_sesion_comparten_conversacion(
    webhook_tenant: uuid.UUID,
) -> None:
    await _process_message(CANAL, CANAL, _frame("wc-0001"))
    await _process_message(CANAL, CANAL, _frame("wc-0002"))

    assert await _contar(webhook_tenant, "contacts") == 1
    assert await _contar(webhook_tenant, "conversations") == 1
    assert await _contar(webhook_tenant, "messages") == 2


async def test_dos_sesiones_distintas_son_dos_contactos(webhook_tenant: uuid.UUID) -> None:
    """Cada visitante del widget es un contacto propio."""
    await _process_message(CANAL, CANAL, _frame("wc-0001"))
    await _process_message(CANAL, CANAL, _frame("wc-0002", sesion="f" * 32))

    assert await _contar(webhook_tenant, "contacts") == 2
    assert await _contar(webhook_tenant, "conversations") == 2


# ─── Reposicion al reconectar ────────────────────────────────────────────────


async def test_repone_solo_lo_posterior_al_ancla(webhook_tenant: uuid.UUID) -> None:
    """Criterio 6 del spec: reconectar recupera lo que se perdio, y nada mas."""
    await _process_message(CANAL, CANAL, _frame("wc-0001"))
    conversacion = await _conversacion_de(webhook_tenant)

    await _responder(webhook_tenant, conversacion, "wc-r1", "el plan pro cuesta 50")
    await _responder(webhook_tenant, conversacion, "wc-r2", "te ayudo con algo mas?")
    await _responder(webhook_tenant, conversacion, "wc-r3", "sigo por aqui")

    perdidos = await mensajes_perdidos(webhook_tenant, SESION, "wc-r1", limite=50)

    assert [m["message_id"] for m in perdidos] == ["wc-r2", "wc-r3"]
    assert perdidos[0]["text"] == "te ayudo con algo mas?"
    assert all(m["replayed"] for m in perdidos)


async def test_no_repone_lo_que_escribio_el_propio_visitante(
    webhook_tenant: uuid.UUID,
) -> None:
    """El widget ya tiene sus propios mensajes en pantalla."""
    await _process_message(CANAL, CANAL, _frame("wc-0001"))
    conversacion = await _conversacion_de(webhook_tenant)
    await _responder(webhook_tenant, conversacion, "wc-r1", "hola")
    await _process_message(CANAL, CANAL, _frame("wc-0002"))

    perdidos = await mensajes_perdidos(webhook_tenant, SESION, "wc-r1", limite=50)

    assert perdidos == []


async def test_un_ancla_de_otra_sesion_no_repone_nada(webhook_tenant: uuid.UUID) -> None:
    """Con el id de otro visitante no se puede leer su conversacion."""
    otra_sesion = "b" * 32
    await _process_message(CANAL, CANAL, _frame("wc-0001"))
    await _process_message(CANAL, CANAL, _frame("wc-0002", sesion=otra_sesion))

    async with tenant_session(webhook_tenant) as session:
        conversaciones = [
            uuid.UUID(str(fila[0]))
            for fila in (
                await session.execute(
                    text(
                        "SELECT c.id FROM conversations c JOIN messages m ON m.conversation_id = c.id "
                        "WHERE c.client_id = :cid AND m.external_message_id = 'wc-0002'"
                    ),
                    {"cid": str(webhook_tenant)},
                )
            ).all()
        ]

    await _responder(webhook_tenant, conversaciones[0], "wc-ajeno", "respuesta de la otra sesion")
    await _responder(webhook_tenant, conversaciones[0], "wc-ajeno-2", "y otra mas")

    perdidos = await mensajes_perdidos(webhook_tenant, SESION, "wc-ajeno", limite=50)

    assert perdidos == []


async def test_una_sesion_sin_historial_no_repone_nada(webhook_tenant: uuid.UUID) -> None:
    assert await mensajes_perdidos(webhook_tenant, "c" * 32, "wc-r1", limite=50) == []


async def test_respeta_el_limite_de_reposicion(webhook_tenant: uuid.UUID) -> None:
    await _process_message(CANAL, CANAL, _frame("wc-0001"))
    conversacion = await _conversacion_de(webhook_tenant)
    await _responder(webhook_tenant, conversacion, "wc-r0", "ancla")
    for i in range(5):
        await _responder(webhook_tenant, conversacion, f"wc-r{i + 1}", f"respuesta {i}")

    perdidos = await mensajes_perdidos(webhook_tenant, SESION, "wc-r0", limite=2)

    assert len(perdidos) == 2


# ─── Transcripcion ───────────────────────────────────────────────────────────


async def test_una_nota_de_voz_se_guarda_como_audio_y_se_transcribe(
    webhook_tenant: uuid.UUID, ia_encolada: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """El audio queda guardado y la transcripcion entra en `content`."""
    from app.tasks import webhook_processor as wp

    transcripciones: list[dict[str, Any]] = []
    monkeypatch.setattr(wp, "_enqueue_transcription", lambda **kw: transcripciones.append(kw))

    await _process_message(
        "ycloud",
        "whatsapp",
        _normalized(
            "whatsapp",
            "573001112233",
            "wamid.voz",
            None,
            media_url="https://cdn.ycloud.com/a.ogg",
            media_type="audio",
        ),
    )

    # El audio no va directo al grafo: primero a Whisper.
    assert len(transcripciones) == 1
    assert ia_encolada == []

    conversacion = transcripciones[0]["conversation_id"]
    await guardar_transcripcion(webhook_tenant, conversacion, "wamid.voz", "quiero una cita")

    async with tenant_session(webhook_tenant) as session:
        tipo, contenido, media, metadatos = (
            await session.execute(
                text(
                    "SELECT message_type, content, media_url, metadata FROM messages "
                    "WHERE client_id = :cid AND external_message_id = 'wamid.voz'"
                ),
                {"cid": str(webhook_tenant)},
            )
        ).one()

    assert tipo == "audio"
    assert contenido == "quiero una cita"
    assert media == "https://cdn.ycloud.com/a.ogg"
    assert metadatos["original_type"] == "audio"
    assert metadatos["transcription_model"]
