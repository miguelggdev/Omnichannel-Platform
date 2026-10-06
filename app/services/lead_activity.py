"""Historial de actividad de un lead (Sprint 16, ADR-084).

Una sola funcion para escribir y una para leer, de modo que todo el modulo registre hechos de la
misma manera. **La regla: la metadata lleva ids, slugs y nombres de campo, jamas datos
personales** (ni el valor de un email, ni el motivo de una descalificacion, que es texto libre).
Asi el historial no necesita anonimizarse cuando se anonimiza el lead y no filtra lo que se
borro. `registrar_actividad` lo comprueba: rechaza claves que suenan a dato personal.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead_activity import LeadActivity

#: Claves de metadata que nunca se aceptan: serian datos personales en el historial.
_CLAVES_PROHIBIDAS = frozenset(
    {
        "email",
        "phone",
        "telefono",
        "first_name",
        "last_name",
        "name",
        "nombre",
        "linkedin_url",
        "reason",
        "motivo",
        "disqualified_reason",
        "message",
        "content",
    }
)


def registrar_actividad(
    session: AsyncSession,
    *,
    client_id: UUID,
    lead_id: UUID,
    tipo: str,
    user_id: UUID | None = None,
    **metadata: Any,
) -> LeadActivity:
    """Anade una entrada al historial del lead (la escribe el `flush` de la sesion).

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant del lead.
        lead_id: Lead al que ocurre el hecho.
        tipo: Una de las constantes `ACTIVITY_*` de `app.models.lead_activity`.
        user_id: Quien lo hace; `None` si es el sistema.
        **metadata: Datos del hecho: ids, slugs, nombres de campo.

    Returns:
        La actividad creada.

    Raises:
        ValueError: Si la metadata trae una clave que sugiere un dato personal.
    """
    prohibidas = _CLAVES_PROHIBIDAS & {k.lower() for k in metadata}
    if prohibidas:
        raise ValueError(
            f"La metadata de actividad no admite datos personales: {sorted(prohibidas)}"
        )
    actividad = LeadActivity(
        client_id=client_id,
        lead_id=lead_id,
        user_id=user_id,
        activity_type=tipo,
        metadata_={k: (str(v) if isinstance(v, UUID) else v) for k, v in metadata.items()},
    )
    session.add(actividad)
    return actividad


async def listar_actividades(
    session: AsyncSession, client_id: UUID, lead_id: UUID, *, page: int, page_size: int
) -> tuple[list[LeadActivity], int]:
    """Una pagina del historial, de la mas reciente a la mas antigua.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        lead_id: Lead.
        page: Pagina, desde 1.
        page_size: Tamano de pagina.

    Returns:
        `(actividades, total)`.
    """
    base = (LeadActivity.client_id == client_id, LeadActivity.lead_id == lead_id)
    total = (
        await session.execute(select(func.count()).select_from(LeadActivity).where(*base))
    ).scalar_one()
    filas = (
        await session.execute(
            select(LeadActivity)
            .where(*base)
            .order_by(LeadActivity.created_at.desc(), LeadActivity.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars()
    return list(filas), int(total)
