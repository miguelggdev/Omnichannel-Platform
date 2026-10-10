"""Supresion RGPD de leads: un solo camino para todo lo que hay que borrar o cortar.

Lo usan los dos endpoints de supresion (`gdpr-delete` de un contacto y de un lead). Cada tabla
nueva con datos de un lead (scores en el Sprint 17, secuencias en el 18, llamadas en el 19) se
engancha **aqui**, no en cada endpoint: si se olvidara en uno, por ese camino se seguiria
perfilando o escribiendo a quien pidio la supresion.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead import Lead
from app.models.lead_activity import ACTIVITY_ANONYMIZED
from app.models.lead_sequence import EXIT_GDPR
from app.services.deals import anonimizar_deals
from app.services.lead_activity import registrar_actividad
from app.services.lead_privacy import anonimizar_lead
from app.services.lead_score_service import borrar_historial_scores
from app.services.lead_sequences import salir_de_secuencias
from app.services.scheduled_calls import suprimir_llamadas


async def suprimir_leads(
    session: AsyncSession,
    client_id: UUID,
    leads: Sequence[Lead],
    *,
    user_id: UUID,
    via: str,
    ahora: datetime,
) -> None:
    """Anonimiza los leads, borra su historial de scores, los saca de sus secuencias, cancela
    sus llamadas futuras y quita el texto libre de sus deals y llamadas.

    No hace `commit`: todo va en la transaccion del endpoint.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        leads: Leads del titular (ya cargados).
        user_id: Quien pidio la supresion.
        via: Por donde llego (`contact` o `lead`), para el historial.
        ahora: Instante de la supresion.
    """
    if not leads:
        return
    for lead in leads:
        anonimizar_lead(lead)
        registrar_actividad(
            session,
            client_id=client_id,
            lead_id=lead.id,
            tipo=ACTIVITY_ANONYMIZED,
            user_id=user_id,
            via=via,
        )
    ids = [lead.id for lead in leads]
    await borrar_historial_scores(session, client_id, ids)
    # Nunca se le escribe a quien pidio la supresion (Sprint 18). Sin esperar a las filas que un
    # worker tiene bloqueadas: la regla de bloqueo del motor las saca en su siguiente turno.
    await salir_de_secuencias(
        session,
        client_id,
        ids,
        EXIT_GDPR,
        ahora=ahora,
        user_id=user_id,
        saltar_bloqueadas=True,
    )
    # Sprint 19: a quien pidio la supresion no se le llama, y el texto libre de sus deals y
    # llamadas (titulo, notas, motivo de perdida) puede identificarle.
    await suprimir_llamadas(session, client_id, ids, ahora=ahora)
    await anonimizar_deals(session, client_id, ids)
