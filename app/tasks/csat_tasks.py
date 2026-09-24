"""Tasks de Celery del CSAT (Sprint 11, Dev B).

Como se dispara
----------------
Este modulo registra `_on_conversation_resolved` como handler de
`EventEmitter.on(EVENT_CONVERSATION_RESOLVED, ...)` al importarse (ver el final
del archivo). Sin importar este modulo en el proceso que emite el evento (la
API, en `PUT /conversations/{id}/status`), el handler nunca se registra y
ninguna encuesta se programa — por eso `app.main.create_app()` lo importa,
ademas de estar en `TASK_MODULES` para el worker.

Nombres de las tasks
---------------------
`app.tasks.notification_send_csat_survey` (cola `notifications`, mismo motivo
que las de webhooks salientes: `app.tasks.notification_*` es el prefijo que
`celery_config.py` enruta ahi) y `app.tasks.bulk_expire_csat_surveys` (cola
`bulk`: recorre todos los tenants, igual que `bulk_auto_close_conversations`).

Dos transacciones cortas, no una
----------------------------------
`send_csat_survey` lee la conversacion en una `tenant_session()`, la cierra,
llama a `deliver_message()` (que abre y cierra la suya propia para enviar y
persistir el mensaje) y recien despues abre una tercera para guardar la
encuesta. Mantener una sola sesion abierta durante el envio retendria una
conexion del Supavisor en modo transaccion durante todo el POST al proveedor
de mensajeria — mismo hallazgo que la revision cruzada del PR #40 le hizo a
`_enviar()` en `outgoing_webhooks.py` (ADR-065).
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from celery import shared_task
from sqlalchemy import select
from sqlalchemy import update as sa_update

from app.agents.nodes._delivery import deliver_message
from app.core.config import get_settings
from app.core.database import run_isolated, tenant_session
from app.core.events import EVENT_CONVERSATION_RESOLVED, EventEmitter
from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.satisfaction_survey import SatisfactionSurvey
from app.services.csat import build_survey_message, survey_exists

# Privada y reusada tal cual: su propio docstring en auto_close.py dice que es
# "el unico punto a cambiar" cuando aparezca un rol de servicio o
# `channel_configs`; duplicarla aca la dejaria desactualizada el dia que eso
# pase.
from app.tasks.auto_close import _load_active_client_ids

logger = logging.getLogger(__name__)

# Cuanto se espera a que Celery acepte el encolado desde el handler de
# EventEmitter, que corre dentro del request de la API. Mismo corte y mismo
# motivo que `ENQUEUE_TIMEOUT_SECONDS` en `app/core/events.py`.
ENQUEUE_TIMEOUT_SECONDS = 5.0


async def _enviar_encuesta(client_id: str, conversation_id: str) -> dict[str, str]:
    """Envia la encuesta CSAT de una conversacion, si todavia corresponde.

    No manda nada si la conversacion se reabrio despues de programarse (ya no
    esta `resolved`) o si ya existe una encuesta para ella.

    Args:
        client_id: Tenant propietario, serializado.
        conversation_id: Conversacion recien resuelta, serializada.

    Returns:
        `{"status": "sent" | "skipped_not_resolved" | "skipped_duplicate" | "skipped_send_failed"}`.
    """
    tenant_id = UUID(client_id)
    conv_id = UUID(conversation_id)

    async with tenant_session(tenant_id) as session:
        conversation = (
            await session.execute(
                select(Conversation).where(
                    Conversation.id == conv_id, Conversation.client_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if conversation is None or conversation.status != "resolved":
            logger.info("Conversacion %s ya no esta resuelta; se cancela la encuesta CSAT", conv_id)
            return {"status": "skipped_not_resolved"}

        if await survey_exists(session, conv_id):
            return {"status": "skipped_duplicate"}

        contact = (
            await session.execute(
                select(Contact).where(
                    Contact.id == conversation.contact_id, Contact.client_id == tenant_id
                )
            )
        ).scalar_one_or_none()

        contact_id = conversation.contact_id
        channel = conversation.channel
        contact_name = (contact.first_name if contact else "") or ""

    # Generado en Python, no dejado al `server_default` de la tabla: el link
    # de email (`build_survey_message`) necesita el id de la encuesta *antes*
    # de que la fila exista.
    survey_id = uuid4()
    texto = build_survey_message(channel, contact_name, survey_id=survey_id, client_id=tenant_id)

    try:
        external_id = await deliver_message(
            client_id=tenant_id,
            conversation_id=conv_id,
            contact_id=contact_id,
            channel=channel,
            text=texto,
        )
    except Exception:
        logger.exception(
            "No se pudo enviar la encuesta CSAT de la conversacion %s; no se registra", conv_id
        )
        return {"status": "skipped_send_failed"}

    async with tenant_session(tenant_id) as session:
        # Si dos disparos concurrentes llegaran hasta aca (no deberia: cada
        # resolucion programa un solo `send_csat_survey`), el segundo INSERT
        # rebota contra `uq_csat_conversation_id` — mejor eso que dos encuestas.
        session.add(
            SatisfactionSurvey(
                id=survey_id,
                client_id=tenant_id,
                conversation_id=conv_id,
                contact_id=contact_id,
                channel=channel,
                sent_at=datetime.now(timezone.utc),
                survey_message_id=external_id,
                status="sent",
            )
        )

    logger.info("Encuesta CSAT enviada: conversacion=%s canal=%s", conv_id, channel)
    return {"status": "sent"}


@shared_task(
    name="app.tasks.notification_send_csat_survey",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    acks_late=True,
    queue="notifications",
    time_limit=60,
    soft_time_limit=45,
)
def send_csat_survey(self: Any, client_id: str, conversation_id: str) -> dict[str, str]:
    """Envia la encuesta CSAT de una conversacion resuelta.

    Se programa con `countdown` desde el handler de `conversation.resolved`,
    minutos despues de la resolucion.

    Args:
        self: Instancia de la tarea (bind=True).
        client_id: Tenant propietario, serializado.
        conversation_id: Conversacion recien resuelta, serializada.

    Returns:
        El resultado del envio (ver `_enviar_encuesta`).

    Raises:
        Retry: Ante un fallo transitorio antes de enviar nada (leer la
            conversacion). Un fallo del envio en si no se reintenta a ciegas:
            `_enviar_encuesta` ya lo atrapa y devuelve `skipped_send_failed` en
            vez de dejar que suba, porque el mensaje podria haber salido.
    """
    try:
        return run_isolated(_enviar_encuesta(client_id, conversation_id))
    except Exception as exc:
        logger.exception("Fallo enviando la encuesta CSAT de %s", conversation_id)
        raise self.retry(exc=exc) from exc


async def _expirar_encuestas(tenant_id: UUID) -> int:
    """Marca `expired` las encuestas `sent` de un tenant, vencido el plazo.

    Args:
        tenant_id: Tenant a barrer.

    Returns:
        Cuantas encuestas se expiraron.
    """
    corte = datetime.now(timezone.utc) - timedelta(hours=get_settings().CSAT_SURVEY_EXPIRY_HOURS)

    async with tenant_session(tenant_id) as session:
        # `Any`: `AsyncSession.execute()` tipa `Result[Any]`, y `rowcount` solo
        # lo expone de forma segura un `CursorResult` (mismo patron que
        # `auto_close._resolve_stale_waiting_client`).
        resultado: Any = await session.execute(
            sa_update(SatisfactionSurvey)
            .where(
                SatisfactionSurvey.client_id == tenant_id,
                SatisfactionSurvey.status == "sent",
                SatisfactionSurvey.sent_at < corte,
            )
            .values(status="expired")
        )
        return int(resultado.rowcount or 0)


async def _expirar_todas() -> dict[str, int]:
    """Recorre los tenants alcanzables expirando encuestas vencidas.

    Mismo patron que `auto_close._auto_close()`: un tenant con error no corta
    el recorrido de los demas.

    Returns:
        Cuantas encuestas se expiraron y cuantos tenants se recorrieron.
    """
    client_ids = await _load_active_client_ids()
    expiradas = 0
    tenants_con_error = 0

    for client_id in client_ids:
        try:
            expiradas += await _expirar_encuestas(client_id)
        except Exception:
            tenants_con_error += 1
            logger.exception("Expiracion de encuestas CSAT fallida para el tenant %s", client_id)

    return {"expired": expiradas, "tenants": len(client_ids), "tenants_failed": tenants_con_error}


@shared_task(
    name="app.tasks.bulk_expire_csat_surveys",
    queue="bulk",
    acks_late=True,
    time_limit=300,
    soft_time_limit=270,
)
def expire_old_surveys() -> dict[str, int]:
    """Marca como `expired` las encuestas sin responder que vencieron el plazo.

    Corre por Celery Beat. No reintenta: la siguiente pasada vuelve a encontrar
    las mismas filas (la condicion es por antiguedad, no por una marca que se
    consuma).

    Returns:
        Conteo de encuestas expiradas y tenants recorridos.
    """
    return run_isolated(_expirar_todas())


async def _on_conversation_resolved(event: str, client_id: str, data: dict[str, Any]) -> None:
    """Handler de `EventEmitter` que programa la encuesta CSAT.

    Registrado sobre `conversation.resolved`: no hace ninguna consulta a la
    base (`send_csat_survey` decide todo lo que necesita saber cuando le toca
    correr), solo encola. `apply_async()` habla con Redis de forma sincrona, y
    esto corre dentro de un request async de la API (quien emite
    `conversation.resolved`) — bloquear el loop por un Redis lento no es
    aceptable (regla 4 de CLAUDE.md), asi que el encolado se manda a un hilo
    con corte de tiempo, igual que el despacho hardcodeado de
    `EventEmitter.emit()`.

    Args:
        event: Nombre del evento (siempre `conversation.resolved`).
        client_id: Tenant propietario, serializado.
        data: Payload del evento; solo se usa `conversation_id`.
    """
    conversation_id = data.get("conversation_id")
    if not conversation_id:
        logger.warning("conversation.resolved sin conversation_id; no se programa CSAT")
        return

    delay_minutes = get_settings().CSAT_SURVEY_DELAY_MINUTES

    # `apply_async` se liga aca, en el hilo que ya tiene la app de Celery
    # configurada, y no dentro de `_encolar()`: el proxy de `shared_task` se
    # resuelve por hilo (ADR-065), y resolverlo fresco dentro del executor
    # daria la app por defecto de Celery (broker amqp://localhost), no la
    # configurada. Al hilo solo viaja la llamada.
    encolar = send_csat_survey.apply_async

    def _encolar() -> None:
        encolar(
            kwargs={"client_id": client_id, "conversation_id": conversation_id},
            countdown=delay_minutes * 60,
            retry=False,
        )

    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _encolar),
            timeout=ENQUEUE_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("No se pudo programar la encuesta CSAT de %s", conversation_id)


EventEmitter.on(EVENT_CONVERSATION_RESOLVED, _on_conversation_resolved)
