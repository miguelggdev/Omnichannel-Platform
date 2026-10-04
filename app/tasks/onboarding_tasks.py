"""Tasks del onboarding: email de verificacion (Sprint 15, Fase 2).

El token se genera aqui, dentro de la task, y no al encolarla: asi no viaja por el
broker un JWT que da acceso a verificar la cuenta, solo los ids y el email.
Si el SMTP no esta configurado la task no falla: lo registra y termina, porque la
verificacion no bloquea el uso de la cuenta (no hay enforcement todavia).
"""

import asyncio
import logging
from typing import Any

from celery import shared_task

from app.core.config import get_settings
from app.core.security import create_email_verification_token
from app.services.messaging.base import MessageContent
from app.services.messaging.email_provider import EmailProvider

logger = logging.getLogger(__name__)

_ASUNTO = "Verifica tu correo / Verify your email"
_CUERPO = (
    "Hola,\n\nConfirma tu correo con este enlace (vale {horas} horas):\n{enlace}\n\n"
    "Hello,\n\nConfirm your email with this link (valid for {horas} hours):\n{enlace}\n"
)


async def _enviar(client_id: str, user_id: str, email: str) -> str:
    """Envia el email de verificacion.

    Args:
        client_id: Tenant del usuario.
        user_id: Usuario.
        email: Destinatario.

    Returns:
        `sent` o `skipped_smtp_not_configured`.
    """
    settings = get_settings()
    if not settings.EMAIL_SMTP_HOST or not settings.EMAIL_FROM_ADDRESS:
        logger.warning("SMTP sin configurar: no se envia la verificacion de %s", user_id)
        return "skipped_smtp_not_configured"
    token = create_email_verification_token(user_id, client_id, email)
    enlace = f"{settings.FRONTEND_PUBLIC_URL.rstrip('/')}/verify-email?token={token}"
    config: dict[str, Any] = {
        "smtp_host": settings.EMAIL_SMTP_HOST,
        "smtp_port": settings.EMAIL_SMTP_PORT,
        "smtp_user": settings.EMAIL_SMTP_USER,
        "smtp_password": settings.EMAIL_SMTP_PASSWORD,
        "from_email": settings.EMAIL_FROM_ADDRESS,
        "from_name": settings.EMAIL_FROM_NAME,
    }
    contenido = MessageContent(
        text=_CUERPO.format(horas=settings.EMAIL_VERIFICATION_TTL_HOURS, enlace=enlace),
        metadata={"subject": _ASUNTO},
    )
    await EmailProvider().send_message(email, contenido, config)
    return "sent"


@shared_task(
    name="app.tasks.notification_send_verification_email",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    queue="notifications",
    time_limit=60,
    soft_time_limit=45,
)
def send_verification_email(self: Any, client_id: str, user_id: str, email: str) -> str:
    """Envia el enlace de verificacion de email al administrador recien registrado.

    Args:
        self: Instancia de la tarea (bind=True).
        client_id: Tenant del usuario.
        user_id: Usuario.
        email: Destinatario.

    Returns:
        Resultado del envio.

    Raises:
        Retry: Si el servidor SMTP falla (reintenta hasta 3 veces).
    """
    try:
        return asyncio.run(_enviar(client_id, user_id, email))
    except Exception as exc:
        logger.exception("Fallo enviando la verificacion de %s", user_id)
        raise self.retry(exc=exc) from exc
