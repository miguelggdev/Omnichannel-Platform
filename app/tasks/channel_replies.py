"""Worker de Celery que envia respuestas fijas del sistema por un canal.

Cola: `notifications` (el nombre empieza por `app.tasks.notification_`). Es para
mensajes que no genera la IA — hoy, pedir y agradecer el telefono en Telegram
(`app/services/contact_request.py`) — y que hay que enviar y dejar en el historial
igual que cualquier otra respuesta del bot (`deliver_message`).

Reintentos: 3, con backoff. Agotados, se registra como CRITICAL y ya: a diferencia
de una respuesta de la IA, no hay una persona esperando una contestacion a una
pregunta, asi que no se escala a un humano.
"""

import logging
from typing import Any
from uuid import UUID

from celery import shared_task

from app.core.database import run_isolated

logger = logging.getLogger(__name__)

RETRY_BACKOFF_SECONDS = (5, 25, 125)


async def _enviar(
    client_id: str,
    conversation_id: str,
    contact_id: str,
    channel: str,
    text: str,
    metadata: dict[str, Any] | None,
) -> None:
    """Envia la respuesta y la registra como mensaje saliente.

    Args:
        client_id: Tenant propietario.
        conversation_id: Conversacion a la que se responde.
        contact_id: Contacto destinatario.
        channel: Canal por el que responder.
        text: Texto a enviar.
        metadata: Datos propios del canal para el envio.
    """
    from app.agents.nodes._delivery import deliver_message

    await deliver_message(
        client_id=UUID(client_id),
        conversation_id=UUID(conversation_id),
        contact_id=UUID(contact_id),
        channel=channel,
        text=text,
        metadata=metadata,
    )


@shared_task(
    name="app.tasks.notification_send_channel_reply",
    bind=True,
    max_retries=3,
    acks_late=True,
    queue="notifications",
    time_limit=60,
    soft_time_limit=45,
)
def send_channel_reply(
    self: Any,
    client_id: str,
    conversation_id: str,
    contact_id: str,
    channel: str,
    text: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Envia una respuesta fija del sistema por el canal del contacto.

    Args:
        self: Instancia de la tarea (bind=True), para los reintentos.
        client_id: Tenant propietario.
        conversation_id: Conversacion a la que se responde.
        contact_id: Contacto destinatario.
        channel: Canal por el que responder.
        text: Texto a enviar.
        metadata: Datos propios del canal para el envio.

    Returns:
        `{"status": "sent"}` o `{"status": "failed"}` si se agotaron los reintentos.

    Raises:
        Retry: Reintento ante un fallo transitorio.
    """
    try:
        run_isolated(_enviar(client_id, conversation_id, contact_id, channel, text, metadata))
    except Exception as exc:
        if self.request.retries < self.max_retries:
            espera = RETRY_BACKOFF_SECONDS[
                min(self.request.retries, len(RETRY_BACKOFF_SECONDS) - 1)
            ]
            logger.warning(
                "Fallo el envio de la respuesta fija en %s (intento %s/%s): %s",
                conversation_id,
                self.request.retries + 1,
                self.max_retries,
                type(exc).__name__,
            )
            raise self.retry(exc=exc, countdown=espera) from exc
        logger.critical(
            "Respuesta fija sin enviar tras %s intentos en %s: %s",
            self.max_retries,
            conversation_id,
            type(exc).__name__,
            exc_info=True,
        )
        return {"status": "failed"}
    return {"status": "sent"}
