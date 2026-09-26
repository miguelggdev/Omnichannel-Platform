"""Canal de voz contra PostgreSQL real (Sprint 13, Dev A).

Lo que los dobles no prueban: que una frase de una llamada recorre el mismo
camino que un WhatsApp (contacto, conversacion del canal `voice`, mensaje,
encolado a la IA), que el upsert de `call_records` aguanta eventos fuera de
orden, que los telefonos quedan cifrados y que la API del CRM ve la llamada
vinculada a su conversacion y a nadie de otro tenant.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.core.encryption import mask_identifier
from app.models.call_record import CallRecord
from app.services.messaging.voice_provider import TwilioVoiceProvider
from app.tasks.voice_tasks import guardar_llamada
from app.tasks.webhook_processor import _process_message

pytestmark = [pytest.mark.db, pytest.mark.asyncio, pytest.mark.usefixtures("ia_encolada")]

CLIENTE = "+573001234567"
PLATAFORMA = "+15005550006"


@pytest_asyncio.fixture
async def tenant_voz(webhook_tenant: uuid.UUID) -> AsyncGenerator[uuid.UUID, None]:
    """`webhook_tenant` mas la limpieza de `call_records`, que la suya no conoce."""
    yield webhook_tenant
    async with tenant_session(webhook_tenant) as session:
        await session.execute(
            text("DELETE FROM call_records WHERE client_id = :cid"), {"cid": str(webhook_tenant)}
        )


async def _frase(call_sid: str, indice: int, texto: str) -> dict[str, Any]:
    """Lo que `CallSession` encola por cada frase transcrita."""
    normalized = await TwilioVoiceProvider().parse_webhook(
        {
            "CallSid": call_sid,
            "From": CLIENTE,
            "To": PLATAFORMA,
            "Direction": "inbound",
            "SpeechResult": texto,
            "UtteranceIndex": indice,
        }
    )
    return normalized.model_dump(mode="json")


async def _registro(client_id: uuid.UUID, call_sid: str) -> CallRecord:
    async with tenant_session(client_id) as session:
        registro = (
            await session.execute(select(CallRecord).where(CallRecord.call_sid == call_sid))
        ).scalar_one()
        session.expunge(registro)
        return registro


async def test_una_frase_de_la_llamada_entra_como_mensaje_del_canal_voice(
    tenant_voz: uuid.UUID, ia_encolada: list[dict[str, Any]]
) -> None:
    await _process_message("twilio", "voice", await _frase("CA-flujo", 1, "quiero una cita"))
    await _process_message("twilio", "voice", await _frase("CA-flujo", 2, "para el martes"))

    async with tenant_session(tenant_voz) as session:
        conversaciones = (
            await session.execute(
                text("SELECT id, channel FROM conversations WHERE client_id = :cid"),
                {"cid": str(tenant_voz)},
            )
        ).all()
        mensajes = (
            await session.execute(
                text(
                    "SELECT content, external_message_id FROM messages "
                    "WHERE client_id = :cid ORDER BY created_at"
                ),
                {"cid": str(tenant_voz)},
            )
        ).all()

    # Las dos frases de la misma llamada quedan en una sola conversacion de voz.
    assert [c.channel for c in conversaciones] == ["voice"]
    assert [(m.content, m.external_message_id) for m in mensajes] == [
        ("quiero una cita", "CA-flujo:1"),
        ("para el martes", "CA-flujo:2"),
    ]
    assert len(ia_encolada) == 2


async def test_el_registro_se_vincula_a_la_conversacion_de_la_llamada(
    tenant_voz: uuid.UUID,
) -> None:
    await _process_message("twilio", "voice", await _frase("CA-vinculo", 1, "hola"))
    await guardar_llamada(
        tenant_voz,
        "CA-vinculo",
        {"direction": "inbound", "status": "in-progress", "phone_from": CLIENTE},
    )

    registro = await _registro(tenant_voz, "CA-vinculo")
    async with tenant_session(tenant_voz) as session:
        conversacion = (
            await session.execute(
                text("SELECT id, contact_id FROM conversations WHERE client_id = :cid"),
                {"cid": str(tenant_voz)},
            )
        ).one()
    assert registro.conversation_id == conversacion.id
    assert registro.contact_id == conversacion.contact_id


async def test_eventos_fuera_de_orden_no_deshacen_el_estado_final(
    tenant_voz: uuid.UUID,
) -> None:
    """El status callback `completed` llega antes que el webhook inicial y el cierre."""
    inicio = datetime.now(timezone.utc) - timedelta(minutes=2)

    await guardar_llamada(
        tenant_voz,
        "CA-orden",
        {
            "status": "completed",
            "duration_seconds": 42,
            "ended_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    await guardar_llamada(
        tenant_voz,
        "CA-orden",
        {
            "direction": "inbound",
            "status": "in-progress",
            "phone_from": CLIENTE,
            "phone_to": PLATAFORMA,
            "started_at": inicio.isoformat(),
        },
    )
    await guardar_llamada(
        tenant_voz,
        "CA-orden",
        {"transcript": [{"role": "caller", "text": "hola", "timestamp": inicio.isoformat()}]},
    )

    registro = await _registro(tenant_voz, "CA-orden")
    assert registro.status == "completed"
    assert registro.duration_seconds == 42
    assert registro.ended_at is not None
    assert registro.started_at == inicio
    assert registro.transcript == [
        {"role": "caller", "text": "hola", "timestamp": inicio.isoformat()}
    ]
    assert registro.phone_from == CLIENTE
    assert registro.phone_to == PLATAFORMA


async def test_los_telefonos_de_la_llamada_quedan_cifrados(tenant_voz: uuid.UUID) -> None:
    await guardar_llamada(
        tenant_voz,
        "CA-cifrado",
        {"status": "in-progress", "phone_from": CLIENTE, "phone_to": PLATAFORMA},
    )
    async with tenant_session(tenant_voz) as session:
        crudo = (
            await session.execute(
                text(
                    "SELECT phone_from::text, phone_to::text FROM call_records "
                    "WHERE call_sid = 'CA-cifrado'"
                )
            )
        ).one()
    assert "3001234567" not in crudo[0]
    assert "5005550006" not in crudo[1]


async def test_un_estado_desconocido_se_rechaza(tenant_voz: uuid.UUID) -> None:
    with pytest.raises(ValueError, match="desconocido"):
        await guardar_llamada(tenant_voz, "CA-raro", {"status": "exploded"})


async def test_la_api_lista_la_llamada_enmascarada_y_solo_a_su_tenant(
    tenant_voz: uuid.UUID, authenticated_client_factory: Any
) -> None:
    await _process_message("twilio", "voice", await _frase("CA-api", 1, "hola"))
    await guardar_llamada(
        tenant_voz,
        "CA-api",
        {
            "direction": "inbound",
            "status": "completed",
            "phone_from": CLIENTE,
            "phone_to": PLATAFORMA,
            "transcript": [{"role": "caller", "text": "hola", "timestamp": "t"}],
        },
    )

    cliente = authenticated_client_factory(role="agent", client_id=tenant_voz)
    listado = await cliente.get("/api/v1/voice/calls")
    assert listado.status_code == 200, listado.text
    [llamada] = listado.json()["items"]
    assert llamada["call_sid"] == "CA-api"
    assert llamada["phone_from"] == mask_identifier(CLIENTE)
    assert llamada["phone_from"].endswith("4567")
    assert "300123" not in llamada["phone_from"]
    assert llamada["transcript"] == []  # el listado no trae transcripciones
    assert llamada["conversation_id"] is not None

    detalle = await cliente.get(f"/api/v1/voice/calls/{llamada['id']}")
    assert detalle.status_code == 200
    assert detalle.json()["transcript"][0]["text"] == "hola"

    otro = authenticated_client_factory(role="admin", client_id=uuid.uuid4())
    assert (await otro.get(f"/api/v1/voice/calls/{llamada['id']}")).status_code == 404
    assert (await otro.get("/api/v1/voice/calls")).json()["total"] == 0
