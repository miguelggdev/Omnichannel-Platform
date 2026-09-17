"""GoogleCalendarService — integracion con Google Calendar para el scheduling agent.

Desviacion sobre `specs/sprint-07-scheduling-crm.md` #3: el spec modela un
flujo OAuth2 con refresh token por tenant, cifrado con pgcrypto en
`agent_configs.config`. El cifrado real (`app/core/encryption.py`) es un
entregable de Sprint 8 (METHODOLOGY.md) que todavia no existe — montar un
cifrado ad-hoc aca lo duplicaria mal. `.env.example` ya preve algo mas simple
y coincide con lo que la propia spec recomienda para produccion ("Service
Account: no requiere interaccion del usuario"): un unico service account para
toda la plataforma (`GOOGLE_CALENDAR_CREDENTIALS_JSON`, JSON en base64 via
`.env`), cuyo email cada tenant comparte con su propio calendario de Google.
Lo unico que varia por tenant es su `calendar_id` y `timezone` — ninguno de
los dos sensible — guardados sin cifrar en `agent_configs.config.scheduling`,
mismo patron que `config.enabled_agents` en `app/agents/nodes/_tenant.py`.

`google-api-python-client` es sincrono: cada llamada de red se corre con
`asyncio.to_thread()` para no bloquear el loop de eventos (CLAUDE.md regla 4).
"""

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import tenant_session
from app.models.agent_config import AgentConfig

logger = logging.getLogger(__name__)

SCOPES = ("https://www.googleapis.com/auth/calendar",)

# Fallback si el tenant no configuro agent_configs.config.scheduling.timezone.
DEFAULT_TIMEZONE = "America/Bogota"
DEFAULT_BUSINESS_HOURS: tuple[int, int] = (9, 18)


class SchedulingNotConfiguredError(RuntimeError):
    """El tenant no tiene el agendamiento configurado (falta `calendar_id`)."""


class CalendarCredentialsError(RuntimeError):
    """`GOOGLE_CALENDAR_CREDENTIALS_JSON` falta o no es un service account valido."""


@dataclass(frozen=True)
class CalendarSlot:
    """Un espacio libre en el calendario, ya en el timezone del tenant."""

    start: datetime
    end: datetime

    def to_dict(self) -> dict[str, str]:
        """Serializa el slot para devolvérselo al LLM."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "display": f"{self.start:%H:%M} - {self.end:%H:%M}",
        }


@dataclass(frozen=True)
class CalendarEvent:
    """Un evento creado o leído en Google Calendar."""

    event_id: str
    summary: str
    start: datetime
    end: datetime
    html_link: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        """Serializa el evento para devolvérselo al LLM."""
        return {
            "event_id": self.event_id,
            "summary": self.summary,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "html_link": self.html_link,
        }


def _load_credentials() -> service_account.Credentials:
    """Decodifica el service account global configurado en `.env`.

    Returns:
        Credenciales listas para `googleapiclient.discovery.build()`.

    Raises:
        CalendarCredentialsError: Si la variable falta o no decodifica a un
            JSON de service account válido.
    """
    raw = get_settings().GOOGLE_CALENDAR_CREDENTIALS_JSON
    if not raw:
        raise CalendarCredentialsError("GOOGLE_CALENDAR_CREDENTIALS_JSON no está configurado")
    try:
        info = json.loads(base64.b64decode(raw))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CalendarCredentialsError(
            "GOOGLE_CALENDAR_CREDENTIALS_JSON no es un JSON válido en base64"
        ) from exc
    return service_account.Credentials.from_service_account_info(info, scopes=list(SCOPES))


def _event_from_payload(payload: dict[str, Any]) -> CalendarEvent:
    """Convierte la respuesta cruda de la API en un `CalendarEvent`.

    Args:
        payload: Diccionario devuelto por `events().insert/get/update()`.

    Returns:
        El evento parseado.
    """
    return CalendarEvent(
        event_id=payload["id"],
        summary=payload.get("summary", ""),
        start=datetime.fromisoformat(payload["start"]["dateTime"]),
        end=datetime.fromisoformat(payload["end"]["dateTime"]),
        html_link=payload.get("htmlLink"),
    )


class GoogleCalendarService:
    """Agenda citas en el calendario de Google de un tenant.

    Attributes:
        calendar_id: ID del calendario de Google del tenant.
        timezone: Zona horaria IANA del tenant (ej. "America/Bogota").
    """

    def __init__(
        self, calendar_id: str, timezone: str, credentials: service_account.Credentials
    ) -> None:
        """Guarda la configuración; el cliente HTTP se construye perezosamente.

        Args:
            calendar_id: ID del calendario de Google del tenant.
            timezone: Zona horaria IANA del tenant.
            credentials: Credenciales del service account.
        """
        self.calendar_id = calendar_id
        self.timezone = timezone
        self._credentials = credentials
        self._service: Resource | None = None

    @classmethod
    async def from_tenant(cls, client_id: UUID) -> "GoogleCalendarService":
        """Arma el servicio a partir de la configuración del tenant.

        Args:
            client_id: Tenant para el que se agenda.

        Returns:
            Servicio listo para consultar o crear eventos en su calendario.

        Raises:
            SchedulingNotConfiguredError: Si el tenant no tiene `calendar_id`
                configurado (ni en `agent_configs` ni en `GOOGLE_CALENDAR_ID`).
            CalendarCredentialsError: Si el service account global no está
                configurado.
        """
        async with tenant_session(client_id) as session:
            stmt = (
                select(AgentConfig)
                .where(AgentConfig.is_active.is_(True))
                .order_by(AgentConfig.created_at.asc())
                .limit(1)
            )
            config = (await session.execute(stmt)).scalar_one_or_none()

        scheduling: dict[str, Any] = (config.config or {}).get("scheduling", {}) if config else {}
        calendar_id = scheduling.get("calendar_id") or get_settings().GOOGLE_CALENDAR_ID
        if not calendar_id:
            raise SchedulingNotConfiguredError(
                f"Tenant {client_id} no tiene calendario configurado "
                "(agent_configs.config.scheduling.calendar_id)"
            )
        timezone = scheduling.get("timezone") or DEFAULT_TIMEZONE

        return cls(calendar_id=calendar_id, timezone=timezone, credentials=_load_credentials())

    async def _get_service(self) -> Resource:
        """Construye el cliente de la API la primera vez que se necesita.

        `build()` puede hacer I/O (resolver el documento de descubrimiento);
        se corre en un hilo aparte para no bloquear el loop de eventos.

        Returns:
            El cliente de la Calendar API v3, cacheado en la instancia.
        """
        if self._service is None:
            self._service = await asyncio.to_thread(
                build,
                "calendar",
                "v3",
                credentials=self._credentials,
                cache_discovery=False,
            )
        return self._service

    async def check_availability(
        self,
        date: datetime,
        duration_minutes: int,
        buffer_minutes: int = 15,
        business_hours: tuple[int, int] = DEFAULT_BUSINESS_HOURS,
    ) -> list[CalendarSlot]:
        """Calcula los espacios libres de un día dentro del horario comercial.

        Args:
            date: Día a consultar (solo se usan year/month/day).
            duration_minutes: Duración del servicio a agendar.
            buffer_minutes: Separación mínima con la siguiente cita.
            business_hours: Rango `(hora_inicio, hora_fin)` del horario comercial.

        Returns:
            Slots libres de al menos `duration_minutes`, en el timezone del tenant.

        Raises:
            HttpError: Si la consulta a la API de Google falla.
        """
        tz = ZoneInfo(self.timezone)
        day_start = date.replace(
            hour=business_hours[0], minute=0, second=0, microsecond=0, tzinfo=tz
        )
        day_end = date.replace(hour=business_hours[1], minute=0, second=0, microsecond=0, tzinfo=tz)

        service = await self._get_service()
        body = {
            "timeMin": day_start.isoformat(),
            "timeMax": day_end.isoformat(),
            "items": [{"id": self.calendar_id}],
            "timeZone": self.timezone,
        }
        try:
            freebusy = await asyncio.to_thread(
                lambda: service.freebusy().query(body=body).execute()
            )
        except HttpError:
            logger.exception(
                "Error consultando disponibilidad en Calendar (calendar_id=%s)", self.calendar_id
            )
            raise

        busy = sorted(
            (
                (datetime.fromisoformat(periodo["start"]), datetime.fromisoformat(periodo["end"]))
                for periodo in freebusy["calendars"][self.calendar_id]["busy"]
            ),
            key=lambda periodo: periodo[0],
        )

        slot_duration = timedelta(minutes=duration_minutes)
        buffer_delta = timedelta(minutes=buffer_minutes)
        slots: list[CalendarSlot] = []
        current = day_start
        for busy_start, busy_end in busy:
            while current + slot_duration <= busy_start:
                slots.append(CalendarSlot(start=current, end=current + slot_duration))
                current += slot_duration + buffer_delta
            current = max(current, busy_end + buffer_delta)

        while current + slot_duration <= day_end:
            slots.append(CalendarSlot(start=current, end=current + slot_duration))
            current += slot_duration + buffer_delta

        return slots

    async def has_conflict(self, start: datetime, end: datetime) -> bool:
        """Verifica si `[start, end)` se superpone con algún evento ya ocupado.

        A diferencia de `check_availability()` (que calcula todos los huecos
        libres de un día), esto sólo pregunta por el rango exacto que se está
        por reservar. `create_appointment` (`app/agents/tools/calendar_tools.py`)
        lo usa justo antes de crear el evento, para no confiar en una
        disponibilidad consultada turnos de conversación atrás — entre que el
        LLM llamó `check_availability` y el usuario confirmó, ese horario pudo
        haberse ocupado (BUG-023, ver MEMORY.md).

        Args:
            start: Inicio del rango a verificar.
            end: Fin del rango a verificar.

        Returns:
            `True` si hay al menos un evento ocupado que se superpone.

        Raises:
            HttpError: Si la consulta a la API de Google falla.
        """
        service = await self._get_service()
        body = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "items": [{"id": self.calendar_id}],
            "timeZone": self.timezone,
        }
        try:
            freebusy = await asyncio.to_thread(
                lambda: service.freebusy().query(body=body).execute()
            )
        except HttpError:
            logger.exception(
                "Error verificando conflictos en Calendar (calendar_id=%s)", self.calendar_id
            )
            raise

        # timeMin/timeMax ya acotan la consulta al rango exacto: cualquier
        # periodo ocupado devuelto necesariamente se superpone con el.
        return len(freebusy["calendars"][self.calendar_id]["busy"]) > 0

    async def create_event(
        self,
        summary: str,
        start: datetime,
        end: datetime,
        description: str | None = None,
        attendee_email: str | None = None,
    ) -> CalendarEvent:
        """Crea un evento en el calendario del tenant.

        Args:
            summary: Título del evento.
            start: Inicio de la cita.
            end: Fin de la cita.
            description: Descripción opcional.
            attendee_email: Email del contacto a invitar, si se conoce.

        Returns:
            El evento creado, con el `event_id` para modificarlo o cancelarlo.

        Raises:
            HttpError: Si la creación falla en la API de Google.
        """
        body: dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": start.isoformat(), "timeZone": self.timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": self.timezone},
        }
        if description:
            body["description"] = description
        if attendee_email:
            body["attendees"] = [{"email": attendee_email}]

        service = await self._get_service()
        try:
            event = await asyncio.to_thread(
                lambda: (
                    service.events()
                    .insert(
                        calendarId=self.calendar_id,
                        body=body,
                        sendUpdates="all" if attendee_email else "none",
                    )
                    .execute()
                )
            )
        except HttpError:
            logger.exception("Error creando evento en Calendar (calendar_id=%s)", self.calendar_id)
            raise

        return _event_from_payload(event)

    async def modify_event(
        self,
        event_id: str,
        new_start: datetime | None = None,
        new_end: datetime | None = None,
        new_summary: str | None = None,
    ) -> CalendarEvent:
        """Modifica los campos dados de un evento existente.

        Args:
            event_id: ID del evento a modificar.
            new_start: Nuevo inicio, si cambia.
            new_end: Nuevo fin, si cambia.
            new_summary: Nuevo título, si cambia.

        Returns:
            El evento ya actualizado.

        Raises:
            HttpError: Si la modificación falla en la API de Google.
        """
        service = await self._get_service()
        try:
            event = await asyncio.to_thread(
                lambda: (
                    service.events().get(calendarId=self.calendar_id, eventId=event_id).execute()
                )
            )
            if new_start is not None:
                event["start"]["dateTime"] = new_start.isoformat()
            if new_end is not None:
                event["end"]["dateTime"] = new_end.isoformat()
            if new_summary is not None:
                event["summary"] = new_summary

            updated = await asyncio.to_thread(
                lambda: (
                    service.events()
                    .update(
                        calendarId=self.calendar_id, eventId=event_id, body=event, sendUpdates="all"
                    )
                    .execute()
                )
            )
        except HttpError:
            logger.exception("Error modificando el evento %s en Calendar", event_id)
            raise

        return _event_from_payload(updated)

    async def cancel_event(self, event_id: str) -> bool:
        """Cancela (borra) un evento del calendario.

        Args:
            event_id: ID del evento a cancelar.

        Returns:
            `True` si se canceló.

        Raises:
            HttpError: Si la cancelación falla en la API de Google.
        """
        service = await self._get_service()
        try:
            await asyncio.to_thread(
                lambda: (
                    service.events()
                    .delete(calendarId=self.calendar_id, eventId=event_id, sendUpdates="all")
                    .execute()
                )
            )
        except HttpError:
            logger.exception("Error cancelando el evento %s en Calendar", event_id)
            raise
        return True

    async def list_events(
        self, time_min: datetime, time_max: datetime, max_results: int = 50
    ) -> list[CalendarEvent]:
        """Lista eventos del calendario del tenant en un rango de tiempo.

        Args:
            time_min: Inicio del rango.
            time_max: Fin del rango.
            max_results: Máximo de eventos a devolver.

        Returns:
            Eventos ordenados por inicio.

        Raises:
            HttpError: Si la consulta falla en la API de Google.
        """
        service = await self._get_service()
        try:
            result = await asyncio.to_thread(
                lambda: (
                    service.events()
                    .list(
                        calendarId=self.calendar_id,
                        timeMin=time_min.isoformat(),
                        timeMax=time_max.isoformat(),
                        maxResults=max_results,
                        singleEvents=True,
                        orderBy="startTime",
                        timeZone=self.timezone,
                    )
                    .execute()
                )
            )
        except HttpError:
            logger.exception(
                "Error listando eventos en Calendar (calendar_id=%s)", self.calendar_id
            )
            raise

        return [_event_from_payload(item) for item in result.get("items", [])]
