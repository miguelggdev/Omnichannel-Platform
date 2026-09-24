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
from app.models.satisfaction_survey import STATUS_EXPIRED, STATUS_RESPONDED, SatisfactionSurvey

logger = logging.getLogger(__name__)


def build_survey_message(
    channel: str,
    contact_name: str = "",
    survey_id: UUID | None = None,
    client_id: UUID | None = None,
) -> str:
    """Arma el texto de la encuesta CSAT.

    En email se agrega el link de respuesta (`GET /api/v1/csat/respond`): un
    correo no tiene el concepto de "responder con un numero" que si tienen
    WhatsApp o Telegram, y pedirle a alguien que escriba un email de vuelta
    con solo un digito es mucha friccion. El link abre una pagina de
    confirmacion, no registra nada por si solo — un GET que grabara el rating
    de una vez quedaria a merced de cualquier escaner de enlaces de un gateway
    de correo que prefetchee los 5 links del mensaje (ver el docstring de
    `app/api/v1/csat.py`); el registro real pasa por el POST que confirma.
    El link lleva `client_id` ademas de `survey_id` porque el endpoint es
    publico (sin JWT) y `tenant_session()` necesita el tenant *antes* de poder
    leer la encuesta bajo RLS — la seguridad real la da `survey_id`, un UUID
    que nadie adivina; un `client_id` que no le corresponde simplemente no
    encuentra nada (ver el 404 de `respond_csat_via_link`).

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


async def survey_exists(session: AsyncSession, client_id: UUID, conversation_id: UUID) -> bool:
    """Si ya hay una encuesta (de cualquier status) para esta conversacion.

    La usa `send_csat_survey` (`app/tasks/csat_tasks.py`) para no duplicar el
    envio: el `UNIQUE` de la migracion 012 es la garantia de fondo, esto evita
    el viaje extra a la base cuando ya se sabe la respuesta.

    `client_id` va explicito en el WHERE ademas de la RLS de la sesion (CLAUDE.md
    §2, "las queries de seguridad deben ser explicitas"), igual que el resto de
    las consultas nuevas de este sprint: sin el, esta funcion pasaria a depender
    por completo de que quien la llame ya este dentro de un `tenant_session()`
    correcto, sin nada que lo verifique en la propia query.

    Args:
        session: Sesion con contexto de tenant.
        client_id: Tenant propietario de la conversacion.
        conversation_id: Conversacion a comprobar.

    Returns:
        `True` si ya existe una fila en `satisfaction_surveys` para ella.
    """
    stmt = select(SatisfactionSurvey.id).where(
        SatisfactionSurvey.client_id == client_id,
        SatisfactionSurvey.conversation_id == conversation_id,
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
        La encuesta ya actualizada, o `None` si no existe, ya expiro o el
        rating esta fuera de rango. Si ya estaba respondida, devuelve la
        encuesta tal cual sin sobreescribir el rating anterior.
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
    if survey.status == STATUS_EXPIRED:
        # Un link de email viejo no puede revivir una encuesta que
        # `bulk_expire_csat_surveys` ya cerro: `None` hace que el llamador
        # (`respond_csat_via_link`) la trate igual que un link invalido, que es
        # exactamente lo que es a esta altura.
        logger.info("Encuesta CSAT %s ya habia expirado; no se registra", survey_id)
        return None

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
