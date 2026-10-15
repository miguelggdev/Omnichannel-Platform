"""Que pasa con las secuencias de un lead cuando contesta (Sprint 18, Dev B).

`procesar_respuesta()` se llama desde el mensaje entrante (`registrar_mensaje_entrante`):

1. Clasifica el texto (`lead_reply_classifier`, sin IA: es el camino del webhook).
2. Una **ausencia** (autorespuesta) no cuenta: no se hace nada.
3. Deja `sequence_reply` en el historial del lead con el tipo, **sin el texto**.
4. Saca al lead de sus secuencias vivas con el motivo que corresponde: `unsubscribe` ->
   `unsubscribed`, `negative` -> `negative_reply` (ambos bloquean nuevas inscripciones), el resto ->
   `replied`.

Auto-acciones: salir de la secuencia al contestar es la accion para todas las respuestas; que una
respuesta positiva agende una llamada llega con la agenda del Sprint 19.

**La baja persiste aunque no haya secuencias vivas.** Quien escribe "BAJA" despues de terminar una
secuencia tambien debe quedar bloqueado: `sequence_reply` con `unsubscribe` o `negative` cuenta en
`se_dio_de_baja()`.
"""

import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead_activity import ACTIVITY_SEQUENCE_REPLY, LeadActivity
from app.models.lead_sequence import (
    EXIT_NEGATIVE_REPLY,
    EXIT_REPLIED,
    EXIT_UNSUBSCRIBED,
    LIVE_STATUSES,
    LeadSequenceEnrollment,
)
from app.services.lead_activity import registrar_actividad
from app.services.lead_reply_classifier import clasificar_respuesta
from app.services.lead_sequences import salir_de_secuencias

logger = logging.getLogger(__name__)

#: Tipo de respuesta -> motivo de salida. Los que no estan aqui salen como `replied`.
_MOTIVO_POR_TIPO = {"unsubscribe": EXIT_UNSUBSCRIBED, "negative": EXIT_NEGATIVE_REPLY}
#: Tipos que equivalen a pedir no recibir mas mensajes (los lee `se_dio_de_baja`).
TIPOS_DE_BAJA: tuple[str, ...] = ("unsubscribe", "negative")


async def procesar_respuesta(
    session: AsyncSession,
    *,
    client_id: UUID,
    lead_id: UUID,
    texto: str | None,
    canal: str,
    ahora: datetime,
) -> str | None:
    """Clasifica la respuesta de un lead y actualiza sus secuencias.

    Corre dentro de la transaccion del mensaje; quien llama la aisla en un SAVEPOINT.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        lead_id: Lead que escribio.
        texto: Contenido del mensaje.
        canal: Canal por el que escribio.
        ahora: Instante del mensaje.

    Returns:
        El tipo de respuesta si se proceso; `None` si era una autorespuesta o no habia nada que
        hacer (sin secuencias vivas y sin pedir la baja).
    """
    tipo = clasificar_respuesta(texto)
    if tipo == "out_of_office":
        return None
    es_baja = tipo in TIPOS_DE_BAJA
    hay_vivas = bool(
        (
            await session.execute(
                select(
                    exists().where(
                        LeadSequenceEnrollment.client_id == client_id,
                        LeadSequenceEnrollment.lead_id == lead_id,
                        LeadSequenceEnrollment.status.in_(LIVE_STATUSES),
                    )
                )
            )
        ).scalar_one()
    )
    if not hay_vivas and not es_baja:
        return None
    registrar_actividad(
        session,
        client_id=client_id,
        lead_id=lead_id,
        tipo=ACTIVITY_SEQUENCE_REPLY,
        reply_kind=tipo,
        channel=canal,
    )
    if hay_vivas:
        await salir_de_secuencias(
            session,
            client_id,
            [lead_id],
            _MOTIVO_POR_TIPO.get(tipo, EXIT_REPLIED),
            ahora=ahora,
        )
    return tipo


async def pidio_la_baja(session: AsyncSession, client_id: UUID, lead_id: UUID) -> bool:
    """Si el lead escribio alguna vez pidiendo no recibir mas mensajes (aunque no tuviera secuencias).

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        lead_id: Lead.

    Returns:
        `True` si hay un `sequence_reply` de tipo baja o negativa.
    """
    return bool(
        (
            await session.execute(
                select(
                    exists().where(
                        LeadActivity.client_id == client_id,
                        LeadActivity.lead_id == lead_id,
                        LeadActivity.activity_type == ACTIVITY_SEQUENCE_REPLY,
                        LeadActivity.metadata_["reply_kind"].astext.in_(TIPOS_DE_BAJA),
                    )
                )
            )
        ).scalar_one()
    )
