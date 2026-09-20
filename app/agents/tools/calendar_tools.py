"""Tools de agendamiento que el scheduling agent expone a GPT-4o.

Contrato de seguridad (`specs/sprint-07-scheduling-crm.md` #4, "CRITICO"): el
`client_id` llega por `config["configurable"]`, nunca como argumento que el LLM
pueda rellenar. Cada tool declara un parametro `config: RunnableConfig`; LangChain
lo excluye del schema que ve el modelo (confirmado: `tool.args` no incluye
`config`) y lo inyecta en runtime a partir del `config=` que recibe `.ainvoke()`
en `app/agents/nodes/scheduling.py`.

`contact_id` sigue el mismo criterio y por el mismo motivo: se inyecta desde
`config["configurable"]["contact_id"]` (el contacto real de la conversación,
disponible en `ConversationState`), no es un argumento que el LLM rellene.
Antes de este fix, `create_appointment`/`list_appointments` pedían `contact_id`
como argumento normal — pero GPT-4o no tiene ninguna fuente legítima de ese
UUID interno (nunca aparece en el system prompt ni en la conversación), así
que en el mejor caso lo inventaba y `UUID(contact_id)` explotaba con
`ValueError`, y en el peor caso el diseño dejaba abierta la puerta a que el
bot operara sobre el contacto equivocado dentro del mismo tenant. Un contacto
que chatea con el bot solo puede agendar/consultar sus propias citas.

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
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from sqlalchemy import select, text

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


def _contact_id(config: RunnableConfig) -> UUID:
    """Extrae el `contact_id` inyectado por el nodo, nunca provisto por el LLM.

    Args:
        config: Config que LangChain inyecta a partir de `.ainvoke(..., config=...)`.

    Returns:
        UUID del contacto real de la conversación en curso.
    """
    return UUID(config["configurable"]["contact_id"])


def _localizar(momento: datetime, timezone: str) -> datetime:
    """Adjunta el timezone del tenant a un datetime naive del LLM.

    `Appointment.starts_at`/`ends_at` son `TIMESTAMPTZ`: guardar un datetime
    naive ahí lo interpreta como UTC (asyncpg), no como la hora local del
    tenant que el LLM en realidad quiso decir — corrimiento silencioso de
    varias horas entre lo que muestra Google Calendar y lo que queda en la
    fila de `appointments`. Si el LLM ya mandó un offset explícito, se respeta.

    Args:
        momento: Datetime parseado de la entrada del LLM.
        timezone: Zona horaria IANA del tenant.

    Returns:
        El mismo datetime, con tzinfo si no lo traía.
    """
    if momento.tzinfo is not None:
        return momento
    return momento.replace(tzinfo=ZoneInfo(timezone))


async def _find_service_type(client_id: UUID, name: str) -> ServiceType | None:
    """Busca un tipo de servicio activo por nombre exacto.

    Args:
        client_id: Tenant propietario.
        name: Nombre del tipo de servicio, tal como lo dijo el usuario.

    Returns:
        El tipo de servicio, o `None` si no existe o está desactivado.
    """
    async with tenant_session(client_id) as session:
        stmt = select(ServiceType).where(
            ServiceType.client_id == client_id,
            ServiceType.name == name,
            ServiceType.is_active.is_(True),
        )
        return (await session.execute(stmt)).scalar_one_or_none()


async def _list_service_types(client_id: UUID) -> str:
    """Lista los tipos de servicio activos del tenant, para el mensaje de error.

    Args:
        client_id: Tenant propietario.

    Returns:
        Los nombres y duraciones separados por coma, o un aviso si no hay ninguno.
    """
    async with tenant_session(client_id) as session:
        stmt = select(ServiceType).where(
            ServiceType.client_id == client_id, ServiceType.is_active.is_(True)
        )
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
    datetime_iso: str,
    service_type_name: str,
    config: RunnableConfig,
    notes: str | None = None,
) -> str:
    """Crea una cita en el calendario. Confirma con el usuario ANTES de llamarla.

    Args:
        datetime_iso: Fecha y hora en formato ISO 8601 (ej. "2026-09-21T10:00:00").
        service_type_name: Nombre del tipo de servicio.
        notes: Notas adicionales sobre la cita, si las hay.
    """
    client_id = _client_id(config)
    contact_id = _contact_id(config)
    conversation_id = config["configurable"].get("conversation_id")

    service_type = await _find_service_type(client_id, service_type_name)
    if service_type is None:
        disponibles = await _list_service_types(client_id)
        return f"No encontré el tipo de servicio '{service_type_name}'. Disponibles: {disponibles}."

    try:
        start = datetime.fromisoformat(datetime_iso)
    except ValueError:
        return f"'{datetime_iso}' no es una fecha/hora ISO 8601 válida."

    calendar = await GoogleCalendarService.from_tenant(client_id)
    start = _localizar(start, calendar.timezone)
    end = start + timedelta(minutes=service_type.duration_minutes)

    # Todo lo que sigue queda en UNA sola transacción, con un advisory lock
    # transaccional (pg_advisory_xact_lock: se libera solo al COMMIT/ROLLBACK,
    # el único modo compatible con el Transaction Pooler de Supavisor) por
    # tenant (BUG-023, ver MEMORY.md). check_availability() y esta tool son
    # dos tool-calls separadas -- pueden pasar turnos enteros de conversación
    # entre una y otra, o dos conversaciones del mismo tenant pedir el mismo
    # horario a la vez. Sin el lock, dos llamadas concurrentes podrían pasar
    # el recheck de abajo a la vez y crear dos eventos superpuestos. El lock
    # se sostiene durante el recheck, la llamada a Calendar y el INSERT, no
    # solo la escritura en la base.
    async with tenant_session(client_id) as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:clave))").bindparams(
                clave=f"appointments:{client_id}"
            )
        )

        if await calendar.has_conflict(start, end):
            return "Ese horario ya no está disponible. ¿Quieres que revise otros horarios?"

        stmt = select(Contact).where(Contact.id == contact_id, Contact.client_id == client_id)
        contact = (await session.execute(stmt)).scalar_one_or_none()
        if contact is None:
            return "No encontré ese contacto."
        contact_name = (
            contact.display_name
            or f"{contact.first_name or ''} {contact.last_name or ''}".strip()
            or "Contacto"
        )

        event = await calendar.create_event(
            summary=f"{service_type_name} - {contact_name}",
            start=start,
            end=end,
            description=notes,
        )

        session.add(
            Appointment(
                client_id=client_id,
                contact_id=contact_id,
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
        stmt = select(Appointment).where(
            Appointment.id == UUID(appointment_id), Appointment.client_id == client_id
        )
        appointment = (await session.execute(stmt)).scalar_one_or_none()
        if appointment is None:
            return "No encontré esa cita."
        if appointment.status == "cancelled":
            return "Esa cita ya fue cancelada. Puedo crear una nueva si lo deseas."

        service_type = (
            await session.execute(
                select(ServiceType).where(
                    ServiceType.id == appointment.service_type_id,
                    ServiceType.client_id == client_id,
                )
            )
        ).scalar_one_or_none()

        calendar = await GoogleCalendarService.from_tenant(client_id)
        new_start = _localizar(new_start, calendar.timezone)
        new_end = new_start + timedelta(
            minutes=service_type.duration_minutes if service_type else 60
        )

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
        stmt = select(Appointment).where(
            Appointment.id == UUID(appointment_id), Appointment.client_id == client_id
        )
        appointment = (await session.execute(stmt)).scalar_one_or_none()
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
async def list_appointments(date_from: str, date_to: str, config: RunnableConfig) -> str:
    """Lista las citas del contacto en un rango de fechas.

    Args:
        date_from: Fecha de inicio en formato YYYY-MM-DD.
        date_to: Fecha de fin en formato YYYY-MM-DD.
    """
    client_id = _client_id(config)
    contact_id = _contact_id(config)

    try:
        start = datetime.strptime(date_from, "%Y-%m-%d")
        end = datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    except ValueError:
        return "Las fechas deben tener formato YYYY-MM-DD."

    # Nota: el rango se compara tal cual contra `starts_at` (TIMESTAMPTZ), sin
    # localizar al timezone del tenant como sí hacen create/modify_appointment
    # -- listar no necesita tocar GoogleCalendarService.from_tenant() (que
    # exige service account + calendar_id configurados) solo para leer una
    # zona horaria. El desvío es de a lo sumo un dia en el borde del rango,
    # aceptable para una lista informativa.
    async with tenant_session(client_id) as session:
        stmt = (
            select(Appointment)
            .where(
                Appointment.client_id == client_id,
                Appointment.contact_id == contact_id,
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
