"""Persistencia de las llamadas del canal de voz (Sprint 13, Dev A).

Cola: `webhooks` (el nombre empieza por `app.tasks.webhook_`). Tres momentos
de una llamada escriben su fila de `call_records`, sin orden garantizado entre
ellos (son tareas de Celery independientes):

1. El webhook de llamada entrante (o saliente contestada): direccion, numeros,
   inicio.
2. El cierre del stream de audio: la transcripcion y el fin.
3. El status callback de Twilio: estado final y duracion facturada.

Por eso es un **upsert** sobre `(client_id, call_sid)` en el que cada momento
solo pisa las columnas que conoce (`COALESCE` con lo que ya habia) y en el que
un estado final no se deshace: si el `completed` del status callback llega
antes que el `in-progress` del webhook inicial, gana el `completed`.

La API no toca la base (el webhook tiene que contestar TwiML en milisegundos y
el WebSocket no debe bloquear el audio): encola esta tarea y sigue.

El contacto y la conversacion se completan aca, desde los mensajes que dejo la
llamada (`external_message_id` = `CallSid:indice`): es la resolucion que ya hizo
`webhook_processor` para cada frase, con fusiones de contactos incluidas. Una
llamada en la que el cliente no llego a decir nada queda sin contacto.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from celery import shared_task
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert

from app.core.database import run_isolated, tenant_session
from app.models.call_record import CALL_FINAL_STATUSES, CALL_STATUSES, CallRecord
from app.models.conversation import Conversation
from app.models.message import Message

logger = logging.getLogger(__name__)

#: Columnas que un evento puede aportar; el resto las fija la base o la tarea.
_CAMPOS = (
    "direction",
    "status",
    "phone_from",
    "phone_to",
    "started_at",
    "ended_at",
    "duration_seconds",
    "transcript",
    "recording_url",
    "recording_duration",
)


def normalizar_direccion(direccion: str | None) -> str:
    """Traduce la direccion de Twilio a la de `call_records`.

    Args:
        direccion: `inbound`, `outbound-api` u `outbound-dial`.

    Returns:
        `inbound` u `outbound`.
    """
    return "outbound" if (direccion or "").startswith("outbound") else "inbound"


def direccion_reconocida(direccion: str | None) -> str | None:
    """Traduce la direccion solo cuando Twilio la mando y se entiende.

    `normalizar_direccion()` nunca devuelve `None`: convierte lo ausente y lo
    desconocido en `inbound`. Eso sirve para el webhook de TwiML, que siempre
    trae `Direction` y es la fuente autoritativa, pero no para los status
    callbacks: ahi el valor puede faltar, o ser uno que no empieza por
    `outbound` sin ser entrante (`trunking-terminating` en troncales SIP). Como
    el upsert solo escribe los campos que no son `None`, devolver `None` deja
    intacta la direccion ya guardada en vez de voltear un `outbound` a
    `inbound`.

    Args:
        direccion: Valor de `Direction`, si vino.

    Returns:
        `inbound`, `outbound`, o `None` si no vino o no se reconoce.
    """
    if not direccion:
        return None
    if direccion.startswith("outbound"):
        return "outbound"
    return "inbound" if direccion == "inbound" else None


def _instante(valor: str | datetime | None) -> datetime | None:
    """Convierte un instante ISO-8601 (como viaja por Celery) a `datetime`.

    Args:
        valor: Texto ISO, `datetime` o `None`.

    Returns:
        El instante con zona horaria, o `None`.
    """
    if valor is None or isinstance(valor, datetime):
        return valor
    instante = datetime.fromisoformat(valor)
    return instante if instante.tzinfo else instante.replace(tzinfo=timezone.utc)


async def _conversacion_de_la_llamada(
    session: Any, client_id: UUID, call_sid: str
) -> tuple[UUID, UUID] | None:
    """Conversacion y contacto en los que quedaron las frases de la llamada.

    Args:
        session: Sesion con el contexto del tenant.
        client_id: Tenant.
        call_sid: Llamada.

    Returns:
        `(conversation_id, contact_id)`, o `None` si la llamada no dejo mensajes.
    """
    fila = (
        await session.execute(
            select(Message.conversation_id, Conversation.contact_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.client_id == client_id,
                Message.external_message_id.like(f"{call_sid}:%"),
            )
            .order_by(Message.created_at)
            .limit(1)
        )
    ).first()
    return (fila[0], fila[1]) if fila else None


async def guardar_llamada(client_id: UUID, call_sid: str, datos: dict[str, Any]) -> None:
    """Crea o completa la fila de una llamada.

    Args:
        client_id: Tenant.
        call_sid: `CallSid` de Twilio.
        datos: Columnas que aporta este evento (ver `_CAMPOS`). Las ausentes o
            en `None` no se tocan.

    Raises:
        ValueError: Si `status` no es un estado de Twilio conocido.
    """
    valores = {campo: datos.get(campo) for campo in _CAMPOS}
    valores["started_at"] = _instante(valores["started_at"])
    valores["ended_at"] = _instante(valores["ended_at"])
    if valores["status"] is not None and valores["status"] not in CALL_STATUSES:
        raise ValueError(f"Estado de llamada desconocido: {valores['status']!r}")

    async with tenant_session(client_id) as session:
        vinculo = await _conversacion_de_la_llamada(session, client_id, call_sid)
        conversation_id, contact_id = vinculo if vinculo else (None, None)

        insercion = insert(CallRecord).values(
            client_id=client_id,
            call_sid=call_sid,
            conversation_id=conversation_id,
            contact_id=contact_id,
            direction=valores["direction"] or "inbound",
            status=valores["status"] or "in-progress",
            phone_from=valores["phone_from"],
            phone_to=valores["phone_to"],
            started_at=valores["started_at"] or datetime.now(timezone.utc),
            ended_at=valores["ended_at"],
            duration_seconds=valores["duration_seconds"] or 0,
            transcript=valores["transcript"] or [],
            recording_url=valores["recording_url"],
            recording_duration=valores["recording_duration"],
        )
        actual = CallRecord.__table__.c
        nuevo = insercion.excluded

        def _si_llego(campo: str) -> Any:
            """Toma el valor nuevo solo si este evento lo trajo."""
            return nuevo[campo] if valores[campo] is not None else actual[campo]

        estado_final = actual.status.in_(sorted(CALL_FINAL_STATUSES))
        actualizacion = {
            "conversation_id": func.coalesce(actual.conversation_id, nuevo.conversation_id),
            "contact_id": func.coalesce(actual.contact_id, nuevo.contact_id),
            "direction": _si_llego("direction"),
            # Un estado final no se deshace con uno intermedio que llegue tarde.
            "status": (
                case((estado_final, actual.status), else_=nuevo.status)
                if valores["status"] is not None
                else actual.status
            ),
            "phone_from": _si_llego("phone_from"),
            "phone_to": _si_llego("phone_to"),
            # El inicio es el primero que se conocio, no el ultimo evento.
            "started_at": func.least(actual.started_at, nuevo.started_at),
            "ended_at": _si_llego("ended_at"),
            "duration_seconds": _si_llego("duration_seconds"),
            "transcript": _si_llego("transcript"),
            "recording_url": _si_llego("recording_url"),
            "recording_duration": _si_llego("recording_duration"),
            "updated_at": func.now(),
        }
        await session.execute(
            insercion.on_conflict_do_update(
                index_elements=[actual.client_id, actual.call_sid],
                set_=actualizacion,
            )
        )


@shared_task(
    name="app.tasks.webhook_save_call_record",
    bind=True,
    max_retries=3,
    acks_late=True,
    time_limit=60,
    soft_time_limit=45,
)
def save_call_record(self: Any, client_id: str, call_sid: str, datos: dict[str, Any]) -> str:
    """Tarea de Celery que persiste un evento de una llamada.

    Args:
        self: Instancia de la tarea (bind=True), para los reintentos.
        client_id: Tenant.
        call_sid: `CallSid` de Twilio.
        datos: Columnas que aporta el evento.

    Returns:
        `"saved"`.

    Raises:
        Retry: Ante un fallo de base de datos, con backoff.
    """
    try:
        run_isolated(guardar_llamada(UUID(client_id), call_sid, datos))
    except ValueError:
        logger.exception("Evento de llamada %s descartado: datos invalidos", call_sid)
        return "invalid"
    except Exception as exc:
        logger.warning("No se pudo guardar la llamada %s; se reintenta", call_sid, exc_info=True)
        raise self.retry(exc=exc, countdown=5 * (self.request.retries + 1)) from exc
    return "saved"
