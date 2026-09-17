"""Tools de agendamiento que el scheduling agent expone a GPT-4o.

Contrato de seguridad (`specs/sprint-07-scheduling-crm.md` #4, "CRITICO"): el
`client_id` llega por `config["configurable"]`, nunca como argumento que el LLM
pueda rellenar. Cada tool declara un parametro `config: RunnableConfig`; LangChain
lo excluye del schema que ve el modelo (confirmado: `tool.args` no incluye
`config`) y lo inyecta en runtime a partir del `config=` que recibe `.ainvoke()`
en `app/agents/nodes/scheduling.py`.

Cada tool usa `@tool(parse_docstring=True)`: el schema de argumentos que ve el
LLM (nombre, tipo, descripcion) sale del docstring Google-style de la funcion,
que ya es obligatorio por CLAUDE.md — no hace falta declarar un `BaseModel` de
entrada aparte y arriesgarse a que se desincronice del docstring (el spec los
declara pero nunca los conecta a ninguna tool).
"""

import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.contact import Contact
from app.models.service_type import Appointment, ServiceType
from app.services.calendar import GoogleCalendarService

logger = logging.getLogger(__name__)


def _client_id(config: RunnableConfig) -> UUID:
    """Extrae el `client_id` inyectado por el nodo, nunca provisto por el LLM.

    Args:
        config: Config que LangChain inyecta a partir de `.ainvoke(..., config=...)`.

    Returns:
        UUID del tenant dueño de la conversación.
    """
    return UUID(config["configurable"]["client_id"])


async def _find_service_type(client_id: UUID, name: str) -> ServiceType | None:
    """Busca un tipo de servicio activo por nombre exacto.

    Args:
        client_id: Tenant propietario.
        name: Nombre del tipo de servicio, tal como lo dijo el usuario.

    Returns:
        El tipo de servicio, o `None` si no existe o está desactivado.
    """
    async with tenant_session(client_id) as session:
        stmt = select(ServiceType).where(ServiceType.name == name, ServiceType.is_active.is_(True))
        return (await session.execute(stmt)).scalar_one_or_none()


async def _list_service_types(client_id: UUID) -> str:
    """Lista los tipos de servicio activos del tenant, para el mensaje de error.

    Args:
        client_id: Tenant propietario.

    Returns:
        Los nombres y duraciones separados por coma, o un aviso si no hay ninguno.
    """
    async with tenant_session(client_id) as session:
        stmt = select(ServiceType).where(ServiceType.is_active.is_(True))
        tipos = (await session.execute(stmt)).scalars().all()

    if not tipos:
        return "no hay tipos de servicio configurados"
    return ", ".join(f"'{tipo.name}' ({tipo.duration_minutes} min)" for tipo in tipos)


@tool(parse_docstring=True)
async def check_availability(date: str, service_type_name: str, config: RunnableConfig) -> str:
    """Verifica los horarios disponibles para una fecha y un tipo de servicio.

    Args:
        date: Fecha a consultar, en formato YYYY-MM-DD.
        service_type_name: Nombre del tipo de servicio (ej. "Consulta general").
    """
    client_id = _client_id(config)

    service_type = await _find_service_type(client_id, service_type_name)
    if service_type is None:
        disponibles = await _list_service_types(client_id)
        return f"No encontré el tipo de servicio '{service_type_name}'. Disponibles: {disponibles}."

    try:
        target_date = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return f"'{date}' no es una fecha válida. Usa el formato YYYY-MM-DD."

    calendar = await GoogleCalendarService.from_tenant(client_id)
    slots = await calendar.check_availability(
        date=target_date,
        duration_minutes=service_type.duration_minutes,
        buffer_minutes=service_type.buffer_minutes,
    )

    if not slots:
        return f"No hay horarios disponibles para el {date}. Puedo revisar otro día si quieres."

    slots_texto = "\n".join(f"  - {slot.to_dict()['display']}" for slot in slots)
    return (
        f"Horarios disponibles para {date} ({service_type_name}, "
        f"{service_type.duration_minutes} min):\n{slots_texto}\n\n"
        "Indica cuál horario prefieres."
    )


@tool(parse_docstring=True)
async def create_appointment(
    contact_id: str,
    datetime_iso: str,
    service_type_name: str,
    config: RunnableConfig,
    notes: str | None = None,
) -> str:
    """Crea una cita en el calendario. Confirma con el usuario ANTES de llamarla.

    Args:
        contact_id: UUID del contacto que agenda la cita.
        datetime_iso: Fecha y hora en formato ISO 8601 (ej. "2026-09-21T10:00:00").
        service_type_name: Nombre del tipo de servicio.
        notes: Notas adicionales sobre la cita, si las hay.
    """
    client_id = _client_id(config)
    conversation_id = config["configurable"].get("conversation_id")

    service_type = await _find_service_type(client_id, service_type_name)
    if service_type is None:
        disponibles = await _list_service_types(client_id)
        return f"No encontré el tipo de servicio '{service_type_name}'. Disponibles: {disponibles}."

    try:
        start = datetime.fromisoformat(datetime_iso)
    except ValueError:
        return f"'{datetime_iso}' no es una fecha/hora ISO 8601 válida."
    end = start + timedelta(minutes=service_type.duration_minutes)

    async with tenant_session(client_id) as session:
        contact = await session.get(Contact, UUID(contact_id))
    if contact is None:
        return "No encontré ese contacto."
    contact_name = (
        contact.display_name
        or f"{contact.first_name or ''} {contact.last_name or ''}".strip()
        or "Contacto"
    )

    calendar = await GoogleCalendarService.from_tenant(client_id)
    event = await calendar.create_event(
        summary=f"{service_type_name} - {contact_name}",
        start=start,
        end=end,
        description=notes,
    )

    async with tenant_session(client_id) as session:
        session.add(
            Appointment(
                client_id=client_id,
                contact_id=UUID(contact_id),
                conversation_id=UUID(conversation_id) if conversation_id else None,
                service_type_id=service_type.id,
                google_event_id=event.event_id,
                title=event.summary,
                starts_at=start,
                ends_at=end,
                status="confirmed",
                notes=notes,
            )
        )

    return (
        "Cita creada exitosamente:\n"
        f"  Servicio: {service_type_name}\n"
        f"  Fecha: {start.strftime('%d/%m/%Y')}\n"
        f"  Hora: {start.strftime('%H:%M')} - {end.strftime('%H:%M')}\n"
        f"  Duración: {service_type.duration_minutes} minutos\n"
        "Si necesitas modificarla o cancelarla, avísame."
    )


@tool(parse_docstring=True)
async def modify_appointment(
    appointment_id: str, new_datetime_iso: str, config: RunnableConfig
) -> str:
    """Reprograma una cita existente a una nueva fecha y hora.

    Args:
        appointment_id: UUID de la cita a modificar.
        new_datetime_iso: Nueva fecha y hora en formato ISO 8601.
    """
    client_id = _client_id(config)

    try:
        new_start = datetime.fromisoformat(new_datetime_iso)
    except ValueError:
        return f"'{new_datetime_iso}' no es una fecha/hora ISO 8601 válida."

    async with tenant_session(client_id) as session:
        appointment = await session.get(Appointment, UUID(appointment_id))
        if appointment is None:
            return "No encontré esa cita."
        if appointment.status == "cancelled":
            return "Esa cita ya fue cancelada. Puedo crear una nueva si lo deseas."

        service_type = await session.get(ServiceType, appointment.service_type_id)
        new_end = new_start + timedelta(
            minutes=service_type.duration_minutes if service_type else 60
        )

        calendar = await GoogleCalendarService.from_tenant(client_id)
        if appointment.google_event_id:
            await calendar.modify_event(
                event_id=appointment.google_event_id, new_start=new_start, new_end=new_end
            )

        appointment.starts_at = new_start
        appointment.ends_at = new_end

    return (
        "Cita reprogramada:\n"
        f"  Nueva fecha: {new_start.strftime('%d/%m/%Y')}\n"
        f"  Nueva hora: {new_start.strftime('%H:%M')} - {new_end.strftime('%H:%M')}"
    )


@tool(parse_docstring=True)
async def cancel_appointment(appointment_id: str, reason: str, config: RunnableConfig) -> str:
    """Cancela una cita existente.

    Args:
        appointment_id: UUID de la cita a cancelar.
        reason: Motivo de la cancelación.
    """
    client_id = _client_id(config)

    async with tenant_session(client_id) as session:
        appointment = await session.get(Appointment, UUID(appointment_id))
        if appointment is None:
            return "No encontré esa cita."
        if appointment.status == "cancelled":
            return "Esa cita ya estaba cancelada."

        calendar = await GoogleCalendarService.from_tenant(client_id)
        if appointment.google_event_id:
            await calendar.cancel_event(appointment.google_event_id)

        appointment.status = "cancelled"
        appointment.cancelled_reason = reason

    return f"Cita cancelada. Motivo registrado: {reason}. Puedo agendar una nueva si lo deseas."


@tool(parse_docstring=True)
async def list_appointments(
    contact_id: str, date_from: str, date_to: str, config: RunnableConfig
) -> str:
    """Lista las citas de un contacto en un rango de fechas.

    Args:
        contact_id: UUID del contacto.
        date_from: Fecha de inicio en formato YYYY-MM-DD.
        date_to: Fecha de fin en formato YYYY-MM-DD.
    """
    client_id = _client_id(config)

    try:
        start = datetime.strptime(date_from, "%Y-%m-%d")
        end = datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    except ValueError:
        return "Las fechas deben tener formato YYYY-MM-DD."

    async with tenant_session(client_id) as session:
        stmt = (
            select(Appointment)
            .where(
                Appointment.contact_id == UUID(contact_id),
                Appointment.starts_at >= start,
                Appointment.starts_at <= end,
                Appointment.status != "cancelled",
            )
            .order_by(Appointment.starts_at)
        )
        citas = (await session.execute(stmt)).scalars().all()

    if not citas:
        return f"No hay citas programadas entre {date_from} y {date_to}."

    lineas = [
        f"  - {cita.starts_at.strftime('%d/%m/%Y %H:%M')} | {cita.title} | "
        f"Estado: {cita.status} | ID: {cita.id}"
        for cita in citas
    ]
    return "Citas encontradas:\n" + "\n".join(lineas)


SCHEDULING_TOOLS: list[Any] = [
    check_availability,
    create_appointment,
    modify_appointment,
    cancel_appointment,
    list_appointments,
]
