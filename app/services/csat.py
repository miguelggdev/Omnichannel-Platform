"""CSATService — encuestas de satisfaccion post-resolucion (Sprint 11, Dev B).

Que envia y como
-----------------
Un solo mensaje de texto por todos los canales, via `deliver_message()`
(`app/agents/nodes/_delivery.py`, compartido con `respond`/`human_handoff`):
resuelve el proveedor del canal, envia y deja el mensaje en el historial en un
solo paso, asi que la encuesta aparece en la conversacion como cualquier otra
respuesta del sistema. El spec propone botones interactivos por canal
(`interactive_buttons`, `email_template`, `csat_widget`); `deliver_message()`
no tiene ese vocabulario (solo texto + metadata de hilo de email) y agregarlo
—junto con la captura de la respuesta por boton/widget— queda fuera de este
sprint (ver PROGRESS.md, "Sin resolver en esta entrega").

Como se captura la respuesta
-----------------------------
Por ahora, solo por el link de email (`GET /api/v1/csat/respond`,
`app/api/v1/csat.py`). Un texto libre del contacto ("5", "me atendieron mal")
en WhatsApp/Telegram/Webchat **no** se captura: `_resolve_conversation()` de
`webhook_processor.py` no busca conversaciones `resolved`, asi que esa
respuesta abriria una conversacion nueva en vez de encontrar la encuesta
pendiente. Conectar ese camino significa tocar el flujo critico de mensajes
entrantes que usan los demas canales; queda anotado como pendiente en vez de
half-implementado a las apuradas. `_extract_rating_from_text()` ya existe para
cuando se conecte.
"""

import logging
import re
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.satisfaction_survey import STATUS_RESPONDED, SatisfactionSurvey

logger = logging.getLogger(__name__)


def build_survey_message(
    channel: str,
    contact_name: str = "",
    survey_id: UUID | None = None,
    client_id: UUID | None = None,
) -> str:
    """Arma el texto de la encuesta CSAT.

    En email se agrega el link de respuesta de un clic
    (`GET /api/v1/csat/respond`): un correo no tiene el concepto de "responder
    con un numero" que si tienen WhatsApp o Telegram, y pedirle a alguien que
    escriba un email de vuelta con solo un digito es mucha friccion. El link
    lleva `client_id` ademas de `survey_id` porque el endpoint es publico (sin
    JWT) y `tenant_session()` necesita el tenant *antes* de poder leer la
    encuesta bajo RLS — la seguridad real la da `survey_id`, un UUID que nadie
    adivina; un `client_id` que no le corresponde simplemente no encuentra
    nada (ver el 404 de `respond_csat_via_link`).

    Args:
        channel: Canal por el que se envia.
        contact_name: Nombre del contacto, si se conoce.
        survey_id: Id de la encuesta ya creada (o por crear con ese mismo id);
            solo hace falta para el link de email.
        client_id: Tenant de la encuesta; solo hace falta para el link de email.

    Returns:
        Texto listo para `deliver_message()`.
    """
    saludo = f"Hola{' ' + contact_name if contact_name else ''}"
    pregunta = (
        f"{saludo}, tu conversacion fue resuelta. "
        "¿Como calificarias la atencion, del 1 (muy mala) al 5 (excelente)?"
    )

    base_url = get_settings().APP_PUBLIC_URL
    if channel == "email" and base_url and survey_id and client_id:
        enlaces = " · ".join(
            f"[{n}]({base_url}/api/v1/csat/respond?survey_id={survey_id}"
            f"&client_id={client_id}&rating={n})"
            for n in range(1, 6)
        )
        return f"{pregunta}\n\n{enlaces}"

    return f"{pregunta} Responde solo con el numero."


def extract_rating_from_text(text: str | None) -> int | None:
    """Extrae una calificacion 1-5 de una respuesta de texto libre.

    Busca un digito del 1 al 5 aislado (no dentro de otro numero, para que
    "el 15 de mayo" no se lea como un 1 o un 5). El comentario opcional que
    venga junto al numero no se descarta aca: `process_response()` lo guarda
    aparte.

    Args:
        text: Texto de la respuesta del contacto.

    Returns:
        La calificacion, o `None` si no se encuentra ninguna.
    """
    if not text:
        return None
    match = re.search(r"(?<!\d)([1-5])(?!\d)", text.strip())
    return int(match.group(1)) if match else None


async def survey_exists(session: AsyncSession, conversation_id: UUID) -> bool:
    """Si ya hay una encuesta (de cualquier status) para esta conversacion.

    La usa `send_csat_survey` (`app/tasks/csat_tasks.py`) para no duplicar el
    envio: el `UNIQUE` de la migracion 012 es la garantia de fondo, esto evita
    el viaje extra a la base cuando ya se sabe la respuesta.

    Args:
        session: Sesion con contexto de tenant.
        conversation_id: Conversacion a comprobar.

    Returns:
        `True` si ya existe una fila en `satisfaction_surveys` para ella.
    """
    stmt = select(SatisfactionSurvey.id).where(
        SatisfactionSurvey.conversation_id == conversation_id
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def process_response(
    session: AsyncSession,
    client_id: UUID,
    survey_id: UUID,
    rating: int,
    comment: str | None = None,
) -> SatisfactionSurvey | None:
    """Registra la respuesta a una encuesta CSAT.

    Args:
        session: Sesion con contexto de tenant.
        client_id: Tenant propietario de la encuesta.
        survey_id: Encuesta que se responde.
        rating: Calificacion 1-5. Fuera de ese rango, no se registra.
        comment: Comentario opcional.

    Returns:
        La encuesta ya actualizada, o `None` si no existe, ya fue respondida o
        el rating esta fuera de rango.
    """
    if not 1 <= rating <= 5:
        return None

    survey = (
        await session.execute(
            select(SatisfactionSurvey).where(
                SatisfactionSurvey.id == survey_id, SatisfactionSurvey.client_id == client_id
            )
        )
    ).scalar_one_or_none()
    if survey is None:
        logger.info("Encuesta CSAT %s no encontrada (tenant %s)", survey_id, client_id)
        return None
    if survey.status == STATUS_RESPONDED:
        logger.info("Encuesta CSAT %s ya estaba respondida", survey_id)
        return survey

    survey.rating = rating
    survey.comment = comment
    survey.responded_at = datetime.now(timezone.utc)
    survey.status = STATUS_RESPONDED
    await session.flush()

    logger.info(
        "CSAT %s respondida: rating=%s (conversacion %s)",
        survey_id,
        rating,
        survey.conversation_id,
    )
    return survey
