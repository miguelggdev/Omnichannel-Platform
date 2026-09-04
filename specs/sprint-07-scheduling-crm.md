# Sprint 7 — Agente de Agendamiento & CRM API

## Objetivo
Integracion con Google Calendar para agendamiento automatizado via IA conversacional, API completa del CRM (contactos, tags, notas, conversaciones con ciclo de vida), unificacion de contactos cross-canal y auto-cierre de conversaciones inactivas. Al finalizar este sprint, un mensaje como "quiero una cita" es procesado por el scheduling agent que gestiona el calendario del tenant, y los endpoints del CRM permiten gestion completa de contactos y conversaciones.

## Prerequisitos
- Sprint 6 completado: grafo de agentes funcional con intent routing, RAG, token budget, handoff y checkpointing
- Credenciales OAuth2 de Google Calendar API (service account o consent screen configurado)
- Tabla `service_types` como migracion adicional (no estaba en el schema original de Sprint 1)
- Intent routing (Sprint 6) capaz de detectar intent "scheduling"
- MessagingProvider (Sprint 4) funcional para envio de respuestas
- LangGraph checkpointing funcional para conversaciones multi-turno (necesario para dialogo de agendamiento)
- Celery Beat configurado (Sprint 2) para tareas periodicas de auto-cierre
- `tenant_session()` y RBAC middleware (Sprint 3) disponibles

## Archivos a Crear
```
app/
  agents/
    nodes/
      scheduling.py                  # Nodo de agendamiento LangGraph
    tools/
      __init__.py
      calendar_tools.py              # 5 tools para Google Calendar
  api/
    v1/
      contacts.py                    # CRUD contactos con paginacion y filtros
      conversations.py               # CRUD conversaciones con ciclo de vida
      tags.py                        # CRUD tags por tenant
      notes.py                       # CRUD notas internas
  services/
    calendar.py                      # GoogleCalendarService (abstraccion)
    contact_unifier.py               # Unificacion cross-canal de contactos
    conversation_lifecycle.py        # Maquina de estados de conversaciones
  models/
    service_type.py                  # Modelo SQLAlchemy para service_types
  schemas/
    contact.py                       # Schemas Pydantic adicionales (merge, search)
    conversation.py                  # Schemas adicionales (assign, status change)
    tag.py                           # Schemas de tags
    note.py                          # Schemas de notas
    scheduling.py                    # Schemas de agendamiento
  tasks/
    conversation_lifecycle.py        # Tasks Celery Beat para auto-cierre
supabase/
  migrations/
    002_service_types.sql            # Migracion para tabla service_types
tests/
  unit/
    test_scheduling_agent.py
    test_contact_unifier.py
    test_conversation_lifecycle.py
  integration/
    test_crm_api.py
    test_scheduling_flow.py
```

## Tareas Detalladas

### 1. Migracion: Tabla `service_types` (`supabase/migrations/002_service_types.sql`)

Esta tabla no estaba en el schema original del Sprint 1. Se agrega como migracion separada.

```sql
-- 002_service_types.sql
-- Tabla adicional para tipos de servicio con duracion configurable por tenant

CREATE TABLE IF NOT EXISTS service_types (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    duration_minutes INT NOT NULL DEFAULT 60,
    buffer_minutes INT NOT NULL DEFAULT 15,  -- Tiempo entre citas
    is_active BOOLEAN NOT NULL DEFAULT true,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indice para busqueda por tenant
CREATE INDEX IF NOT EXISTS idx_service_types_client_id ON service_types(client_id);

-- Unicidad de nombre por tenant
ALTER TABLE service_types ADD CONSTRAINT uq_service_types_name
    UNIQUE (client_id, name);

-- RLS
ALTER TABLE service_types ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_types FORCE ROW LEVEL SECURITY;

CREATE POLICY service_types_isolation ON service_types
    USING (client_id = current_setting('app.current_client_id')::uuid);

-- Trigger updated_at
CREATE TRIGGER trg_service_types_updated_at
    BEFORE UPDATE ON service_types
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Tabla para citas (appointments)
CREATE TABLE IF NOT EXISTS appointments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
    service_type_id UUID NOT NULL REFERENCES service_types(id) ON DELETE RESTRICT,
    google_event_id VARCHAR(255),
    title VARCHAR(500) NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'confirmed',  -- confirmed, cancelled, completed, no_show
    notes TEXT,
    cancelled_reason TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_appointments_client_id ON appointments(client_id);
CREATE INDEX IF NOT EXISTS idx_appointments_contact_id ON appointments(contact_id);
CREATE INDEX IF NOT EXISTS idx_appointments_starts_at ON appointments(client_id, starts_at);
CREATE INDEX IF NOT EXISTS idx_appointments_google_event_id ON appointments(client_id, google_event_id);

ALTER TABLE appointments ENABLE ROW LEVEL SECURITY;
ALTER TABLE appointments FORCE ROW LEVEL SECURITY;

CREATE POLICY appointments_isolation ON appointments
    USING (client_id = current_setting('app.current_client_id')::uuid);

CREATE TRIGGER trg_appointments_updated_at
    BEFORE UPDATE ON appointments
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
```

**IMPORTANTE**: Ejecutar esta migracion DESPUES de verificar que `update_updated_at()` ya existe (fue creada en Sprint 1).

### 2. Modelo SQLAlchemy (`app/models/service_type.py`)

```python
from datetime import datetime
from uuid import UUID
from sqlalchemy import String, Integer, Boolean, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.base import Base


class ServiceType(Base):
    __tablename__ = "service_types"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, server_default="gen_random_uuid()")
    client_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    buffer_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default="now()")


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, server_default="gen_random_uuid()")
    client_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    contact_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False)
    conversation_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True)
    service_type_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("service_types.id", ondelete="RESTRICT"), nullable=False)
    google_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(nullable=False)
    ends_at: Mapped[datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="confirmed")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default="now()")

    # Relationships
    contact = relationship("Contact", lazy="selectin")
    service_type = relationship("ServiceType", lazy="selectin")
```

### 3. Google Calendar Service (`app/services/calendar.py`)

Abstraccion sobre la Google Calendar API. El refresh token se almacena cifrado con pgcrypto en `agent_configs.settings`.

```python
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import json
import logging

from app.db.session import tenant_session
from app.core.config import settings

logger = logging.getLogger(__name__)


class CalendarSlot:
    """Slot de disponibilidad."""
    def __init__(self, start: datetime, end: datetime):
        self.start = start
        self.end = end

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "display": self.start.strftime("%H:%M") + " - " + self.end.strftime("%H:%M"),
        }


class CalendarEvent:
    """Evento de calendario creado."""
    def __init__(self, event_id: str, summary: str, start: datetime, end: datetime, html_link: str | None = None):
        self.event_id = event_id
        self.summary = summary
        self.start = start
        self.end = end
        self.html_link = html_link

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "summary": self.summary,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "html_link": self.html_link,
        }


class GoogleCalendarService:
    """
    Servicio de Google Calendar por tenant.

    Cada tenant tiene su propia configuracion de Calendar:
    - calendar_id: ID del calendario de Google
    - timezone: Zona horaria (e.g. 'America/Mexico_City')
    - credentials: OAuth2 refresh token cifrado

    Estas configuraciones se almacenan en agent_configs.settings (JSONB)
    para el agente de tipo 'scheduling'.
    """

    def __init__(self, calendar_id: str, timezone: str, credentials: Credentials):
        self.calendar_id = calendar_id
        self.timezone = timezone
        self.service = build("calendar", "v3", credentials=credentials)

    @classmethod
    async def from_tenant(cls, client_id: UUID) -> "GoogleCalendarService":
        """
        Crea una instancia del servicio a partir de la configuracion del tenant.

        Lee la configuracion del agente 'scheduling' en agent_configs:
        - settings.calendar_id
        - settings.timezone
        - settings.oauth_refresh_token (cifrado con pgcrypto)
        """
        from sqlalchemy import text

        async with tenant_session(client_id) as session:
            result = await session.execute(
                text("""
                    SELECT settings
                    FROM agent_configs
                    WHERE client_id = :client_id
                      AND agent_type = 'scheduling'
                      AND is_enabled = true
                """),
                {"client_id": str(client_id)},
            )
            row = result.fetchone()
            if not row:
                raise ValueError(f"Scheduling agent no habilitado para tenant {client_id}")

            config = row[0]  # JSONB

            # Descifrar refresh token
            # El refresh token esta cifrado con pgp_sym_encrypt
            token_result = await session.execute(
                text("""
                    SELECT pgp_sym_decrypt(
                        decode(:encrypted_token, 'base64'),
                        :encryption_key
                    )
                """),
                {
                    "encrypted_token": config["oauth_refresh_token_encrypted"],
                    "encryption_key": settings.ENCRYPTION_KEY,
                },
            )
            refresh_token = token_result.scalar()

        calendar_id = config["calendar_id"]
        timezone = config.get("timezone", "America/Mexico_City")

        credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token",
        )

        return cls(calendar_id=calendar_id, timezone=timezone, credentials=credentials)

    async def check_availability(
        self,
        date: datetime,
        duration_minutes: int,
        buffer_minutes: int = 15,
        business_hours: tuple[int, int] = (9, 18),  # 9 AM - 6 PM
    ) -> list[CalendarSlot]:
        """
        Consulta disponibilidad en el calendario para una fecha.

        1. Obtiene eventos del dia via freebusy API
        2. Calcula slots libres dentro del horario comercial
        3. Descarta slots menores a duration_minutes + buffer_minutes
        """
        import pytz

        tz = pytz.timezone(self.timezone)
        day_start = tz.localize(date.replace(hour=business_hours[0], minute=0, second=0, microsecond=0))
        day_end = tz.localize(date.replace(hour=business_hours[1], minute=0, second=0, microsecond=0))

        body = {
            "timeMin": day_start.isoformat(),
            "timeMax": day_end.isoformat(),
            "items": [{"id": self.calendar_id}],
            "timeZone": self.timezone,
        }

        try:
            freebusy = self.service.freebusy().query(body=body).execute()
            busy_periods = freebusy["calendars"][self.calendar_id]["busy"]
        except HttpError as e:
            logger.error(f"Error consultando Calendar API: {e}")
            raise

        # Parsear periodos ocupados
        busy = []
        for period in busy_periods:
            busy_start = datetime.fromisoformat(period["start"])
            busy_end = datetime.fromisoformat(period["end"])
            busy.append((busy_start, busy_end))

        # Ordenar por inicio
        busy.sort(key=lambda x: x[0])

        # Calcular slots libres
        slot_duration = timedelta(minutes=duration_minutes)
        buffer = timedelta(minutes=buffer_minutes)
        available_slots = []

        current = day_start
        for busy_start, busy_end in busy:
            # Espacio libre antes del evento ocupado
            while current + slot_duration <= busy_start:
                slot = CalendarSlot(start=current, end=current + slot_duration)
                available_slots.append(slot)
                current = current + slot_duration + buffer
            # Avanzar despues del evento ocupado + buffer
            current = max(current, busy_end + buffer)

        # Slots libres despues del ultimo evento
        while current + slot_duration <= day_end:
            slot = CalendarSlot(start=current, end=current + slot_duration)
            available_slots.append(slot)
            current = current + slot_duration + buffer

        return available_slots

    async def create_event(
        self,
        summary: str,
        start: datetime,
        end: datetime,
        description: str | None = None,
        attendee_email: str | None = None,
    ) -> CalendarEvent:
        """
        Crea un evento en Google Calendar.
        Retorna el evento creado con su ID para futuras modificaciones.
        """
        event_body = {
            "summary": summary,
            "start": {
                "dateTime": start.isoformat(),
                "timeZone": self.timezone,
            },
            "end": {
                "dateTime": end.isoformat(),
                "timeZone": self.timezone,
            },
        }

        if description:
            event_body["description"] = description

        if attendee_email:
            event_body["attendees"] = [{"email": attendee_email}]

        try:
            event = self.service.events().insert(
                calendarId=self.calendar_id,
                body=event_body,
                sendUpdates="all" if attendee_email else "none",
            ).execute()

            return CalendarEvent(
                event_id=event["id"],
                summary=event["summary"],
                start=datetime.fromisoformat(event["start"]["dateTime"]),
                end=datetime.fromisoformat(event["end"]["dateTime"]),
                html_link=event.get("htmlLink"),
            )
        except HttpError as e:
            logger.error(f"Error creando evento: {e}")
            raise

    async def modify_event(
        self,
        event_id: str,
        new_start: datetime | None = None,
        new_end: datetime | None = None,
        new_summary: str | None = None,
    ) -> CalendarEvent:
        """
        Modifica un evento existente.
        Solo actualiza los campos proporcionados.
        """
        try:
            # Obtener evento actual
            event = self.service.events().get(
                calendarId=self.calendar_id,
                eventId=event_id,
            ).execute()

            if new_start:
                event["start"]["dateTime"] = new_start.isoformat()
            if new_end:
                event["end"]["dateTime"] = new_end.isoformat()
            if new_summary:
                event["summary"] = new_summary

            updated = self.service.events().update(
                calendarId=self.calendar_id,
                eventId=event_id,
                body=event,
                sendUpdates="all",
            ).execute()

            return CalendarEvent(
                event_id=updated["id"],
                summary=updated["summary"],
                start=datetime.fromisoformat(updated["start"]["dateTime"]),
                end=datetime.fromisoformat(updated["end"]["dateTime"]),
                html_link=updated.get("htmlLink"),
            )
        except HttpError as e:
            logger.error(f"Error modificando evento {event_id}: {e}")
            raise

    async def cancel_event(self, event_id: str) -> bool:
        """
        Cancela (elimina) un evento del calendario.
        Retorna True si se cancelo exitosamente.
        """
        try:
            self.service.events().delete(
                calendarId=self.calendar_id,
                eventId=event_id,
                sendUpdates="all",
            ).execute()
            return True
        except HttpError as e:
            logger.error(f"Error cancelando evento {event_id}: {e}")
            raise

    async def list_events(
        self,
        time_min: datetime,
        time_max: datetime,
        max_results: int = 50,
    ) -> list[CalendarEvent]:
        """
        Lista eventos en un rango de tiempo.
        """
        try:
            events_result = self.service.events().list(
                calendarId=self.calendar_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
                timeZone=self.timezone,
            ).execute()

            events = []
            for item in events_result.get("items", []):
                events.append(CalendarEvent(
                    event_id=item["id"],
                    summary=item.get("summary", ""),
                    start=datetime.fromisoformat(item["start"]["dateTime"]),
                    end=datetime.fromisoformat(item["end"]["dateTime"]),
                    html_link=item.get("htmlLink"),
                ))
            return events
        except HttpError as e:
            logger.error(f"Error listando eventos: {e}")
            raise
```

**Dependencias Python**: `google-api-python-client`, `google-auth-oauthlib`, `pytz`. Agregar a `requirements.txt`.

### 4. Calendar Tools para LangGraph (`app/agents/tools/calendar_tools.py`)

Las tools se registran usando el decorador `@tool` de LangChain y son invocadas por el scheduling agent via function calling de GPT-4o.

```python
from datetime import datetime, timedelta
from uuid import UUID
from langchain_core.tools import tool
from pydantic import BaseModel, Field
import logging

from app.services.calendar import GoogleCalendarService
from app.db.session import tenant_session
from app.models.service_type import Appointment, ServiceType

logger = logging.getLogger(__name__)


# --- Schemas de entrada para las tools ---

class CheckAvailabilityInput(BaseModel):
    date: str = Field(description="Fecha en formato YYYY-MM-DD")
    service_type_name: str = Field(description="Nombre del tipo de servicio")

class CreateAppointmentInput(BaseModel):
    contact_id: str = Field(description="UUID del contacto")
    datetime_iso: str = Field(description="Fecha y hora en formato ISO 8601")
    service_type_name: str = Field(description="Nombre del tipo de servicio")
    notes: str | None = Field(default=None, description="Notas adicionales")

class ModifyAppointmentInput(BaseModel):
    appointment_id: str = Field(description="UUID de la cita a modificar")
    new_datetime_iso: str = Field(description="Nueva fecha y hora en formato ISO 8601")

class CancelAppointmentInput(BaseModel):
    appointment_id: str = Field(description="UUID de la cita a cancelar")
    reason: str = Field(description="Razon de la cancelacion")

class ListAppointmentsInput(BaseModel):
    contact_id: str = Field(description="UUID del contacto")
    date_from: str = Field(description="Fecha inicio en formato YYYY-MM-DD")
    date_to: str = Field(description="Fecha fin en formato YYYY-MM-DD")


# --- Tools ---

# NOTA: Las tools reciben client_id a traves del estado del grafo,
# no como parametro directo. Se inyecta via el RunnableConfig de LangGraph.

@tool
async def check_availability(
    date: str,
    service_type_name: str,
    config: dict | None = None,
) -> str:
    """
    Verifica la disponibilidad para una fecha y tipo de servicio.
    Retorna los horarios disponibles.

    Args:
        date: Fecha en formato YYYY-MM-DD
        service_type_name: Nombre del tipo de servicio (e.g. "Consulta general")
    """
    client_id = UUID(config["configurable"]["client_id"])

    # Obtener service_type
    async with tenant_session(client_id) as session:
        from sqlalchemy import select
        result = await session.execute(
            select(ServiceType).where(
                ServiceType.name == service_type_name,
                ServiceType.is_active == True,
            )
        )
        service_type = result.scalar_one_or_none()
        if not service_type:
            return f"No encontre el tipo de servicio '{service_type_name}'. Los tipos disponibles son: {await _list_service_types(client_id)}"

    calendar = await GoogleCalendarService.from_tenant(client_id)
    target_date = datetime.strptime(date, "%Y-%m-%d")

    slots = await calendar.check_availability(
        date=target_date,
        duration_minutes=service_type.duration_minutes,
        buffer_minutes=service_type.buffer_minutes,
    )

    if not slots:
        return f"No hay horarios disponibles para el {date}. Puedo verificar otro dia si lo deseas."

    slots_text = "\n".join([f"  - {s.to_dict()['display']}" for s in slots])
    return f"Horarios disponibles para {date} ({service_type_name}, {service_type.duration_minutes} min):\n{slots_text}\n\nIndica cual horario prefieres."


@tool
async def create_appointment(
    contact_id: str,
    datetime_iso: str,
    service_type_name: str,
    notes: str | None = None,
    config: dict | None = None,
) -> str:
    """
    Crea una cita en el calendario.
    IMPORTANTE: Confirmar con el usuario ANTES de llamar esta tool.

    Args:
        contact_id: UUID del contacto
        datetime_iso: Fecha y hora en formato ISO 8601 (e.g. "2024-01-15T10:00:00")
        service_type_name: Nombre del tipo de servicio
        notes: Notas adicionales opcionales
    """
    client_id = UUID(config["configurable"]["client_id"])
    conversation_id = config["configurable"].get("conversation_id")

    # Obtener service_type
    async with tenant_session(client_id) as session:
        from sqlalchemy import select
        result = await session.execute(
            select(ServiceType).where(
                ServiceType.name == service_type_name,
                ServiceType.is_active == True,
            )
        )
        service_type = result.scalar_one_or_none()
        if not service_type:
            return f"Tipo de servicio '{service_type_name}' no encontrado."

    start = datetime.fromisoformat(datetime_iso)
    end = start + timedelta(minutes=service_type.duration_minutes)

    # Crear evento en Google Calendar
    calendar = await GoogleCalendarService.from_tenant(client_id)

    # Obtener nombre del contacto para el titulo del evento
    async with tenant_session(client_id) as session:
        from app.models.contact import Contact
        contact = await session.get(Contact, UUID(contact_id))
        contact_name = contact.display_name or f"{contact.first_name or ''} {contact.last_name or ''}".strip() or "Contacto"

    event = await calendar.create_event(
        summary=f"{service_type_name} - {contact_name}",
        start=start,
        end=end,
        description=notes,
    )

    # Guardar appointment en la base de datos
    async with tenant_session(client_id) as session:
        appointment = Appointment(
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
        session.add(appointment)
        await session.commit()

    return (
        f"Cita creada exitosamente:\n"
        f"  Servicio: {service_type_name}\n"
        f"  Fecha: {start.strftime('%d/%m/%Y')}\n"
        f"  Hora: {start.strftime('%H:%M')} - {end.strftime('%H:%M')}\n"
        f"  Duracion: {service_type.duration_minutes} minutos\n"
        f"Si necesitas modificar o cancelar, no dudes en avisarme."
    )


@tool
async def modify_appointment(
    appointment_id: str,
    new_datetime_iso: str,
    config: dict | None = None,
) -> str:
    """
    Modifica la fecha/hora de una cita existente.

    Args:
        appointment_id: UUID de la cita
        new_datetime_iso: Nueva fecha y hora en formato ISO 8601
    """
    client_id = UUID(config["configurable"]["client_id"])

    async with tenant_session(client_id) as session:
        appointment = await session.get(Appointment, UUID(appointment_id))
        if not appointment:
            return "No encontre esa cita. Puedo buscar tus citas si me indicas tu nombre."
        if appointment.status == "cancelled":
            return "Esa cita ya fue cancelada. Puedo crear una nueva si lo deseas."

        service_type = await session.get(ServiceType, appointment.service_type_id)
        new_start = datetime.fromisoformat(new_datetime_iso)
        new_end = new_start + timedelta(minutes=service_type.duration_minutes)

        # Modificar en Google Calendar
        calendar = await GoogleCalendarService.from_tenant(client_id)
        if appointment.google_event_id:
            await calendar.modify_event(
                event_id=appointment.google_event_id,
                new_start=new_start,
                new_end=new_end,
            )

        # Actualizar en base de datos
        appointment.starts_at = new_start
        appointment.ends_at = new_end
        await session.commit()

    return (
        f"Cita reprogramada:\n"
        f"  Nueva fecha: {new_start.strftime('%d/%m/%Y')}\n"
        f"  Nueva hora: {new_start.strftime('%H:%M')} - {new_end.strftime('%H:%M')}"
    )


@tool
async def cancel_appointment(
    appointment_id: str,
    reason: str,
    config: dict | None = None,
) -> str:
    """
    Cancela una cita existente.

    Args:
        appointment_id: UUID de la cita
        reason: Razon de la cancelacion
    """
    client_id = UUID(config["configurable"]["client_id"])

    async with tenant_session(client_id) as session:
        appointment = await session.get(Appointment, UUID(appointment_id))
        if not appointment:
            return "No encontre esa cita."
        if appointment.status == "cancelled":
            return "Esa cita ya estaba cancelada."

        # Cancelar en Google Calendar
        calendar = await GoogleCalendarService.from_tenant(client_id)
        if appointment.google_event_id:
            await calendar.cancel_event(appointment.google_event_id)

        # Actualizar en base de datos
        appointment.status = "cancelled"
        appointment.cancelled_reason = reason
        await session.commit()

    return f"Cita cancelada. Razon registrada: {reason}. Puedo agendar una nueva cita si lo deseas."


@tool
async def list_appointments(
    contact_id: str,
    date_from: str,
    date_to: str,
    config: dict | None = None,
) -> str:
    """
    Lista las citas de un contacto en un rango de fechas.

    Args:
        contact_id: UUID del contacto
        date_from: Fecha inicio en formato YYYY-MM-DD
        date_to: Fecha fin en formato YYYY-MM-DD
    """
    client_id = UUID(config["configurable"]["client_id"])

    from sqlalchemy import select
    start = datetime.strptime(date_from, "%Y-%m-%d")
    end = datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)

    async with tenant_session(client_id) as session:
        result = await session.execute(
            select(Appointment)
            .where(
                Appointment.contact_id == UUID(contact_id),
                Appointment.starts_at >= start,
                Appointment.starts_at <= end,
                Appointment.status != "cancelled",
            )
            .order_by(Appointment.starts_at)
        )
        appointments = result.scalars().all()

    if not appointments:
        return f"No hay citas programadas entre {date_from} y {date_to}."

    lines = []
    for apt in appointments:
        lines.append(
            f"  - {apt.starts_at.strftime('%d/%m/%Y %H:%M')} | "
            f"{apt.title} | Estado: {apt.status} | ID: {apt.id}"
        )

    return f"Citas encontradas:\n" + "\n".join(lines)


# --- Helper interno ---

async def _list_service_types(client_id: UUID) -> str:
    """Retorna lista de service_types activos del tenant."""
    from sqlalchemy import select
    async with tenant_session(client_id) as session:
        result = await session.execute(
            select(ServiceType).where(ServiceType.is_active == True)
        )
        types = result.scalars().all()
    if not types:
        return "No hay tipos de servicio configurados."
    return ", ".join([f"'{t.name}' ({t.duration_minutes} min)" for t in types])
```

### 5. Nodo Scheduling para LangGraph (`app/agents/nodes/scheduling.py`)

```python
from datetime import datetime
from uuid import UUID
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
import logging

from app.agents.state import ConversationState
from app.agents.tools.calendar_tools import (
    check_availability,
    create_appointment,
    modify_appointment,
    cancel_appointment,
    list_appointments,
)
from app.db.session import tenant_session

logger = logging.getLogger(__name__)

# Tools disponibles para el scheduling agent
SCHEDULING_TOOLS = [
    check_availability,
    create_appointment,
    modify_appointment,
    cancel_appointment,
    list_appointments,
]

SCHEDULING_SYSTEM_PROMPT = """Eres un asistente de agendamiento. Tu rol es ayudar a los clientes a:
1. Consultar disponibilidad de horarios
2. Crear citas nuevas
3. Modificar citas existentes
4. Cancelar citas
5. Consultar sus citas programadas

Reglas:
- SIEMPRE confirma con el usuario antes de crear o modificar una cita.
- Pregunta por el tipo de servicio si no lo especifica.
- Pregunta por la fecha y hora deseada si no lo especifica.
- Si no hay disponibilidad, ofrece fechas alternativas.
- Usa un tono amable y profesional.
- Responde en el idioma del usuario.
- Formatea las fechas de forma legible (dd/mm/yyyy HH:MM).

Tipos de servicio disponibles:
{service_types}

Fecha y hora actual: {current_datetime}
Zona horaria: {timezone}
"""


async def scheduling_node(state: ConversationState) -> dict:
    """
    Nodo de agendamiento del grafo.

    Usa GPT-4o con function calling para invocar las calendar tools.
    Soporta dialogo multi-turno gracias al checkpointing de LangGraph.

    Flujo:
    1. Cargar configuracion del tenant (service types, timezone)
    2. Construir system prompt con contexto
    3. Invocar LLM con tools
    4. Si el LLM llama una tool, ejecutarla y devolver resultado
    5. Si el LLM responde directamente, usar como response_text

    PAT-003: Retorna dict parcial, nunca muta el estado.
    """
    client_id = state["client_id"]
    message_text = state["message"].get("text", "")
    model_to_use = state.get("model_to_use", "gpt-4o")

    # Cargar service types del tenant
    service_types_text = await _get_service_types_text(UUID(client_id))

    # Cargar timezone del tenant
    timezone = await _get_tenant_timezone(UUID(client_id))

    # Construir system prompt
    system_prompt = SCHEDULING_SYSTEM_PROMPT.format(
        service_types=service_types_text,
        current_datetime=datetime.now().strftime("%Y-%m-%d %H:%M"),
        timezone=timezone,
    )

    # Crear LLM con tools
    llm = ChatOpenAI(
        model=model_to_use,
        temperature=0,
    ).bind_tools(SCHEDULING_TOOLS)

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=message_text),
    ]

    # Configuracion para las tools (inyeccion de client_id y conversation_id)
    tool_config = {
        "configurable": {
            "client_id": client_id,
            "conversation_id": state["conversation_id"],
        }
    }

    try:
        response = await llm.ainvoke(messages)

        # Si el LLM decidio llamar tools
        if response.tool_calls:
            tool_results = []
            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]

                # Buscar y ejecutar la tool
                tool_fn = _get_tool_by_name(tool_name)
                if tool_fn:
                    result = await tool_fn.ainvoke(
                        {**tool_args, "config": tool_config}
                    )
                    tool_results.append(result)
                else:
                    tool_results.append(f"Tool '{tool_name}' no encontrada.")

            # Generar respuesta final con los resultados de las tools
            final_response = await _generate_final_response(
                model_to_use=model_to_use,
                system_prompt=system_prompt,
                user_message=message_text,
                tool_results=tool_results,
            )

            return {
                "response_text": final_response,
                "intent": "scheduling",
            }
        else:
            # Respuesta directa del LLM (pregunta de clarificacion)
            return {
                "response_text": response.content,
                "intent": "scheduling",
            }

    except Exception as e:
        logger.error(f"Error en scheduling node: {e}", exc_info=True)
        return {
            "response_text": "Hubo un problema al procesar tu solicitud de agendamiento. Te transfiero con un agente para ayudarte.",
            "requires_handoff": True,
            "handoff_reason": "scheduling_error",
        }


def _get_tool_by_name(name: str):
    """Busca una tool por nombre."""
    tools_map = {t.name: t for t in SCHEDULING_TOOLS}
    return tools_map.get(name)


async def _get_service_types_text(client_id: UUID) -> str:
    """Obtiene los tipos de servicio activos del tenant como texto."""
    from sqlalchemy import select
    from app.models.service_type import ServiceType

    async with tenant_session(client_id) as session:
        result = await session.execute(
            select(ServiceType).where(ServiceType.is_active == True)
        )
        types = result.scalars().all()

    if not types:
        return "No hay tipos de servicio configurados."

    lines = []
    for t in types:
        lines.append(f"- {t.name}: {t.description or 'Sin descripcion'} ({t.duration_minutes} minutos)")
    return "\n".join(lines)


async def _get_tenant_timezone(client_id: UUID) -> str:
    """Obtiene la zona horaria configurada para el tenant."""
    from sqlalchemy import text

    async with tenant_session(client_id) as session:
        result = await session.execute(
            text("""
                SELECT settings->>'timezone'
                FROM agent_configs
                WHERE client_id = :client_id AND agent_type = 'scheduling'
            """),
            {"client_id": str(client_id)},
        )
        tz = result.scalar()
    return tz or "America/Mexico_City"


async def _generate_final_response(
    model_to_use: str,
    system_prompt: str,
    user_message: str,
    tool_results: list[str],
) -> str:
    """Genera respuesta final incorporando resultados de las tools."""
    llm = ChatOpenAI(model=model_to_use, temperature=0)

    results_text = "\n\n".join(tool_results)
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_message),
        SystemMessage(content=f"Resultados de las operaciones realizadas:\n{results_text}\n\nGenera una respuesta amable para el usuario basandote en estos resultados."),
    ]

    response = await llm.ainvoke(messages)
    return response.content
```

### 6. Integracion del nodo scheduling en el grafo (`app/agents/graph.py`)

Agregar el nodo scheduling al grafo existente del Sprint 6:

```python
# --- Agregar al build_conversation_graph() existente ---

from app.agents.nodes.scheduling import scheduling_node

def build_conversation_graph():
    """
    Construye el grafo de conversacion con el nodo de scheduling.

    Modificar el grafo existente del Sprint 6 para agregar:
    1. El nodo 'scheduling' que conecta con el scheduling agent
    2. El edge condicional desde intent_routing que enruta "scheduling" al nodo scheduling
    """
    graph = StateGraph(ConversationState)

    # Nodos existentes (Sprint 6)
    graph.add_node("token_budget_check", token_budget_check_node)
    graph.add_node("intent_routing", intent_routing_node)
    graph.add_node("rag_query", rag_query_node)
    graph.add_node("respond", respond_node)
    graph.add_node("human_handoff", human_handoff_node)
    graph.add_node("training_approval", training_approval_node)

    # Nodo nuevo (Sprint 7)
    graph.add_node("scheduling", scheduling_node)

    # Edge de entrada
    graph.add_edge(START, "token_budget_check")

    # Despues de token_budget_check
    graph.add_conditional_edges(
        "token_budget_check",
        route_after_budget,
        {
            "continue": "intent_routing",
            "handoff": "human_handoff",
        },
    )

    # Despues de intent_routing: agregar "scheduling" a las opciones
    graph.add_conditional_edges(
        "intent_routing",
        route_after_intent,
        {
            "rag_query": "rag_query",
            "respond": "respond",
            "human_handoff": "human_handoff",
            "scheduling": "scheduling",  # NUEVO
        },
    )

    # Despues de scheduling: enviar respuesta o handoff
    graph.add_conditional_edges(
        "scheduling",
        route_after_scheduling,
        {
            "respond": "respond",
            "handoff": "human_handoff",
        },
    )

    # ... (resto de edges existentes del Sprint 6)

    graph.add_edge("respond", END)
    graph.add_edge("human_handoff", END)
    graph.add_edge("training_approval", END)

    return graph.compile()


def route_after_intent(state: ConversationState) -> str:
    """
    Routing condicional despues del intent.
    Modificado para incluir scheduling.
    """
    intent = state.get("intent")

    if intent == "scheduling":
        return "scheduling"
    elif intent in ("rag_query",):
        return "rag_query"
    elif intent in ("greeting", "farewell"):
        return "respond"
    elif intent in ("human_request", "complaint"):
        return "human_handoff"
    else:
        return "rag_query"  # Default: intentar RAG


def route_after_scheduling(state: ConversationState) -> str:
    """Routing despues del scheduling node."""
    if state.get("requires_handoff"):
        return "handoff"
    return "respond"
```

### 7. CRUD Contactos (`app/api/v1/contacts.py`)

```python
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_tenant_session, require_role
from app.models.contact import Contact
from app.models.contact_identifier import ContactIdentifier
from app.models.contact_tag import ContactTag
from app.models.conversation import Conversation
from app.models.internal_note import InternalNote
from app.schemas.contact import (
    ContactCreate,
    ContactUpdate,
    ContactResponse,
    ContactDetailResponse,
    ContactMergeRequest,
)
from app.schemas.common import PaginatedResponse

router = APIRouter(prefix="/contacts", tags=["contacts"])


@router.get("", response_model=PaginatedResponse[ContactResponse])
async def list_contacts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None, description="Buscar por nombre"),
    tag_id: UUID | None = Query(None, description="Filtrar por tag"),
    session: AsyncSession = Depends(get_tenant_session),
    current_user: dict = Depends(get_current_user),
):
    """
    Lista contactos del tenant con paginacion, busqueda y filtro por tags.

    - Busqueda por first_name, last_name, display_name (case-insensitive)
    - Filtro por tag_id
    - Excluye contactos con merged_into_id (ya fusionados)
    - Ordenados por updated_at DESC
    """
    query = select(Contact).where(Contact.merged_into_id == None)

    # Filtro de busqueda por nombre
    if search:
        search_filter = f"%{search}%"
        query = query.where(
            or_(
                Contact.first_name.ilike(search_filter),
                Contact.last_name.ilike(search_filter),
                Contact.display_name.ilike(search_filter),
            )
        )

    # Filtro por tag
    if tag_id:
        query = query.join(ContactTag, Contact.id == ContactTag.contact_id).where(
            ContactTag.tag_id == tag_id
        )

    # Contar total
    count_query = select(func.count()).select_from(query.subquery())
    total = (await session.execute(count_query)).scalar()

    # Paginacion
    query = query.order_by(Contact.updated_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await session.execute(query)
    contacts = result.scalars().all()

    return PaginatedResponse(
        items=[ContactResponse.model_validate(c) for c in contacts],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )


@router.get("/{contact_id}", response_model=ContactDetailResponse)
async def get_contact(
    contact_id: UUID,
    session: AsyncSession = Depends(get_tenant_session),
    current_user: dict = Depends(get_current_user),
):
    """
    Detalle de un contacto con sus identifiers, tags y notas.
    """
    contact = await session.get(Contact, contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contacto no encontrado")

    # Cargar identifiers
    identifiers_result = await session.execute(
        select(ContactIdentifier).where(ContactIdentifier.contact_id == contact_id)
    )
    identifiers = identifiers_result.scalars().all()

    # Cargar tags
    tags_result = await session.execute(
        select(ContactTag).where(ContactTag.contact_id == contact_id)
    )
    tags = tags_result.scalars().all()

    # Cargar notas recientes
    notes_result = await session.execute(
        select(InternalNote)
        .where(InternalNote.contact_id == contact_id)
        .order_by(InternalNote.created_at.desc())
        .limit(20)
    )
    notes = notes_result.scalars().all()

    return ContactDetailResponse.from_contact(contact, identifiers, tags, notes)


@router.post("", response_model=ContactResponse, status_code=201)
async def create_contact(
    data: ContactCreate,
    session: AsyncSession = Depends(get_tenant_session),
    current_user: dict = Depends(get_current_user),
):
    """Crear un nuevo contacto."""
    contact = Contact(
        client_id=UUID(current_user["client_id"]),
        first_name=data.first_name,
        last_name=data.last_name,
        display_name=data.display_name,
        metadata_=data.metadata or {},
    )
    session.add(contact)
    await session.commit()
    await session.refresh(contact)
    return ContactResponse.model_validate(contact)


@router.put("/{contact_id}", response_model=ContactResponse)
async def update_contact(
    contact_id: UUID,
    data: ContactUpdate,
    session: AsyncSession = Depends(get_tenant_session),
    current_user: dict = Depends(get_current_user),
):
    """Actualizar un contacto existente."""
    contact = await session.get(Contact, contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contacto no encontrado")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(contact, field, value)

    await session.commit()
    await session.refresh(contact)
    return ContactResponse.model_validate(contact)


@router.post("/{contact_id}/merge/{target_id}", response_model=ContactResponse)
async def merge_contacts(
    contact_id: UUID,
    target_id: UUID,
    session: AsyncSession = Depends(get_tenant_session),
    current_user: dict = Depends(require_role(["admin", "super_admin"])),
):
    """
    Merge manual de contactos.
    Mueve todos los identifiers, conversations, notes y tags del contacto origen
    al contacto destino (target). Marca el origen con merged_into_id.

    Solo admin y super_admin pueden hacer merge.
    """
    if contact_id == target_id:
        raise HTTPException(status_code=400, detail="No se puede fusionar un contacto consigo mismo")

    source = await session.get(Contact, contact_id)
    target = await session.get(Contact, target_id)

    if not source:
        raise HTTPException(status_code=404, detail="Contacto origen no encontrado")
    if not target:
        raise HTTPException(status_code=404, detail="Contacto destino no encontrado")
    if source.merged_into_id:
        raise HTTPException(status_code=400, detail="El contacto origen ya fue fusionado")

    from app.services.contact_unifier import ContactUnifier
    unifier = ContactUnifier(session)
    await unifier.merge(source_id=contact_id, target_id=target_id)

    await session.refresh(target)
    return ContactResponse.model_validate(target)


@router.get("/{contact_id}/conversations", response_model=PaginatedResponse)
async def get_contact_conversations(
    contact_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_tenant_session),
    current_user: dict = Depends(get_current_user),
):
    """Historial de conversaciones de un contacto."""
    contact = await session.get(Contact, contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contacto no encontrado")

    query = (
        select(Conversation)
        .where(Conversation.contact_id == contact_id)
        .order_by(Conversation.last_message_at.desc().nullslast())
    )

    count_query = select(func.count()).select_from(query.subquery())
    total = (await session.execute(count_query)).scalar()

    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(query)
    conversations = result.scalars().all()

    from app.schemas.conversation import ConversationResponse
    return PaginatedResponse(
        items=[ConversationResponse.model_validate(c) for c in conversations],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )
```

### 8. Contact Unifier (`app/services/contact_unifier.py`)

```python
from uuid import UUID
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.models.contact import Contact
from app.models.contact_identifier import ContactIdentifier
from app.models.conversation import Conversation
from app.models.internal_note import InternalNote
from app.models.contact_tag import ContactTag
from app.db.session import tenant_session

logger = logging.getLogger(__name__)


class ContactUnifier:
    """
    Servicio de unificacion de contactos cross-canal.

    Cuando un mismo usuario contacta por WhatsApp y Telegram con el mismo
    numero de telefono, el sistema debe reconocerlo como un unico contacto.

    Dos flujos:
    1. Automatico: Al recibir un mensaje, buscar contact_identifier existente
    2. Manual: Merge de dos contactos via API (admin/super_admin)
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def resolve_contact(
        self,
        client_id: UUID,
        channel: str,
        identifier_value: str,
        encryption_key: str,
    ) -> tuple[Contact, bool]:
        """
        Resuelve el contacto a partir del identificador de canal.

        Busca si existe un contact_identifier con el mismo (channel, identifier_value).
        Si existe, retorna el contacto existente.
        Si no existe, crea un nuevo contacto e identifier.

        Retorna: (contact, is_new) - el contacto y si fue creado nuevo.

        NOTA: identifier_value se almacena cifrado con pgcrypto.
        La busqueda requiere descifrar para comparar, o cifrar el valor
        de busqueda y comparar. Se usa pgp_sym_encrypt para consistencia.
        """
        from sqlalchemy import text

        # Buscar identifier existente
        # NOTA: Como identifier_value esta cifrado con pgcrypto,
        # necesitamos usar pgp_sym_decrypt para la comparacion.
        result = await self.session.execute(
            text("""
                SELECT ci.id, ci.contact_id
                FROM contact_identifiers ci
                WHERE ci.channel = :channel
                  AND pgp_sym_decrypt(ci.identifier_value, :key) = :value
                LIMIT 1
            """),
            {
                "channel": channel,
                "value": identifier_value,
                "key": encryption_key,
            },
        )
        row = result.fetchone()

        if row:
            # Contacto existente
            contact_id = row[1]
            contact = await self.session.get(Contact, contact_id)

            # Si el contacto fue mergeado, seguir la cadena
            if contact and contact.merged_into_id:
                contact = await self._follow_merge_chain(contact.merged_into_id)

            return contact, False
        else:
            # Crear nuevo contacto e identifier
            contact = Contact(
                client_id=client_id,
                display_name=None,  # Se puede actualizar despues
            )
            self.session.add(contact)
            await self.session.flush()  # Para obtener contact.id

            # Crear identifier cifrado
            await self.session.execute(
                text("""
                    INSERT INTO contact_identifiers (client_id, contact_id, channel, identifier_value, is_primary)
                    VALUES (:client_id, :contact_id, :channel, pgp_sym_encrypt(:value, :key), true)
                """),
                {
                    "client_id": str(client_id),
                    "contact_id": str(contact.id),
                    "channel": channel,
                    "value": identifier_value,
                    "key": encryption_key,
                },
            )

            return contact, True

    async def merge(self, source_id: UUID, target_id: UUID) -> None:
        """
        Merge de dos contactos.

        Mueve al contacto target:
        1. Todos los contact_identifiers del source
        2. Todas las conversations del source
        3. Todas las internal_notes del source
        4. Todos los contact_tags del source (evitando duplicados)

        Marca source.merged_into_id = target_id.

        IMPORTANTE: Este merge es irreversible. Solo admin/super_admin.
        """
        logger.info(f"Merging contact {source_id} into {target_id}")

        # 1. Mover contact_identifiers
        await self.session.execute(
            update(ContactIdentifier)
            .where(ContactIdentifier.contact_id == source_id)
            .values(contact_id=target_id)
        )

        # 2. Mover conversations
        await self.session.execute(
            update(Conversation)
            .where(Conversation.contact_id == source_id)
            .values(contact_id=target_id)
        )

        # 3. Mover internal_notes
        await self.session.execute(
            update(InternalNote)
            .where(InternalNote.contact_id == source_id)
            .values(contact_id=target_id)
        )

        # 4. Mover contact_tags (evitar duplicados)
        # Obtener tags del target
        target_tags_result = await self.session.execute(
            select(ContactTag.tag_id).where(ContactTag.contact_id == target_id)
        )
        target_tag_ids = set(row[0] for row in target_tags_result.fetchall())

        # Obtener tags del source
        source_tags_result = await self.session.execute(
            select(ContactTag).where(ContactTag.contact_id == source_id)
        )
        source_tags = source_tags_result.scalars().all()

        for tag in source_tags:
            if tag.tag_id in target_tag_ids:
                # Tag duplicado: eliminar del source
                await self.session.delete(tag)
            else:
                # Mover al target
                tag.contact_id = target_id

        # 5. Marcar source como mergeado
        source = await self.session.get(Contact, source_id)
        source.merged_into_id = target_id

        await self.session.commit()
        logger.info(f"Contact {source_id} merged into {target_id} successfully")

    async def _follow_merge_chain(self, contact_id: UUID, max_depth: int = 10) -> Contact:
        """
        Sigue la cadena de merges hasta el contacto final.

        Previene ciclos infinitos con max_depth.
        """
        current = await self.session.get(Contact, contact_id)
        depth = 0
        while current and current.merged_into_id and depth < max_depth:
            current = await self.session.get(Contact, current.merged_into_id)
            depth += 1
        if depth >= max_depth:
            logger.warning(f"Merge chain too deep for contact {contact_id}")
        return current
```

### 9. Conversation Lifecycle (`app/services/conversation_lifecycle.py`)

Maquina de estados para las transiciones validas de conversaciones.

```python
from uuid import UUID
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException
import logging

from app.models.conversation import Conversation

logger = logging.getLogger(__name__)


# Definicion de transiciones validas
# Formato: {estado_actual: [estados_permitidos]}
VALID_TRANSITIONS: dict[str, list[str]] = {
    "new": ["bot_active", "human_active"],
    "bot_active": ["human_active", "waiting_human", "resolved"],
    "human_active": ["waiting_client", "resolved"],
    "waiting_human": ["human_active"],
    "waiting_client": ["human_active", "bot_active", "resolved"],
    "resolved": ["archived"],
    "archived": [],  # Terminal — no se puede transicionar desde archived
}


class ConversationLifecycle:
    """
    Gestion del ciclo de vida de conversaciones.

    La maquina de estados define las transiciones validas:
      new -> bot_active, human_active
      bot_active -> human_active, waiting_human, resolved
      human_active -> waiting_client, resolved
      waiting_human -> human_active
      waiting_client -> human_active, bot_active, resolved
      resolved -> archived
      archived -> (terminal)

    Cada transicion puede tener side effects (e.g. resolved_at timestamp).
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def transition(
        self,
        conversation: Conversation,
        new_status: str,
        user_id: UUID | None = None,
    ) -> Conversation:
        """
        Ejecuta una transicion de estado.

        Valida que la transicion sea permitida.
        Aplica side effects segun el nuevo estado.
        """
        current_status = conversation.status

        if new_status not in VALID_TRANSITIONS.get(current_status, []):
            raise HTTPException(
                status_code=400,
                detail=f"Transicion no permitida: {current_status} -> {new_status}. "
                       f"Transiciones validas desde '{current_status}': {VALID_TRANSITIONS.get(current_status, [])}"
            )

        old_status = conversation.status
        conversation.status = new_status
        conversation.updated_at = datetime.now(timezone.utc)

        # Side effects segun transicion
        if new_status == "human_active" and user_id:
            conversation.assigned_user_id = user_id

        if new_status == "resolved":
            conversation.resolved_at = datetime.now(timezone.utc)

        if new_status == "archived":
            pass  # No side effects adicionales

        if new_status in ("bot_active", "human_active"):
            # Limpiar resolved_at si se reabre
            if old_status in ("resolved",):
                conversation.resolved_at = None

        logger.info(
            f"Conversation {conversation.id} transitioned: "
            f"{old_status} -> {new_status}"
        )

        return conversation

    @staticmethod
    def validate_transition(current_status: str, new_status: str) -> bool:
        """Valida si una transicion es permitida sin ejecutarla."""
        return new_status in VALID_TRANSITIONS.get(current_status, [])

    @staticmethod
    def get_valid_transitions(status: str) -> list[str]:
        """Retorna las transiciones validas desde un estado."""
        return VALID_TRANSITIONS.get(status, [])
```

### 10. CRUD Conversaciones (`app/api/v1/conversations.py`)

```python
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_tenant_session, require_role
from app.models.conversation import Conversation
from app.models.message import Message
from app.services.conversation_lifecycle import ConversationLifecycle
from app.schemas.conversation import (
    ConversationResponse,
    ConversationDetailResponse,
    ConversationAssignRequest,
    ConversationStatusChangeRequest,
)
from app.schemas.common import PaginatedResponse

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("", response_model=PaginatedResponse[ConversationResponse])
async def list_conversations(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None, description="Filtrar por status"),
    channel: str | None = Query(None, description="Filtrar por canal"),
    assigned_user_id: UUID | None = Query(None, description="Filtrar por agente asignado"),
    session: AsyncSession = Depends(get_tenant_session),
    current_user: dict = Depends(get_current_user),
):
    """
    Lista conversaciones del tenant con filtros y paginacion.

    Filtros:
    - status: new, bot_active, human_active, waiting_human, waiting_client, resolved, archived
    - channel: whatsapp, telegram, messenger, instagram, webchat, email, sms
    - assigned_user_id: UUID del agente humano asignado

    Ordenadas por last_message_at DESC (mas recientes primero).
    """
    query = select(Conversation)

    if status:
        query = query.where(Conversation.status == status)
    if channel:
        query = query.where(Conversation.channel == channel)
    if assigned_user_id:
        query = query.where(Conversation.assigned_user_id == assigned_user_id)

    # Total
    count_query = select(func.count()).select_from(query.subquery())
    total = (await session.execute(count_query)).scalar()

    # Paginacion
    query = query.order_by(Conversation.last_message_at.desc().nullslast())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await session.execute(query)
    conversations = result.scalars().all()

    return PaginatedResponse(
        items=[ConversationResponse.model_validate(c) for c in conversations],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )


@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: UUID,
    page: int = Query(1, ge=1, description="Pagina de mensajes"),
    page_size: int = Query(50, ge=1, le=200, description="Mensajes por pagina"),
    session: AsyncSession = Depends(get_tenant_session),
    current_user: dict = Depends(get_current_user),
):
    """
    Detalle de una conversacion con mensajes paginados.

    Los mensajes se retornan ordenados por created_at ASC (cronologico).
    """
    conversation = await session.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversacion no encontrada")

    # Contar mensajes
    msg_count_query = select(func.count()).where(
        Message.conversation_id == conversation_id
    )
    total_messages = (await session.execute(msg_count_query)).scalar()

    # Obtener mensajes paginados (orden cronologico)
    messages_query = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    messages_result = await session.execute(messages_query)
    messages = messages_result.scalars().all()

    return ConversationDetailResponse.from_conversation(
        conversation=conversation,
        messages=messages,
        total_messages=total_messages,
        page=page,
        page_size=page_size,
    )


@router.put("/{conversation_id}/assign")
async def assign_conversation(
    conversation_id: UUID,
    data: ConversationAssignRequest,
    session: AsyncSession = Depends(get_tenant_session),
    current_user: dict = Depends(require_role(["admin", "supervisor", "super_admin"])),
):
    """
    Asignar una conversacion a un agente humano.

    Transiciona automaticamente a 'human_active' si es necesario.
    Solo admin, supervisor y super_admin pueden asignar.
    """
    conversation = await session.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversacion no encontrada")

    # Verificar que el usuario destino existe y pertenece al tenant
    from app.models.user import User
    target_user = await session.get(User, data.user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="Agente no encontrado")

    lifecycle = ConversationLifecycle(session)

    # Si la conversacion no esta en human_active, transicionar
    if conversation.status != "human_active":
        if ConversationLifecycle.validate_transition(conversation.status, "human_active"):
            await lifecycle.transition(conversation, "human_active", user_id=data.user_id)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"No se puede asignar una conversacion en estado '{conversation.status}'"
            )
    else:
        conversation.assigned_user_id = data.user_id

    await session.commit()
    return {"message": "Conversacion asignada", "assigned_to": str(data.user_id)}


@router.put("/{conversation_id}/status")
async def change_conversation_status(
    conversation_id: UUID,
    data: ConversationStatusChangeRequest,
    session: AsyncSession = Depends(get_tenant_session),
    current_user: dict = Depends(get_current_user),
):
    """
    Cambiar el estado de una conversacion.
    Valida que la transicion sea permitida segun la maquina de estados.

    Las transiciones validas son:
      new -> bot_active, human_active
      bot_active -> human_active, waiting_human, resolved
      human_active -> waiting_client, resolved
      waiting_human -> human_active
      waiting_client -> human_active, bot_active, resolved
      resolved -> archived
      archived -> (terminal, no transiciones)
    """
    conversation = await session.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversacion no encontrada")

    lifecycle = ConversationLifecycle(session)
    user_id = UUID(current_user["user_id"]) if data.status == "human_active" else None

    await lifecycle.transition(conversation, data.status, user_id=user_id)
    await session.commit()

    return {
        "message": f"Estado actualizado a '{data.status}'",
        "previous_status": conversation.status,
        "valid_next_transitions": ConversationLifecycle.get_valid_transitions(data.status),
    }
```

### 11. CRUD Tags (`app/api/v1/tags.py`)

```python
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_tenant_session
from app.models.tag import Tag
from app.models.contact_tag import ContactTag
from app.schemas.tag import TagCreate, TagResponse

router = APIRouter(prefix="/tags", tags=["tags"])


@router.get("", response_model=list[TagResponse])
async def list_tags(
    session: AsyncSession = Depends(get_tenant_session),
    current_user: dict = Depends(get_current_user),
):
    """Lista todas las tags del tenant."""
    result = await session.execute(
        select(Tag).order_by(Tag.name)
    )
    return [TagResponse.model_validate(t) for t in result.scalars().all()]


@router.post("", response_model=TagResponse, status_code=201)
async def create_tag(
    data: TagCreate,
    session: AsyncSession = Depends(get_tenant_session),
    current_user: dict = Depends(get_current_user),
):
    """Crear una nueva tag."""
    # Verificar unicidad por nombre
    existing = await session.execute(
        select(Tag).where(Tag.name == data.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"La tag '{data.name}' ya existe")

    tag = Tag(
        client_id=UUID(current_user["client_id"]),
        name=data.name,
        color=data.color,
    )
    session.add(tag)
    await session.commit()
    await session.refresh(tag)
    return TagResponse.model_validate(tag)


@router.delete("/{tag_id}", status_code=204)
async def delete_tag(
    tag_id: UUID,
    session: AsyncSession = Depends(get_tenant_session),
    current_user: dict = Depends(get_current_user),
):
    """Eliminar una tag. Las relaciones contact_tags se eliminan por CASCADE."""
    tag = await session.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag no encontrada")
    await session.delete(tag)
    await session.commit()


@router.post("/contacts/{contact_id}/tags/{tag_id}", status_code=201)
async def assign_tag_to_contact(
    contact_id: UUID,
    tag_id: UUID,
    session: AsyncSession = Depends(get_tenant_session),
    current_user: dict = Depends(get_current_user),
):
    """Asignar una tag a un contacto."""
    # Verificar existencia
    from app.models.contact import Contact
    contact = await session.get(Contact, contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contacto no encontrado")

    tag = await session.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag no encontrada")

    # Verificar que no este ya asignada
    existing = await session.execute(
        select(ContactTag).where(
            ContactTag.contact_id == contact_id,
            ContactTag.tag_id == tag_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Tag ya asignada a este contacto")

    contact_tag = ContactTag(
        client_id=UUID(current_user["client_id"]),
        contact_id=contact_id,
        tag_id=tag_id,
    )
    session.add(contact_tag)
    await session.commit()
    return {"message": "Tag asignada"}


@router.delete("/contacts/{contact_id}/tags/{tag_id}", status_code=204)
async def remove_tag_from_contact(
    contact_id: UUID,
    tag_id: UUID,
    session: AsyncSession = Depends(get_tenant_session),
    current_user: dict = Depends(get_current_user),
):
    """Remover una tag de un contacto."""
    result = await session.execute(
        select(ContactTag).where(
            ContactTag.contact_id == contact_id,
            ContactTag.tag_id == tag_id,
        )
    )
    contact_tag = result.scalar_one_or_none()
    if not contact_tag:
        raise HTTPException(status_code=404, detail="Relacion tag-contacto no encontrada")

    await session.delete(contact_tag)
    await session.commit()
```

### 12. CRUD Notas Internas (`app/api/v1/notes.py`)

```python
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_tenant_session
from app.models.internal_note import InternalNote
from app.models.contact import Contact
from app.schemas.note import NoteCreate, NoteResponse
from app.schemas.common import PaginatedResponse

router = APIRouter(prefix="/contacts/{contact_id}/notes", tags=["notes"])


@router.get("", response_model=PaginatedResponse[NoteResponse])
async def list_notes(
    contact_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_tenant_session),
    current_user: dict = Depends(get_current_user),
):
    """Lista notas internas de un contacto con paginacion."""
    contact = await session.get(Contact, contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contacto no encontrado")

    query = (
        select(InternalNote)
        .where(InternalNote.contact_id == contact_id)
        .order_by(InternalNote.created_at.desc())
    )

    count_query = select(func.count()).select_from(query.subquery())
    total = (await session.execute(count_query)).scalar()

    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(query)
    notes = result.scalars().all()

    return PaginatedResponse(
        items=[NoteResponse.model_validate(n) for n in notes],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )


@router.post("", response_model=NoteResponse, status_code=201)
async def create_note(
    contact_id: UUID,
    data: NoteCreate,
    session: AsyncSession = Depends(get_tenant_session),
    current_user: dict = Depends(get_current_user),
):
    """Crear una nota interna sobre un contacto."""
    contact = await session.get(Contact, contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contacto no encontrado")

    note = InternalNote(
        client_id=UUID(current_user["client_id"]),
        contact_id=contact_id,
        user_id=UUID(current_user["user_id"]),
        content=data.content,
    )
    session.add(note)
    await session.commit()
    await session.refresh(note)
    return NoteResponse.model_validate(note)
```

### 13. Schemas Pydantic (`app/schemas/`)

#### `app/schemas/contact.py` (extensiones)

```python
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, Field


class ContactCreate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None
    metadata: dict | None = None


class ContactUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None
    metadata: dict | None = None


class ContactResponse(BaseModel):
    id: UUID
    first_name: str | None
    last_name: str | None
    display_name: str | None
    merged_into_id: UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class IdentifierResponse(BaseModel):
    id: UUID
    channel: str
    is_primary: bool
    verified_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ContactDetailResponse(BaseModel):
    """Contacto con todos sus datos relacionados."""
    id: UUID
    first_name: str | None
    last_name: str | None
    display_name: str | None
    merged_into_id: UUID | None
    metadata: dict
    identifiers: list[IdentifierResponse]
    tags: list[dict]  # {id, name, color}
    notes: list[dict]  # {id, content, user_id, created_at}
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_contact(cls, contact, identifiers, tags, notes):
        return cls(
            id=contact.id,
            first_name=contact.first_name,
            last_name=contact.last_name,
            display_name=contact.display_name,
            merged_into_id=contact.merged_into_id,
            metadata=contact.metadata_ or {},
            identifiers=[IdentifierResponse.model_validate(i) for i in identifiers],
            tags=[{"id": str(t.tag_id), "name": t.tag.name if hasattr(t, 'tag') else None, "color": None} for t in tags],
            notes=[{
                "id": str(n.id),
                "content": n.content,
                "user_id": str(n.user_id),
                "created_at": n.created_at.isoformat(),
            } for n in notes],
            created_at=contact.created_at,
            updated_at=contact.updated_at,
        )


class ContactMergeRequest(BaseModel):
    """No se necesitan datos adicionales; los IDs vienen en la URL."""
    pass
```

#### `app/schemas/conversation.py` (extensiones)

```python
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, Field


class ConversationResponse(BaseModel):
    id: UUID
    contact_id: UUID
    channel: str
    status: str
    assigned_user_id: UUID | None
    subject: str | None
    started_at: datetime
    resolved_at: datetime | None
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MessageResponse(BaseModel):
    id: UUID
    direction: str
    message_type: str
    content: str | None
    media_url: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationDetailResponse(BaseModel):
    """Conversacion con mensajes paginados."""
    id: UUID
    contact_id: UUID
    channel: str
    status: str
    assigned_user_id: UUID | None
    subject: str | None
    started_at: datetime
    resolved_at: datetime | None
    last_message_at: datetime | None
    messages: list[MessageResponse]
    total_messages: int
    page: int
    page_size: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_conversation(cls, conversation, messages, total_messages, page, page_size):
        return cls(
            id=conversation.id,
            contact_id=conversation.contact_id,
            channel=conversation.channel,
            status=conversation.status,
            assigned_user_id=conversation.assigned_user_id,
            subject=conversation.subject,
            started_at=conversation.started_at,
            resolved_at=conversation.resolved_at,
            last_message_at=conversation.last_message_at,
            messages=[MessageResponse.model_validate(m) for m in messages],
            total_messages=total_messages,
            page=page,
            page_size=page_size,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )


class ConversationAssignRequest(BaseModel):
    user_id: UUID = Field(description="UUID del agente humano a asignar")


class ConversationStatusChangeRequest(BaseModel):
    status: str = Field(description="Nuevo estado de la conversacion")
```

#### `app/schemas/tag.py`

```python
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, Field


class TagCreate(BaseModel):
    name: str = Field(max_length=100)
    color: str | None = Field(None, max_length=7, pattern=r"^#[0-9A-Fa-f]{6}$")


class TagResponse(BaseModel):
    id: UUID
    name: str
    color: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
```

#### `app/schemas/note.py`

```python
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, Field


class NoteCreate(BaseModel):
    content: str = Field(min_length=1)


class NoteResponse(BaseModel):
    id: UUID
    contact_id: UUID
    user_id: UUID
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}
```

#### `app/schemas/scheduling.py`

```python
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, Field


class ServiceTypeCreate(BaseModel):
    name: str = Field(max_length=255)
    description: str | None = None
    duration_minutes: int = Field(default=60, ge=15, le=480)
    buffer_minutes: int = Field(default=15, ge=0, le=120)


class ServiceTypeResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    duration_minutes: int
    buffer_minutes: int
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class AppointmentResponse(BaseModel):
    id: UUID
    contact_id: UUID
    service_type_id: UUID
    title: str
    starts_at: datetime
    ends_at: datetime
    status: str
    notes: str | None
    google_event_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
```

### 14. Auto-cierre de conversaciones (`app/tasks/conversation_lifecycle.py`)

Task de Celery Beat que se ejecuta cada 15 minutos.

```python
from datetime import datetime, timezone, timedelta
from celery import shared_task
from sqlalchemy import select, update
import logging

logger = logging.getLogger(__name__)


@shared_task(
    name="app.tasks.auto_close_conversations",
    queue="bulk",
    acks_late=True,
    time_limit=300,  # 5 minutos max
)
def auto_close_conversations():
    """
    Tarea periodica para auto-cierre de conversaciones inactivas.

    Se ejecuta cada 15 minutos via Celery Beat.

    Reglas:
    1. waiting_client > 24 horas -> resolved
    2. resolved > 7 dias -> archived

    IMPORTANTE: Esta tarea opera sobre TODOS los tenants.
    No usa SET LOCAL porque necesita acceso cross-tenant.
    Se ejecuta con un rol de servicio que bypasea RLS.
    """
    import asyncio
    asyncio.run(_auto_close())


async def _auto_close():
    """Logica asincrona de auto-cierre."""
    from app.db.session import AsyncSessionLocal
    from app.models.conversation import Conversation
    from sqlalchemy import text

    now = datetime.now(timezone.utc)
    threshold_waiting = now - timedelta(hours=24)
    threshold_resolved = now - timedelta(days=7)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Desactivar RLS para esta sesion de servicio
            # NOTA: Esta tarea usa la conexion directa (DATABASE_URL),
            # no pgBouncer, porque necesita SET (no SET LOCAL) para
            # desactivar RLS. Alternativa: usar un rol de servicio
            # que tenga BYPASSRLS.
            #
            # Opcion recomendada: Crear un rol de servicio con BYPASSRLS
            # y usar una session factory separada para tareas de mantenimiento.

            # 1. waiting_client > 24h -> resolved
            result_waiting = await session.execute(
                text("""
                    UPDATE conversations
                    SET status = 'resolved',
                        resolved_at = now(),
                        updated_at = now()
                    WHERE status = 'waiting_client'
                      AND updated_at < :threshold
                    RETURNING id, client_id
                """),
                {"threshold": threshold_waiting},
            )
            closed_waiting = result_waiting.fetchall()

            # 2. resolved > 7 dias -> archived
            result_resolved = await session.execute(
                text("""
                    UPDATE conversations
                    SET status = 'archived',
                        updated_at = now()
                    WHERE status = 'resolved'
                      AND resolved_at < :threshold
                    RETURNING id, client_id
                """),
                {"threshold": threshold_resolved},
            )
            archived = result_resolved.fetchall()

    if closed_waiting:
        logger.info(f"Auto-closed {len(closed_waiting)} conversations (waiting_client > 24h -> resolved)")
    if archived:
        logger.info(f"Auto-archived {len(archived)} conversations (resolved > 7d -> archived)")


# Configuracion de Celery Beat (agregar a celery_config.py)
CELERY_BEAT_SCHEDULE_ADDITION = """
# Agregar al CELERY_BEAT_SCHEDULE existente en app/celery_config.py:

'auto-close-conversations': {
    'task': 'app.tasks.auto_close_conversations',
    'schedule': crontab(minute='*/15'),  # Cada 15 minutos
    'options': {'queue': 'bulk'},
},
"""
```

**IMPORTANTE sobre RLS y tareas de mantenimiento**: La tarea `auto_close_conversations` necesita operar sobre todos los tenants. Hay dos opciones:

1. **Rol con BYPASSRLS** (recomendada): Crear un rol de PostgreSQL con `BYPASSRLS` para tareas de servicio y usar una session factory separada.

```sql
-- Crear rol de servicio
CREATE ROLE service_worker WITH LOGIN PASSWORD 'secure_password' BYPASSRLS;
GRANT ALL ON ALL TABLES IN SCHEMA public TO service_worker;
```

2. **Iterar por tenant**: Obtener la lista de `client_id` y ejecutar `SET LOCAL` para cada uno. Mas seguro pero mas lento.

```python
# Alternativa: iterar por tenant
async def _auto_close_per_tenant():
    from app.db.session import AsyncSessionLocal, tenant_session
    from sqlalchemy import text

    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT id FROM clients WHERE is_active = true"))
        client_ids = [row[0] for row in result.fetchall()]

    for client_id in client_ids:
        async with tenant_session(client_id) as session:
            # ... ejecutar updates con RLS activo
            pass
```

### 15. Registro de routers (`app/main.py`)

Agregar los nuevos routers al app factory existente:

```python
# En app/main.py, dentro de la funcion create_app() o similar:

from app.api.v1.contacts import router as contacts_router
from app.api.v1.conversations import router as conversations_router
from app.api.v1.tags import router as tags_router
from app.api.v1.notes import router as notes_router

app.include_router(contacts_router, prefix="/api/v1")
app.include_router(conversations_router, prefix="/api/v1")
app.include_router(tags_router, prefix="/api/v1")
app.include_router(notes_router, prefix="/api/v1")
```

### 16. Tests

#### `tests/unit/test_scheduling_agent.py`

```python
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timedelta
from uuid import uuid4


@pytest.mark.asyncio
async def test_check_availability_returns_slots():
    """Consulta de disponibilidad retorna slots libres."""
    mock_calendar = AsyncMock()
    mock_calendar.check_availability.return_value = [
        MagicMock(to_dict=lambda: {"start": "2024-01-15T09:00", "end": "2024-01-15T10:00", "display": "09:00 - 10:00"}),
        MagicMock(to_dict=lambda: {"start": "2024-01-15T11:00", "end": "2024-01-15T12:00", "display": "11:00 - 12:00"}),
    ]

    with patch("app.agents.tools.calendar_tools.GoogleCalendarService.from_tenant", return_value=mock_calendar):
        from app.agents.tools.calendar_tools import check_availability
        result = await check_availability.ainvoke({
            "date": "2024-01-15",
            "service_type_name": "Consulta general",
            "config": {"configurable": {"client_id": str(uuid4())}},
        })
        assert "09:00 - 10:00" in result
        assert "11:00 - 12:00" in result


@pytest.mark.asyncio
async def test_check_availability_no_slots():
    """Dia completo retorna mensaje de no disponibilidad."""
    mock_calendar = AsyncMock()
    mock_calendar.check_availability.return_value = []

    with patch("app.agents.tools.calendar_tools.GoogleCalendarService.from_tenant", return_value=mock_calendar):
        from app.agents.tools.calendar_tools import check_availability
        result = await check_availability.ainvoke({
            "date": "2024-01-15",
            "service_type_name": "Consulta general",
            "config": {"configurable": {"client_id": str(uuid4())}},
        })
        assert "No hay horarios disponibles" in result


@pytest.mark.asyncio
async def test_create_appointment_saves_to_db():
    """Crear cita guarda en DB y en Google Calendar."""
    client_id = uuid4()
    contact_id = uuid4()
    mock_calendar = AsyncMock()
    mock_calendar.create_event.return_value = MagicMock(
        event_id="google_event_123",
        summary="Consulta - Juan",
        start=datetime(2024, 1, 15, 10, 0),
        end=datetime(2024, 1, 15, 11, 0),
        html_link="https://calendar.google.com/event/123",
    )

    with patch("app.agents.tools.calendar_tools.GoogleCalendarService.from_tenant", return_value=mock_calendar):
        # Verificar que:
        # 1. Se llama a calendar.create_event
        # 2. Se crea un Appointment en la DB
        # 3. El google_event_id se guarda en el appointment
        mock_calendar.create_event.assert_called_once  # placeholder


@pytest.mark.asyncio
async def test_cancel_appointment_updates_calendar():
    """Cancelar cita actualiza Calendar y DB."""
    # 1. Crear appointment mock en DB
    # 2. Llamar cancel_appointment
    # 3. Verificar que calendar.cancel_event fue llamado
    # 4. Verificar que appointment.status = 'cancelled'
    # 5. Verificar que cancelled_reason se guardo
    pass


@pytest.mark.asyncio
async def test_scheduling_node_routes_to_tools():
    """Nodo scheduling usa GPT-4o con tools de calendario."""
    # 1. Mock del LLM con tool_calls
    # 2. Invocar scheduling_node con state de intent "scheduling"
    # 3. Verificar que la tool correcta fue invocada
    # 4. Verificar que response_text contiene resultado de la tool
    pass


@pytest.mark.asyncio
async def test_scheduling_node_error_triggers_handoff():
    """Error en scheduling envia a handoff."""
    from app.agents.nodes.scheduling import scheduling_node

    state = {
        "client_id": str(uuid4()),
        "conversation_id": str(uuid4()),
        "contact_id": str(uuid4()),
        "channel": "whatsapp",
        "message": {"text": "quiero una cita"},
        "model_to_use": "gpt-4o",
    }

    with patch("app.agents.nodes.scheduling.GoogleCalendarService.from_tenant", side_effect=Exception("API error")):
        result = await scheduling_node(state)
        assert result["requires_handoff"] == True
        assert result["handoff_reason"] == "scheduling_error"
```

#### `tests/unit/test_contact_unifier.py`

```python
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_resolve_existing_contact():
    """Contacto existente se resuelve correctamente."""
    # 1. Insertar contacto con identifier (WhatsApp, +5215512345678)
    # 2. Llamar resolve_contact con el mismo channel e identifier
    # 3. Verificar que retorna el contacto existente
    # 4. Verificar is_new == False
    pass


@pytest.mark.asyncio
async def test_resolve_creates_new_contact():
    """Identifier desconocido crea contacto nuevo."""
    # 1. Llamar resolve_contact con identifier que no existe
    # 2. Verificar que se crea un nuevo Contact
    # 3. Verificar que se crea un ContactIdentifier
    # 4. Verificar is_new == True
    pass


@pytest.mark.asyncio
async def test_cross_channel_same_phone():
    """Mismo telefono en WhatsApp y Telegram: 1 contacto, 2 identifiers."""
    # 1. resolve_contact(channel="whatsapp", identifier="+5215512345678")
    #    -> Crea contacto nuevo con 1 identifier
    # 2. Agregar manualmente un segundo identifier con channel="telegram" al mismo contacto
    # 3. resolve_contact(channel="telegram", identifier="+5215512345678")
    #    -> Debe retornar el MISMO contacto
    # 4. Verificar que el contacto tiene 2 identifiers
    pass


@pytest.mark.asyncio
async def test_merge_moves_all_data():
    """Merge mueve identifiers, conversations, notes y tags."""
    # 1. Crear contacto A con: 1 identifier, 2 conversations, 1 note, 2 tags
    # 2. Crear contacto B con: 1 identifier, 1 conversation, 1 note, 1 tag (diferente)
    # 3. Merge A into B
    # 4. Verificar que B ahora tiene:
    #    - 2 identifiers (1 original + 1 de A)
    #    - 3 conversations (1 original + 2 de A)
    #    - 2 notes (1 original + 1 de A)
    #    - 3 tags (1 original + 2 de A, sin duplicados)
    # 5. Verificar que A tiene merged_into_id = B.id
    pass


@pytest.mark.asyncio
async def test_merge_deduplicates_tags():
    """Merge no duplica tags compartidos."""
    # 1. Crear tag "VIP"
    # 2. Asignar "VIP" a contacto A y B
    # 3. Merge A into B
    # 4. Verificar que B tiene "VIP" una sola vez
    pass


@pytest.mark.asyncio
async def test_follow_merge_chain():
    """Cadena de merges se resuelve hasta el contacto final."""
    # 1. Crear contactos A, B, C
    # 2. Merge A into B
    # 3. Merge B into C
    # 4. Resolver contacto A
    # 5. Verificar que retorna contacto C
    pass


@pytest.mark.asyncio
async def test_merge_chain_max_depth():
    """Cadena de merge tiene limite de profundidad."""
    # Prevenir ciclos infinitos con max_depth=10
    pass
```

#### `tests/unit/test_conversation_lifecycle.py`

```python
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock
from app.services.conversation_lifecycle import ConversationLifecycle, VALID_TRANSITIONS


def test_valid_transitions_map():
    """Mapa de transiciones tiene todos los estados."""
    expected_states = {"new", "bot_active", "human_active", "waiting_human", "waiting_client", "resolved", "archived"}
    assert set(VALID_TRANSITIONS.keys()) == expected_states


def test_archived_is_terminal():
    """archived no tiene transiciones validas."""
    assert VALID_TRANSITIONS["archived"] == []


@pytest.mark.parametrize("current,target,valid", [
    ("new", "bot_active", True),
    ("new", "human_active", True),
    ("new", "resolved", False),
    ("bot_active", "human_active", True),
    ("bot_active", "waiting_human", True),
    ("bot_active", "resolved", True),
    ("bot_active", "archived", False),
    ("human_active", "waiting_client", True),
    ("human_active", "resolved", True),
    ("human_active", "bot_active", False),
    ("waiting_human", "human_active", True),
    ("waiting_human", "bot_active", False),
    ("waiting_client", "human_active", True),
    ("waiting_client", "bot_active", True),
    ("waiting_client", "resolved", True),
    ("resolved", "archived", True),
    ("resolved", "bot_active", False),
    ("archived", "resolved", False),
    ("archived", "new", False),
])
def test_validate_transition(current, target, valid):
    """Validar transiciones permitidas y rechazadas."""
    assert ConversationLifecycle.validate_transition(current, target) == valid


@pytest.mark.asyncio
async def test_transition_sets_resolved_at():
    """Transicion a 'resolved' establece resolved_at."""
    conversation = MagicMock()
    conversation.status = "human_active"
    conversation.resolved_at = None

    session = AsyncMock()
    lifecycle = ConversationLifecycle(session)
    result = await lifecycle.transition(conversation, "resolved")

    assert result.resolved_at is not None


@pytest.mark.asyncio
async def test_transition_sets_assigned_user():
    """Transicion a 'human_active' asigna el agente."""
    conversation = MagicMock()
    conversation.status = "new"
    user_id = uuid4()

    session = AsyncMock()
    lifecycle = ConversationLifecycle(session)
    result = await lifecycle.transition(conversation, "human_active", user_id=user_id)

    assert result.assigned_user_id == user_id


@pytest.mark.asyncio
async def test_invalid_transition_raises():
    """Transicion invalida lanza HTTPException 400."""
    from fastapi import HTTPException

    conversation = MagicMock()
    conversation.status = "archived"

    session = AsyncMock()
    lifecycle = ConversationLifecycle(session)

    with pytest.raises(HTTPException) as exc_info:
        await lifecycle.transition(conversation, "new")
    assert exc_info.value.status_code == 400


def test_get_valid_transitions():
    """get_valid_transitions retorna las opciones correctas."""
    transitions = ConversationLifecycle.get_valid_transitions("bot_active")
    assert "human_active" in transitions
    assert "waiting_human" in transitions
    assert "resolved" in transitions
    assert "archived" not in transitions
```

#### `tests/integration/test_crm_api.py`

```python
import pytest
from httpx import AsyncClient
from uuid import uuid4


@pytest.fixture
async def auth_headers(test_client):
    """Headers con JWT de un usuario de test."""
    # Login y obtener token
    ...


@pytest.mark.asyncio
async def test_create_and_list_contacts(test_client, auth_headers):
    """Crear contacto y verificar que aparece en listado."""
    # 1. POST /api/v1/contacts con datos validos
    # 2. GET /api/v1/contacts
    # 3. Verificar que el contacto creado aparece
    # 4. Verificar paginacion (total, page, page_size)
    pass


@pytest.mark.asyncio
async def test_contact_search(test_client, auth_headers):
    """Busqueda de contactos por nombre."""
    # 1. Crear contactos "Juan Perez", "Maria Lopez", "Juan Garcia"
    # 2. GET /api/v1/contacts?search=Juan
    # 3. Verificar que retorna 2 resultados
    pass


@pytest.mark.asyncio
async def test_contact_filter_by_tag(test_client, auth_headers):
    """Filtrar contactos por tag."""
    # 1. Crear tag "VIP"
    # 2. Crear 3 contactos, asignar tag "VIP" a 2
    # 3. GET /api/v1/contacts?tag_id=<vip_id>
    # 4. Verificar que retorna 2 resultados
    pass


@pytest.mark.asyncio
async def test_contact_detail_includes_relations(test_client, auth_headers):
    """Detalle de contacto incluye identifiers, tags y notas."""
    # 1. Crear contacto con 2 identifiers, 1 tag, 3 notas
    # 2. GET /api/v1/contacts/{id}
    # 3. Verificar que identifiers, tags y notas estan presentes
    pass


@pytest.mark.asyncio
async def test_merge_contacts(test_client, auth_headers):
    """Merge de contactos via API."""
    # 1. Crear contacto A con datos
    # 2. Crear contacto B con datos
    # 3. POST /api/v1/contacts/{A}/merge/{B}
    # 4. Verificar que A.merged_into_id = B
    # 5. Verificar que datos de A se movieron a B
    pass


@pytest.mark.asyncio
async def test_merge_requires_admin(test_client):
    """Solo admin/super_admin puede hacer merge."""
    # 1. Autenticarse como agent (no admin)
    # 2. POST /api/v1/contacts/{A}/merge/{B}
    # 3. Verificar 403 Forbidden
    pass


@pytest.mark.asyncio
async def test_conversation_lifecycle_full(test_client, auth_headers):
    """Ciclo de vida completo: new -> bot_active -> human_active -> resolved -> archived."""
    # 1. Crear conversacion (status: new)
    # 2. PUT /status {status: "bot_active"} -> 200
    # 3. PUT /status {status: "human_active"} -> 200
    # 4. PUT /status {status: "resolved"} -> 200
    # 5. Verificar resolved_at no es null
    # 6. PUT /status {status: "archived"} -> 200
    pass


@pytest.mark.asyncio
async def test_conversation_invalid_transition(test_client, auth_headers):
    """Transicion invalida retorna 400."""
    # 1. Crear conversacion (status: new)
    # 2. PUT /status {status: "archived"} -> 400
    # 3. Verificar mensaje de error con transiciones validas
    pass


@pytest.mark.asyncio
async def test_conversation_assign(test_client, auth_headers):
    """Asignar conversacion a agente."""
    # 1. Crear conversacion
    # 2. PUT /assign {user_id: agent_uuid}
    # 3. Verificar assigned_user_id y status == human_active
    pass


@pytest.mark.asyncio
async def test_conversation_messages_paginated(test_client, auth_headers):
    """Mensajes de conversacion se retornan paginados."""
    # 1. Crear conversacion con 50 mensajes
    # 2. GET /conversations/{id}?page=1&page_size=10
    # 3. Verificar 10 mensajes, total=50, orden cronologico
    pass


@pytest.mark.asyncio
async def test_tags_crud(test_client, auth_headers):
    """CRUD completo de tags."""
    # 1. POST /tags {name: "VIP", color: "#FF0000"} -> 201
    # 2. GET /tags -> lista incluye "VIP"
    # 3. POST /tags/contacts/{id}/tags/{tag_id} -> 201
    # 4. DELETE /tags/contacts/{id}/tags/{tag_id} -> 204
    # 5. DELETE /tags/{id} -> 204
    pass


@pytest.mark.asyncio
async def test_notes_crud(test_client, auth_headers):
    """CRUD de notas internas."""
    # 1. POST /contacts/{id}/notes {content: "Nota de prueba"} -> 201
    # 2. GET /contacts/{id}/notes -> lista con la nota
    # 3. Verificar que user_id del creador esta presente
    pass


@pytest.mark.asyncio
async def test_rls_isolation(test_client):
    """Tenant A no puede ver contactos de Tenant B."""
    # 1. Crear contacto como Tenant A
    # 2. Autenticarse como Tenant B
    # 3. GET /api/v1/contacts -> 0 resultados
    # 4. GET /api/v1/contacts/{id_de_A} -> 404
    pass
```

#### `tests/integration/test_scheduling_flow.py`

```python
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_full_scheduling_flow():
    """
    Flujo completo: mensaje 'quiero una cita' -> intent routing ->
    scheduling agent -> cita creada en Calendar.
    """
    # 1. Configurar tenant con scheduling agent habilitado
    # 2. Crear service_type "Consulta general" (60 min)
    # 3. Mock de Google Calendar API
    # 4. Enviar mensaje "Quiero agendar una cita para manana a las 10"
    # 5. Verificar que intent = "scheduling"
    # 6. Verificar que check_availability fue llamado
    # 7. Verificar que create_appointment fue llamado
    # 8. Verificar que el Appointment se guardo en la DB
    # 9. Verificar que response_text confirma la cita
    pass


@pytest.mark.asyncio
async def test_scheduling_disabled_falls_to_rag():
    """
    Si scheduling agent no esta habilitado, el intent no enruta a scheduling.
    """
    # 1. Configurar tenant SIN scheduling agent
    # 2. Enviar mensaje "Quiero una cita"
    # 3. Verificar que NO se enruta a scheduling
    # 4. Verificar que se enruta a rag_query o human_handoff
    pass


@pytest.mark.asyncio
async def test_auto_close_waiting_conversations():
    """Conversaciones en waiting_client > 24h se auto-cierran."""
    # 1. Crear conversacion en status waiting_client con updated_at = hace 25 horas
    # 2. Ejecutar auto_close_conversations()
    # 3. Verificar que status = 'resolved'
    # 4. Verificar que resolved_at esta establecido
    pass


@pytest.mark.asyncio
async def test_auto_archive_resolved_conversations():
    """Conversaciones resolved > 7 dias se auto-archivan."""
    # 1. Crear conversacion en status resolved con resolved_at = hace 8 dias
    # 2. Ejecutar auto_close_conversations()
    # 3. Verificar que status = 'archived'
    pass


@pytest.mark.asyncio
async def test_auto_close_doesnt_affect_active():
    """Conversaciones activas no se auto-cierran."""
    # 1. Crear conversaciones en status bot_active, human_active con updated_at = hace 48 horas
    # 2. Ejecutar auto_close_conversations()
    # 3. Verificar que ninguna cambio de status
    pass
```

## Criterios de Aceptacion

- [ ] Tabla `service_types` y `appointments` creadas con migracion, RLS activo y FORCE habilitado
- [ ] Mensaje "quiero una cita" es detectado como intent "scheduling" y enrutado al scheduling agent
- [ ] Scheduling agent usa GPT-4o con function calling para invocar las 5 calendar tools
- [ ] `check_availability` consulta Google Calendar y retorna slots disponibles
- [ ] `create_appointment` crea evento en Calendar Y guarda Appointment en la DB
- [ ] `modify_appointment` actualiza evento en Calendar Y actualiza Appointment en la DB
- [ ] `cancel_appointment` cancela evento en Calendar Y marca status "cancelled" en la DB
- [ ] Dialogo multi-turno funciona gracias al checkpointing de LangGraph
- [ ] Contacto con mismo telefono en 2 canales resulta en 1 contacto unificado con 2 identifiers
- [ ] Merge manual de contactos mueve todos los identifiers, conversations, notes y tags
- [ ] Merge evita duplicar tags compartidos
- [ ] Cadena de merges se resuelve hasta el contacto final (max_depth=10)
- [ ] Ciclo de vida de conversacion respeta las transiciones validas de la maquina de estados
- [ ] Transicion invalida retorna HTTP 400 con mensaje descriptivo
- [ ] Auto-cierre: waiting_client > 24h se resuelven cada 15 minutos
- [ ] Auto-cierre: resolved > 7 dias se archivan cada 15 minutos
- [ ] CRUD de contactos con paginacion, busqueda por nombre y filtro por tag
- [ ] CRUD de conversaciones con filtros por status, channel y assigned_user
- [ ] CRUD de tags: crear, listar, eliminar, asignar/remover a contactos
- [ ] CRUD de notas internas: crear, listar por contacto
- [ ] RBAC: merge de contactos solo para admin/super_admin
- [ ] RBAC: asignacion de conversacion solo para admin/supervisor/super_admin
- [ ] RLS: tenant A no puede ver datos de tenant B en ningun endpoint

## Notas Tecnicas

### Google Calendar API: Service Account vs OAuth2 Consent Screen
- **Service Account** (recomendado para produccion): No requiere interaccion del usuario. Crear en Google Cloud Console, compartir el calendario con el service account email.
- **OAuth2 Consent Screen**: Requiere que el tenant autorice via browser. Util para calendarios personales.
- En ambos casos, el refresh token (o la service account key) se almacena cifrado con `pgp_sym_encrypt` en `agent_configs.settings`.

### Tools del scheduling agent y LangGraph
Las tools se registran con el decorador `@tool` de LangChain y se pasan al LLM via `bind_tools()`. GPT-4o decide cual tool invocar basandose en el mensaje del usuario. El nodo scheduling ejecuta las tools y genera la respuesta final.

**CRITICO**: Las tools reciben `client_id` via `config["configurable"]`, no como parametro del usuario. Esto previene que un usuario manipule el tenant ID.

### Cifrado de identifier_value
`contact_identifiers.identifier_value` esta cifrado con pgcrypto (`pgp_sym_encrypt`). La busqueda en `ContactUnifier.resolve_contact()` requiere descifrar con `pgp_sym_decrypt` para comparar. Esto tiene impacto en rendimiento en tablas grandes. Considerar agregar un hash (SHA-256) del identifier como columna adicional para busquedas rapidas sin descifrar.

### Auto-cierre y RLS
La tarea `auto_close_conversations` necesita operar cross-tenant. La opcion recomendada es crear un rol PostgreSQL con `BYPASSRLS` para tareas de mantenimiento. Documentado en la seccion de la tarea con ambas alternativas.

### Tabla service_types como migracion
La tabla `service_types` no estaba en el schema original del Sprint 1. Se agrega como migracion separada (`002_service_types.sql`). Esto es intencional: el schema del Sprint 1 contenia las 18 tablas MVP, y `service_types` es una adicion del Sprint 7.

### Orden de implementacion sugerido
1. Migracion SQL y modelo SQLAlchemy
2. Conversation lifecycle (maquina de estados)
3. Contact unifier
4. CRUD endpoints (contacts, conversations, tags, notes)
5. Google Calendar service
6. Calendar tools
7. Scheduling node + integracion en el grafo
8. Auto-cierre con Celery Beat
9. Tests

## Dependencias para Sprint 8
- API del CRM funcional (contacts, conversations, tags, notes)
- Scheduling agent operativo con Google Calendar
- Conversaciones con ciclo de vida completo y transiciones validadas
- Auto-cierre de conversaciones inactivas activo
- Contact unifier resolviendo contactos cross-canal
