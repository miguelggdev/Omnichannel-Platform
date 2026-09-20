"""Worker de Celery que procesa los webhooks encolados por el endpoint.

Cola: `webhooks` (concurrency=4 en docker-compose). El nombre de la tarea empieza
por `app.tasks.webhook_` para que el `task_routes` de `celery_config.py` la enrute
sola a esa cola.

Flujo por mensaje (todo dentro de UNA transaccion con SET LOCAL):

1. Resolver el `client_id` del tenant.
2. Verificar en `webhook_dedup` que no se haya procesado ya (nivel 2).
3. Resolver o crear el contacto por (canal, identifier).
4. Resolver o crear la conversacion activa del contacto en ese canal.
5. Guardar el mensaje y actualizar `last_message_at`.
6. Registrar en `webhook_dedup`.
7. Encolar el procesamiento de IA, salvo que la conversacion ya sea de un humano
   (`HUMAN_OWNED_STATUSES`): el bot no le responde a un contacto que un agente
   ya esta atendiendo o que acaba de ser escalado.

Reintentos: 5s -> 25s -> 125s (exponencial). Tras 3 fallos el mensaje va a la Dead
Letter Queue de Redis (`dlq:webhook_messages`) para revision manual.
"""

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import run_isolated, tenant_session
from app.core.encryption import blind_index, mask_identifier
from app.core.metrics import record_message
from app.models.contact import Contact
from app.models.contact_identifier import ContactIdentifier
from app.models.conversation import Conversation
from app.models.message import Message
from app.schemas.message import NormalizedMessage
from app.services.dedup import get_redis, is_duplicate_persisted, persist_dedup

if TYPE_CHECKING:
    from collections.abc import Awaitable

logger = logging.getLogger(__name__)

# Lista de Redis donde aterrizan los mensajes que agotaron los reintentos.
DLQ_KEY = "dlq:webhook_messages"

# Backoff exponencial de los reintentos: 5s, 25s, 125s (5 * 5**intento).
RETRY_BASE_DELAY_SECONDS = 5
RETRY_BACKOFF_FACTOR = 5

# Estados en los que una conversacion se considera cerrada: si el contacto vuelve a
# escribir se abre una nueva en lugar de reabrir estas.
CLOSED_STATUSES = ("resolved", "archived")

# Estados en los que un humano ya es dueno de la conversacion. El grafo de IA
# (Sprint 6) no sabe nada de `conversations.status`: si se la encola igual, el bot
# le responde a un contacto que un agente ya esta atendiendo, o al que se acaba de
# escalar y todavia no lo tomo nadie.
HUMAN_OWNED_STATUSES = ("human_active", "waiting_human")


class ClientResolutionError(RuntimeError):
    """No se pudo determinar a que tenant pertenece el mensaje entrante."""


def _resolve_client_id(provider: str, channel: str) -> UUID:
    """Determina el tenant dueno del mensaje.

    Los webhooks no llevan JWT, asi que el tenant no se puede deducir del request.
    En el MVP se resuelve por `DEFAULT_CLIENT_ID`. Cuando exista la tabla
    `channel_configs` (Fase 2) se resolvera por la cuenta de canal que recibio el
    mensaje, y esta funcion sera el unico punto a cambiar.

    Args:
        provider: Proveedor de origen (ycloud, meta).
        channel: Canal de origen (whatsapp, instagram, facebook).

    Returns:
        UUID del tenant.

    Raises:
        ClientResolutionError: Si no hay `DEFAULT_CLIENT_ID` o no es un UUID valido.
    """
    raw = get_settings().DEFAULT_CLIENT_ID
    if not raw:
        raise ClientResolutionError(
            f"No hay DEFAULT_CLIENT_ID configurado; imposible resolver el tenant "
            f"para provider={provider} channel={channel}"
        )
    try:
        return UUID(raw)
    except ValueError as exc:
        raise ClientResolutionError(f"DEFAULT_CLIENT_ID no es un UUID valido: {raw}") from exc


async def _contacto_del_tenant(
    session: AsyncSession, client_id: UUID, contact_id: UUID
) -> Contact | None:
    """Carga un contacto del tenant con el `client_id` explicito en el WHERE.

    `session.get()` no deja ver ningun filtro: si la politica RLS fallara en
    `contacts`, devolveria el contacto de otro tenant sin que nada avisara. Aqui
    el id viene de una fila ya filtrada por tenant (`contact_identifiers`) o de
    `merged_into_id`, asi que el riesgo real es bajo, pero es el mismo patron
    que el resto del proyecto exige para no depender solo de RLS.

    Args:
        session: Sesion con el contexto de tenant ya aplicado.
        client_id: Tenant propietario.
        contact_id: Contacto buscado.

    Returns:
        El contacto, o `None` si no existe en ese tenant.
    """
    return (
        await session.execute(
            select(Contact).where(Contact.id == contact_id, Contact.client_id == client_id)
        )
    ).scalar_one_or_none()


async def _resolve_contact(
    session: AsyncSession,
    client_id: UUID,
    channel: str,
    identifier_value: str,
) -> Contact:
    """Busca el contacto por (canal, identifier) o lo crea.

    Si el contacto encontrado fue fusionado (`merged_into_id`), se sigue la cadena
    hasta el contacto superviviente.

    Args:
        session: Sesion con el contexto de tenant ya aplicado (SET LOCAL).
        client_id: Tenant propietario.
        channel: Canal del identificador.
        identifier_value: Telefono, PSID o username segun el canal.

    Returns:
        Contacto existente o recien creado.
    """
    # Por el hash, no por el valor: `identifier_value` esta cifrado con un IV
    # aleatorio (Sprint 8), asi que comparar contra el ciphertext de esta
    # llamada nunca encontraria la fila. Ver app/core/encryption.py.
    stmt = select(ContactIdentifier).where(
        ContactIdentifier.client_id == client_id,
        ContactIdentifier.channel == channel,
        ContactIdentifier.identifier_hash == blind_index(identifier_value),
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()

    if existing is not None:
        contact = await _contacto_del_tenant(session, client_id, existing.contact_id)
        # Seguir la cadena de merge hasta el contacto superviviente.
        seen: set[UUID] = set()
        while contact is not None and contact.merged_into_id is not None:
            if contact.id in seen:  # proteccion ante un ciclo de merges corrupto
                logger.error("Ciclo de merges detectado en contacto %s", contact.id)
                break
            seen.add(contact.id)
            contact = await _contacto_del_tenant(session, client_id, contact.merged_into_id)
        if contact is not None:
            return contact

    # Enmascarado, no el valor completo: display_name no esta cifrado y el CRM
    # lo busca con ILIKE (MEMORY.md, "el telefono queda en claro"). Solo dura
    # hasta que un agente le pone un nombre real al contacto.
    contact = Contact(client_id=client_id, display_name=mask_identifier(identifier_value))
    session.add(contact)
    await session.flush()  # necesitamos contact.id para el identifier

    session.add(
        ContactIdentifier(
            client_id=client_id,
            contact_id=contact.id,
            channel=channel,
            identifier_value=identifier_value,
        )
    )
    await session.flush()
    return contact


async def _resolve_conversation(
    session: AsyncSession,
    client_id: UUID,
    contact_id: UUID,
    channel: str,
) -> Conversation:
    """Busca la conversacion activa del contacto en el canal, o crea una nueva.

    Activa = su status no esta en `CLOSED_STATUSES`. Si hay varias, gana la de
    actividad mas reciente.

    Args:
        session: Sesion con el contexto de tenant ya aplicado (SET LOCAL).
        client_id: Tenant propietario.
        contact_id: Contacto de la conversacion.
        channel: Canal de la conversacion.

    Returns:
        Conversacion existente o recien creada con status `bot_active`.
    """
    stmt = (
        select(Conversation)
        .where(
            Conversation.client_id == client_id,
            Conversation.contact_id == contact_id,
            Conversation.channel == channel,
            Conversation.status.not_in(CLOSED_STATUSES),
        )
        .order_by(Conversation.last_message_at.desc().nullslast())
        .limit(1)
    )
    conversation = (await session.execute(stmt)).scalar_one_or_none()
    if conversation is not None:
        return conversation

    conversation = Conversation(
        client_id=client_id,
        contact_id=contact_id,
        channel=channel,
        status="bot_active",
    )
    session.add(conversation)
    await session.flush()  # necesitamos conversation.id para el mensaje
    return conversation


def _parse_timestamp(value: Any) -> datetime:
    """Convierte el timestamp del mensaje normalizado a datetime con timezone.

    Args:
        value: ISO-8601 serializado por el endpoint, o ya un datetime.

    Returns:
        Datetime en UTC. Ante un valor invalido, el momento actual.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("Timestamp invalido en webhook: %r", value)
            return datetime.now(timezone.utc)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


async def _process_message(provider: str, channel: str, message_data: dict[str, Any]) -> None:
    """Logica asincrona de procesamiento de un mensaje entrante.

    El dict llega serializado desde el endpoint (`model_dump(mode="json")`) y aqui
    se revalida con `NormalizedMessage`: si el payload viene incompleto o con un
    canal que no existe, salta un ValidationError antes de tocar la base, y la
    tarea lo trata como fallo reintentable.

    Args:
        provider: Proveedor de origen (ycloud, meta).
        channel: Canal de la URL del webhook.
        message_data: NormalizedMessage serializado con `model_dump(mode="json")`.

    Raises:
        ClientResolutionError: Si no se puede determinar el tenant.
        ValidationError: Si el mensaje normalizado no cumple el schema.
    """
    client_id = _resolve_client_id(provider, channel)

    normalized = NormalizedMessage(**message_data)
    external_id = normalized.external_message_id
    message_channel = normalized.channel.value
    sender_identifier = normalized.sender_identifier
    timestamp = _parse_timestamp(normalized.timestamp)

    async with tenant_session(client_id) as session:
        # Dedup nivel 2: si Redis perdio la clave, aqui se corta igual.
        if await is_duplicate_persisted(client_id, message_channel, external_id, session=session):
            logger.info("Mensaje ya procesado (webhook_dedup): %s", external_id)
            return

        contact = await _resolve_contact(session, client_id, message_channel, sender_identifier)
        conversation = await _resolve_conversation(session, client_id, contact.id, message_channel)

        session.add(
            Message(
                client_id=client_id,
                conversation_id=conversation.id,
                direction="inbound",
                message_type=(normalized.media_type.value if normalized.media_type else "text"),
                content=normalized.text,
                media_url=normalized.media_url,
                external_message_id=external_id,
                sender_type="contact",
                sender_id=contact.id,
                metadata_=normalized.raw_payload or {},
            )
        )
        conversation.last_message_at = timestamp
        conversation_status = conversation.status

        await persist_dedup(client_id, message_channel, external_id, session=session)

    # Despues del commit: un mensaje que no llego a persistirse no es un mensaje
    # procesado, y contarlo aqui dejaria la metrica por encima de la tabla.
    record_message(str(client_id), message_channel, "inbound")

    if conversation_status in HUMAN_OWNED_STATUSES:
        logger.info(
            "Conversacion %s en manos de un humano (status=%s); no se encola IA",
            conversation.id,
            conversation_status,
        )
        return

    # Fuera de la transaccion: la IA no debe encolarse si el commit fallo.
    _enqueue_ai_processing(
        client_id=client_id,
        conversation_id=conversation.id,
        contact_id=contact.id,
        channel=message_channel,
        message_data=message_data,
    )


def _enqueue_ai_processing(
    client_id: UUID,
    conversation_id: UUID,
    contact_id: UUID,
    channel: str,
    message_data: dict[str, Any],
) -> None:
    """Encola el procesamiento de IA de la conversacion.

    Desde Sprint 6 `app.tasks.ai_processor` existe y el mensaje entra al grafo de
    agentes.

    Nada de lo que pase aqui propaga: ni el ImportError ni un broker caido. Para
    cuando se llega a esta funcion, el mensaje ya esta commiteado **y** marcado
    en `webhook_dedup`, asi que un reintento de la tarea completa se cortaria en
    la comprobacion de duplicado sin volver a encolar la IA — reintentar no
    arregla nada y solo suma ruido. El fallo se registra como CRITICAL: es un
    mensaje del contacto que se quedo sin respuesta automatica y alguien lo
    tiene que ver.

    Args:
        client_id: Tenant propietario.
        conversation_id: Conversacion a procesar.
        contact_id: Contacto que escribio.
        channel: Canal de origen.
        message_data: NormalizedMessage serializado.
    """
    try:
        from app.tasks.ai_processor import process_ai_response
    except ImportError:
        logger.error(
            "ai_processor no importable; mensaje guardado sin encolar IA: conversation_id=%s",
            conversation_id,
        )
        return

    try:
        process_ai_response.delay(
            client_id=str(client_id),
            conversation_id=str(conversation_id),
            contact_id=str(contact_id),
            channel=channel,
            message_data=message_data,
        )
    except Exception:
        logger.critical(
            "No se pudo encolar la IA; el mensaje quedo guardado y sin respuesta "
            "automatica: conversation_id=%s",
            conversation_id,
            exc_info=True,
        )


async def _send_to_dlq(provider: str, channel: str, message_data: dict[str, Any]) -> None:
    """Empuja un mensaje agotado de reintentos a la Dead Letter Queue.

    Args:
        provider: Proveedor de origen.
        channel: Canal de origen.
        message_data: NormalizedMessage serializado.
    """
    entry = json.dumps(
        {
            "provider": provider,
            "channel": channel,
            "message": message_data,
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "retries_exhausted": True,
        }
    )
    try:
        # redis-py tipa lpush como `Awaitable[int] | int`; con el cliente asincrono
        # siempre es awaitable.
        await cast("Awaitable[int]", get_redis().lpush(DLQ_KEY, entry))
    except Exception:
        # Si tampoco hay Redis, el log es el ultimo registro que queda del mensaje.
        logger.exception("No se pudo escribir en la DLQ; mensaje perdido: %s", entry)


@shared_task(
    name="app.tasks.webhook_process_incoming",
    bind=True,
    max_retries=3,
    acks_late=True,
    queue="webhooks",
)
def process_incoming_message(
    self: Any,
    provider: str,
    channel: str,
    normalized_message: dict[str, Any],
) -> dict[str, str]:
    """Procesa un mensaje entrante normalizado.

    Args:
        self: Instancia de la tarea (bind=True), para los reintentos.
        provider: Proveedor de origen (ycloud, meta).
        channel: Canal de la URL del webhook.
        normalized_message: NormalizedMessage serializado.

    Returns:
        `{"status": "processed"}` o `{"status": "dlq"}` si agoto los reintentos.

    Raises:
        Retry: Reintento con backoff exponencial (5s, 25s, 125s).
    """
    try:
        run_isolated(_process_message(provider, channel, normalized_message))
    except Exception as exc:
        if self.request.retries < self.max_retries:
            countdown = RETRY_BASE_DELAY_SECONDS * (RETRY_BACKOFF_FACTOR**self.request.retries)
            logger.warning(
                "Fallo procesando webhook (intento %s/%s, reintento en %ss): %s",
                self.request.retries + 1,
                self.max_retries,
                countdown,
                exc,
            )
            raise self.retry(exc=exc, countdown=countdown) from exc

        logger.critical(
            "Mensaje a DLQ tras %s intentos: %s",
            self.max_retries,
            exc,
            exc_info=True,
        )
        run_isolated(_send_to_dlq(provider, channel, normalized_message))
        return {"status": "dlq"}

    return {"status": "processed"}
