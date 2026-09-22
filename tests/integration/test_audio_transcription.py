"""Transcripcion de notas de voz contra PostgreSQL real, con RLS activo.

Los dobles de `tests/unit/test_audio_transcription.py` cubren la tarea de Celery
pieza a pieza; lo que falta comprobar contra la base es el contrato que une al
worker de webhooks con Whisper:

- un audio **sin texto** se persiste como tal y no llega al grafo: la IA la
  encola despues la propia transcripcion, cuando ya hay texto que mandarle;
- `_guardar_transcripcion()` escribe `messages.content` **sin perder el
  `media_url`** ni el `message_type`, que es lo que deja la nota de voz
  reproducible en la bandeja despues de transcrita;
- la escritura pasa por `tenant_session()`, asi que un tenant no puede tocar el
  mensaje de otro aunque tenga su UUID.

Requiere base de datos: `pytest tests/ --run-db`. Sin la opcion, el marker `db`
los omite (ver `pytest_collection_modifyitems` en conftest.py).
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.database import tenant_session
from app.services.transcription import PermanentTranscriptionError, Transcription
from app.tasks.audio_transcription import _guardar_transcripcion, _leer_transcripcion_previa
from app.tasks.webhook_processor import _process_message
from tests.integration.test_webhook_flow import _normalized

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("ia_encolada")]

TELEFONO = "573001112233"
AUDIO_URL = "https://cdn.ycloud.com/voz.ogg"
MODELO = "whisper-1"


def _audio(external_id: str, texto: str | None = None, **extra: Any) -> dict[str, Any]:
    """Una nota de voz de WhatsApp ya normalizada, como la encola el endpoint.

    Args:
        external_id: `wamid` del mensaje.
        texto: Caption del audio, si el canal lo trae.
        **extra: Campos que reemplazan a los de base.

    Returns:
        El `NormalizedMessage` serializado.
    """
    # `text` va como campo extra, no en el cuarto posicional: una nota de voz
    # normalmente llega sin texto y ese parametro de `_normalized` no admite None.
    # Y los campos de base se arman en un dict para que `extra` pueda pisarlos.
    campos: dict[str, Any] = {"media_url": AUDIO_URL, "media_type": "audio", "text": texto}
    campos.update(extra)
    return _normalized("whatsapp", TELEFONO, external_id, **campos)


@pytest.fixture
def transcripciones_encoladas(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Sustituye el `.delay()` de la tarea de transcripcion.

    Mismo motivo que `ia_encolada` en el conftest: el job de integracion no
    levanta el broker, y lo que aqui interesa es que el audio salga hacia Whisper
    con los ids serializados, no que Celery sepa hablar con Redis.

    Args:
        monkeypatch: Parcheo del test.

    Returns:
        La lista donde se acumula lo encolado.
    """
    from app.tasks.audio_transcription import transcribe_audio_message

    encoladas: list[dict[str, Any]] = []
    monkeypatch.setattr(transcribe_audio_message, "delay", lambda **kw: encoladas.append(kw))
    return encoladas


async def _mensaje(client_id: uuid.UUID, external_id: str) -> Any:
    """Lee el mensaje del tenant por su id externo, con el contexto aplicado.

    Args:
        client_id: Tenant propietario.
        external_id: Id del mensaje en el proveedor.

    Returns:
        La fila del mensaje.
    """
    async with tenant_session(client_id) as session:
        return (
            await session.execute(
                text(
                    "SELECT id, message_type, content, media_url, metadata FROM messages "
                    "WHERE client_id = :cid AND external_message_id = :ext"
                ),
                {"cid": str(client_id), "ext": external_id},
            )
        ).one()


@pytest_asyncio.fixture
async def otro_tenant() -> AsyncGenerator[uuid.UUID, None]:
    """Un segundo tenant commiteado, con su limpieza."""
    client_id = uuid.uuid4()
    async with tenant_session(client_id) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active) "
                "VALUES (:id, 'Tenant Transcripcion', :slug, 'free', true)"
            ),
            {"id": str(client_id), "slug": f"transcripcion-{client_id.hex[:8]}"},
        )

    yield client_id

    async with tenant_session(client_id) as session:
        await session.execute(text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)})


# ─── Entrada ─────────────────────────────────────────────────────────────────


async def test_un_audio_sin_texto_se_guarda_y_espera_a_whisper(
    webhook_tenant: uuid.UUID,
    ia_encolada: list[dict[str, Any]],
    transcripciones_encoladas: list[dict[str, Any]],
) -> None:
    """El audio queda persistido y va a Whisper, no al grafo."""
    await _process_message("ycloud", "whatsapp", _audio("wamid.voz"))

    fila = await _mensaje(webhook_tenant, "wamid.voz")
    assert fila.message_type == "audio"
    assert not fila.content
    assert fila.media_url == AUDIO_URL

    # El grafo no sabe leer un .ogg: la IA la encola la transcripcion, con texto.
    assert ia_encolada == []
    assert len(transcripciones_encoladas) == 1
    assert transcripciones_encoladas[0]["message_id"] == str(fila.id)
    assert transcripciones_encoladas[0]["client_id"] == str(webhook_tenant)
    assert transcripciones_encoladas[0]["channel"] == "whatsapp"


async def test_un_audio_con_caption_va_directo_a_la_ia(
    webhook_tenant: uuid.UUID,
    ia_encolada: list[dict[str, Any]],
    transcripciones_encoladas: list[dict[str, Any]],
) -> None:
    """Si el canal ya trae texto no hay nada que transcribir."""
    await _process_message("ycloud", "whatsapp", _audio("wamid.caption", "quiero una cita"))

    assert transcripciones_encoladas == []
    assert len(ia_encolada) == 1


async def test_un_audio_sin_media_url_no_se_encola(
    webhook_tenant: uuid.UUID,
    ia_encolada: list[dict[str, Any]],
    transcripciones_encoladas: list[dict[str, Any]],
) -> None:
    """Sin URL no hay que descargar: el mensaje queda guardado y ahi se acaba."""
    await _process_message("ycloud", "whatsapp", _audio("wamid.sinurl", media_url=None))

    assert (await _mensaje(webhook_tenant, "wamid.sinurl")).message_type == "audio"
    assert transcripciones_encoladas == []
    assert ia_encolada == []


# ─── Escritura de la transcripcion ───────────────────────────────────────────


async def test_la_transcripcion_escribe_el_texto_sin_perder_el_audio(
    webhook_tenant: uuid.UUID, transcripciones_encoladas: list[dict[str, Any]]
) -> None:
    """`content` pasa a ser el texto y la nota de voz sigue siendo reproducible."""
    await _process_message("ycloud", "whatsapp", _audio("wamid.voz"))
    mensaje_id = (await _mensaje(webhook_tenant, "wamid.voz")).id

    await _guardar_transcripcion(
        webhook_tenant,
        mensaje_id,
        Transcription(text="quiero una cita para el martes", duration_seconds=3.2),
        MODELO,
    )

    fila = await _mensaje(webhook_tenant, "wamid.voz")
    assert fila.content == "quiero una cita para el martes"
    assert fila.media_url == AUDIO_URL
    assert fila.message_type == "audio"
    assert fila.metadata["transcription"] == {"model": MODELO, "duration_seconds": 3.2}
    # El payload crudo del proveedor no se pisa al reasignar el JSONB.
    assert fila.metadata["origen"] == "test"


async def test_una_transcripcion_ya_guardada_se_reusa(
    webhook_tenant: uuid.UUID, transcripciones_encoladas: list[dict[str, Any]]
) -> None:
    """Un reintento de la tarea no vuelve a pagarle a Whisper por el mismo audio."""
    await _process_message("ycloud", "whatsapp", _audio("wamid.voz"))
    mensaje_id = (await _mensaje(webhook_tenant, "wamid.voz")).id

    assert await _leer_transcripcion_previa(webhook_tenant, mensaje_id) is None

    await _guardar_transcripcion(
        webhook_tenant, mensaje_id, Transcription(text="hola", duration_seconds=1.0), MODELO
    )

    assert await _leer_transcripcion_previa(webhook_tenant, mensaje_id) == "hola"


# ─── Aislamiento entre tenants ───────────────────────────────────────────────


async def test_la_transcripcion_no_cruza_de_tenant(
    webhook_tenant: uuid.UUID,
    otro_tenant: uuid.UUID,
    transcripciones_encoladas: list[dict[str, Any]],
) -> None:
    """Con el UUID del mensaje de otro tenant, RLS no deja ni leerlo ni escribirlo."""
    await _process_message("ycloud", "whatsapp", _audio("wamid.voz"))
    mensaje_id = (await _mensaje(webhook_tenant, "wamid.voz")).id

    with pytest.raises(PermanentTranscriptionError):
        await _guardar_transcripcion(
            otro_tenant, mensaje_id, Transcription(text="ajeno", duration_seconds=1.0), MODELO
        )

    with pytest.raises(PermanentTranscriptionError):
        await _leer_transcripcion_previa(otro_tenant, mensaje_id)

    assert not (await _mensaje(webhook_tenant, "wamid.voz")).content
