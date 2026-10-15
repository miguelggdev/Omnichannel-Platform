"""Disparadores de las secuencias: lo que un hecho del lead hace con sus inscripciones (Sprint 18).

Dos acciones, ambas **aisladas en un SAVEPOINT y sin propagar errores**: una secuencia mal
configurada o una carrera no puede costar la operacion principal (crear el lead, moverlo de etapa,
guardar un score).

- `disparar_inscripciones()`: tras crear un lead, cambiarle la etapa o el score, lo inscribe en las
  secuencias encendidas cuyo disparador cumple (`inscribir_por_evento`).
- `sacar_de_secuencias()`: al borrar o cerrar un lead, cierra sus inscripciones vivas. El motor ya
  lo haria en el siguiente turno (`motivo_de_bloqueo`); hacerlo aqui evita que un lead cerrado
  siga apareciendo como pendiente en el Kanban de follow-up.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead import Lead
from app.schemas.lead_sequence import TriggerEvent
from app.services.lead_sequences import inscribir_por_evento, salir_de_secuencias

logger = logging.getLogger(__name__)


async def disparar_inscripciones(session: AsyncSession, lead: Lead, evento: TriggerEvent) -> int:
    """Inscribe al lead en las secuencias que se disparan con `evento`.

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead (ya con el cambio aplicado y volcado a la sesion).
        evento: Lo que acaba de pasar.

    Returns:
        Inscripciones nuevas; 0 si no habia ninguna o si algo fallo (se registra).
    """
    try:
        async with session.begin_nested():
            nuevas = await inscribir_por_evento(
                session, lead, evento, ahora=datetime.now(timezone.utc)
            )
            return len(nuevas)
    except Exception:
        logger.exception(
            "No se pudo disparar secuencias (evento=%s, lead=%s, client_id=%s)",
            evento,
            lead.id,
            lead.client_id,
        )
        return 0


async def sacar_de_secuencias(session: AsyncSession, lead: Lead, motivo: str) -> int:
    """Cierra las inscripciones vivas del lead con `motivo` (un `EXIT_*`).

    No espera a las que un worker tiene bloqueadas: ese worker las cierra en su turno por la
    guarda del motor (lead borrado o cerrado).

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead.
        motivo: Codigo de salida.

    Returns:
        Inscripciones cerradas; 0 si no habia o si algo fallo (se registra).
    """
    try:
        async with session.begin_nested():
            return await salir_de_secuencias(
                session,
                lead.client_id,
                [lead.id],
                motivo,
                ahora=datetime.now(timezone.utc),
                saltar_bloqueadas=True,
            )
    except Exception:
        logger.exception(
            "No se pudo sacar al lead %s de sus secuencias (client_id=%s)", lead.id, lead.client_id
        )
        return 0
