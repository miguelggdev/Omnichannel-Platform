"""Google Calendar para las llamadas agendadas (Sprint 19, Dev B).

Usa `GoogleCalendarService` (Sprint 7): un calendario por tenant (`calendar_id` en
`agent_configs.config.scheduling` o `GOOGLE_CALENDAR_ID`) y un service account de la plataforma.

Dos reglas:

- **El calendario es opcional y nunca bloquea.** Sin calendario configurado, o si Google no
  responde (10 s de tope), el agendamiento sigue con lo que hay en la base. Cada funcion devuelve
  si consulto de verdad (`consultado`) para que la respuesta lo diga: un hueco "libre" sin haber
  mirado el calendario no es lo mismo que uno libre habiendolo mirado.
- **Nuestras propias llamadas no se cuentan dos veces.** El calendario es del tenant, no de cada
  persona; las llamadas agendadas ya estan en `scheduled_calls` (y por persona). Al calcular lo
  ocupado se ignoran los eventos que nosotros creamos (por su id), o dos agentes no podrian llamar
  a la misma hora.

Limitacion conocida: como el calendario es uno por tenant, un evento ajeno (una reunion) bloquea el
horario para **todas** las personas. Un calendario por persona exige OAuth por usuario (ADR-092).

Nada de esto guarda ni envia datos personales a Google mas alla del titulo del evento ("Llamada
con <nombre de pila> (<empresa>)"): ni telefono ni email.
"""

import asyncio
import logging
from collections.abc import Awaitable, Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar
from uuid import UUID

from app.services.calendar import (
    CalendarCredentialsError,
    GoogleCalendarService,
    SchedulingNotConfiguredError,
)
from app.services.call_scheduling import Intervalo

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Cuanto se espera a Google antes de seguir sin el calendario.
TIMEOUT_SEGUNDOS = 10.0
#: Eventos que se leen para calcular lo ocupado de un rango.
MAX_EVENTOS = 250


@dataclass(frozen=True)
class OcupadosDelCalendario:
    """Lo ocupado segun el calendario.

    Attributes:
        intervalos: Tramos ocupados (vacio si no se consulto).
        consultado: Si se miro el calendario de verdad.
    """

    intervalos: list[Intervalo]
    consultado: bool


async def _servicio(client_id: UUID) -> GoogleCalendarService | None:
    """El servicio del tenant, o `None` si no tiene calendario configurado."""
    try:
        return await GoogleCalendarService.from_tenant(client_id)
    except (SchedulingNotConfiguredError, CalendarCredentialsError):
        return None


async def _con_tope(coro: Awaitable[T]) -> T:
    """Espera a Google como mucho `TIMEOUT_SEGUNDOS`."""
    return await asyncio.wait_for(coro, timeout=TIMEOUT_SEGUNDOS)


def _titulo(nombre: str | None, empresa: str | None) -> str:
    """`Llamada con Ana (Globex)`: sin telefono ni email."""
    quien = (nombre or "").strip() or "un lead"
    return (
        f"Llamada con {quien} ({empresa.strip()})"
        if empresa and empresa.strip()
        else (f"Llamada con {quien}")
    )


async def ocupados_del_calendario(
    client_id: UUID,
    desde: datetime,
    hasta: datetime,
    *,
    ignorar_ids: Collection[str] = (),
) -> OcupadosDelCalendario:
    """Los tramos ocupados del calendario del tenant en un rango.

    Args:
        client_id: Tenant.
        desde: Inicio (aware).
        hasta: Fin (aware).
        ignorar_ids: Ids de eventos que creamos nosotros (ya estan en `scheduled_calls`).

    Returns:
        Los tramos y si se consulto. Un fallo de Google o un calendario sin configurar devuelve
        `consultado=False`: nunca lanza.
    """
    try:
        servicio = await _servicio(client_id)
        if servicio is None:
            return OcupadosDelCalendario([], False)
        eventos = await _con_tope(servicio.list_events(desde, hasta, max_results=MAX_EVENTOS))
    except Exception as exc:
        logger.warning(
            "No se pudo leer el calendario (client_id=%s): %s", client_id, type(exc).__name__
        )
        return OcupadosDelCalendario([], False)
    ignorados = set(ignorar_ids)
    return OcupadosDelCalendario(
        [Intervalo(e.start, e.end) for e in eventos if e.event_id not in ignorados],
        True,
    )


async def crear_evento(
    client_id: UUID,
    *,
    nombre: str | None,
    empresa: str | None,
    inicio: datetime,
    fin: datetime,
) -> str | None:
    """Crea el evento de una llamada en el calendario del tenant.

    Args:
        client_id: Tenant.
        nombre: Nombre de pila del lead.
        empresa: Empresa del lead.
        inicio: Inicio (aware).
        fin: Fin (aware).

    Returns:
        El id del evento, o `None` si no hay calendario o Google fallo (nunca lanza).
    """
    try:
        servicio = await _servicio(client_id)
        if servicio is None:
            return None
        evento = await _con_tope(
            servicio.create_event(
                summary=_titulo(nombre, empresa),
                start=inicio,
                end=fin,
                description="Llamada agendada desde el CRM.",
            )
        )
    except Exception as exc:
        logger.warning(
            "No se pudo crear el evento de la llamada (client_id=%s): %s",
            client_id,
            type(exc).__name__,
        )
        return None
    return str(evento.event_id)


async def mover_evento(client_id: UUID, event_id: str, *, inicio: datetime, fin: datetime) -> bool:
    """Cambia la hora de un evento.

    Args:
        client_id: Tenant.
        event_id: Evento a mover.
        inicio: Nuevo inicio (aware).
        fin: Nuevo fin (aware).

    Returns:
        `True` si se movio; `False` si no hay calendario, el evento ya no existe o Google fallo.
    """
    try:
        servicio = await _servicio(client_id)
        if servicio is None:
            return False
        await _con_tope(servicio.modify_event(event_id, new_start=inicio, new_end=fin))
    except Exception as exc:
        logger.warning(
            "No se pudo mover el evento (client_id=%s): %s", client_id, type(exc).__name__
        )
        return False
    return True


async def cancelar_evento(client_id: UUID, event_id: str) -> bool:
    """Borra el evento de una llamada cancelada o reprogramada.

    Args:
        client_id: Tenant.
        event_id: Evento.

    Returns:
        `True` si se borro; `False` en cualquier otro caso (nunca lanza).
    """
    try:
        servicio = await _servicio(client_id)
        if servicio is None:
            return False
        borrado: Any = await _con_tope(servicio.cancel_event(event_id))
    except Exception as exc:
        logger.warning(
            "No se pudo borrar el evento (client_id=%s): %s", client_id, type(exc).__name__
        )
        return False
    return bool(borrado)
