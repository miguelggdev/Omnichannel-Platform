"""Tests de `app/agents/tools/calendar_tools.py` — sin red, sin Google real.

`GoogleCalendarService` se sustituye por un doble en cada test: lo que se prueba
aquí es la lógica de las tools (búsqueda de service_type, parseo de fechas,
persistencia del `Appointment`), no la integración real con Calendar (eso vive
en `tests/unit/test_calendar_service.py`).
"""

import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.agents.tools import calendar_tools as modulo
from app.services.calendar import CalendarEvent, CalendarSlot
from tests.unit.agent_doubles import FakeSession, parchear_tenant_session

# ─── Dobles ──────────────────────────────────────────────────────────────────


class _FakeServiceType:
    """Sustituto mínimo de `ServiceType`."""

    def __init__(
        self,
        id_: Any = None,
        name: str = "Consulta",
        duration_minutes: int = 60,
        buffer_minutes: int = 15,
    ) -> None:
        self.id = id_ or uuid.uuid4()
        self.name = name
        self.duration_minutes = duration_minutes
        self.buffer_minutes = buffer_minutes


class _FakeContact:
    """Sustituto mínimo de `Contact`."""

    def __init__(
        self,
        display_name: str | None = "Ana Pérez",
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> None:
        self.display_name = display_name
        self.first_name = first_name
        self.last_name = last_name


class _FakeAppointment:
    """Sustituto mínimo de `Appointment`, mutable como el real."""

    def __init__(
        self,
        id_: Any = None,
        status: str = "confirmed",
        service_type_id: Any = None,
        google_event_id: str | None = "evt-1",
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        title: str = "Consulta - Ana Pérez",
    ) -> None:
        self.id = id_ or uuid.uuid4()
        self.status = status
        self.service_type_id = service_type_id
        self.google_event_id = google_event_id
        self.starts_at = starts_at
        self.ends_at = ends_at
        self.title = title
        self.cancelled_reason: str | None = None


class _FakeCalendarService:
    """Sustituto de `GoogleCalendarService`."""

    def __init__(self, timezone: str = "America/Bogota", conflicto: bool = False) -> None:
        self.timezone = timezone
        self.slots: list[CalendarSlot] = []
        self.conflicto = conflicto
        self.created: dict[str, Any] | None = None
        self.modified: dict[str, Any] | None = None
        self.cancelled_event_id: str | None = None

    async def check_availability(self, **kwargs: Any) -> list[CalendarSlot]:
        return self.slots

    async def has_conflict(self, start: Any, end: Any) -> bool:
        return self.conflicto

    async def create_event(self, **kwargs: Any) -> CalendarEvent:
        self.created = kwargs
        return CalendarEvent(
            event_id="evt-nuevo",
            summary=kwargs["summary"],
            start=kwargs["start"],
            end=kwargs["end"],
        )

    async def modify_event(self, **kwargs: Any) -> CalendarEvent:
        self.modified = kwargs
        return CalendarEvent(
            event_id=kwargs["event_id"],
            summary="x",
            start=kwargs["new_start"],
            end=kwargs["new_end"],
        )

    async def cancel_event(self, event_id: str) -> bool:
        self.cancelled_event_id = event_id
        return True


def _config(
    client_id: uuid.UUID,
    conversation_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Arma el `RunnableConfig` con `client_id`/`contact_id` inyectados, como haría el nodo real."""
    configurable: dict[str, Any] = {
        "client_id": str(client_id),
        "contact_id": str(contact_id or uuid.uuid4()),
    }
    if conversation_id is not None:
        configurable["conversation_id"] = str(conversation_id)
    return {"configurable": configurable}


def _parchear_calendar(monkeypatch: pytest.MonkeyPatch, fake: _FakeCalendarService) -> None:
    async def _from_tenant(client_id: uuid.UUID) -> _FakeCalendarService:
        return fake

    monkeypatch.setattr(modulo.GoogleCalendarService, "from_tenant", _from_tenant)


# ─── Contrato de seguridad: client_id nunca en el schema del LLM ────────────


class TestContratoDeSeguridad:
    """El LLM nunca debe poder rellenar `client_id`/`contact_id`: vienen solo del config."""

    @pytest.mark.parametrize("herramienta", modulo.SCHEDULING_TOOLS, ids=lambda t: t.name)
    def test_config_no_aparece_en_el_schema_del_llm(self, herramienta: Any) -> None:
        """`config` (y por lo tanto `client_id`/`contact_id`) no debe estar en `.args`."""
        assert "config" not in herramienta.args
        assert "client_id" not in herramienta.args
        assert "contact_id" not in herramienta.args


# ─── check_availability ──────────────────────────────────────────────────────


class TestCheckAvailability:
    """`check_availability` busca el service_type y consulta el calendario."""

    async def test_service_type_inexistente(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin ese tipo de servicio, se le avisa al LLM con los que sí existen."""
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[None, []]))

        resultado = await modulo.check_availability.ainvoke(
            {"date": "2026-09-21", "service_type_name": "Inexistente"},
            config=_config(uuid.uuid4()),
        )

        assert "No encontré el tipo de servicio" in resultado

    async def test_fecha_invalida(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Una fecha mal formada no debe tumbar la tool con un ValueError crudo."""
        tipo = _FakeServiceType()
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[tipo]))

        resultado = await modulo.check_availability.ainvoke(
            {"date": "21-09-2026", "service_type_name": "Consulta"},
            config=_config(uuid.uuid4()),
        )

        assert "no es una fecha válida" in resultado

    async def test_sin_slots_disponibles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin huecos libres, la tool lo dice explícitamente (no una lista vacía muda)."""
        tipo = _FakeServiceType()
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[tipo]))
        _parchear_calendar(monkeypatch, _FakeCalendarService())

        resultado = await modulo.check_availability.ainvoke(
            {"date": "2026-09-21", "service_type_name": "Consulta"},
            config=_config(uuid.uuid4()),
        )

        assert "No hay horarios disponibles" in resultado

    async def test_lista_los_horarios_libres(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Con slots libres, el texto incluye el horario de cada uno."""
        tipo = _FakeServiceType(duration_minutes=30)
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[tipo]))
        fake_calendar = _FakeCalendarService()
        fake_calendar.slots = [
            CalendarSlot(start=datetime(2026, 9, 21, 9, 0), end=datetime(2026, 9, 21, 9, 30))
        ]
        _parchear_calendar(monkeypatch, fake_calendar)

        resultado = await modulo.check_availability.ainvoke(
            {"date": "2026-09-21", "service_type_name": "Consulta"},
            config=_config(uuid.uuid4()),
        )

        assert "09:00 - 09:30" in resultado


# ─── create_appointment ──────────────────────────────────────────────────────


class TestCreateAppointment:
    """`create_appointment` crea el evento en Calendar y persiste el `Appointment`."""

    async def test_service_type_inexistente(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin tipo de servicio válido, no se llega a tocar el calendario."""
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[None, []]))

        resultado = await modulo.create_appointment.ainvoke(
            {
                "datetime_iso": "2026-09-21T10:00:00",
                "service_type_name": "Inexistente",
            },
            config=_config(uuid.uuid4()),
        )

        assert "No encontré el tipo de servicio" in resultado

    async def test_contacto_inexistente(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un `contact_id` que no resuelve a nadie no debe romper con AttributeError."""
        tipo = _FakeServiceType()
        # tipo (find_service_type), None (advisory lock, valor ignorado), None (contacto)
        sesion = FakeSession(resultados=[tipo, None, None])
        parchear_tenant_session(monkeypatch, modulo, sesion)
        _parchear_calendar(monkeypatch, _FakeCalendarService())

        resultado = await modulo.create_appointment.ainvoke(
            {
                "datetime_iso": "2026-09-21T10:00:00",
                "service_type_name": "Consulta",
            },
            config=_config(uuid.uuid4()),
        )

        assert "No encontré ese contacto" in resultado

    async def test_horario_ya_no_disponible_no_crea_la_cita(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BUG-023: si `has_conflict()` dice que ya no está libre, no se llega a crear nada."""
        tipo = _FakeServiceType()
        # tipo (find_service_type), None (advisory lock, valor ignorado)
        sesion = FakeSession(resultados=[tipo, None])
        parchear_tenant_session(monkeypatch, modulo, sesion)
        fake_calendar = _FakeCalendarService(conflicto=True)
        _parchear_calendar(monkeypatch, fake_calendar)

        resultado = await modulo.create_appointment.ainvoke(
            {"datetime_iso": "2026-09-21T10:00:00", "service_type_name": "Consulta"},
            config=_config(uuid.uuid4()),
        )

        assert "ya no está disponible" in resultado
        assert fake_calendar.created is None
        assert sesion.agregados_de(modulo.Appointment) == []

    async def test_crea_el_evento_y_persiste_la_cita(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """El evento se crea con el título correcto y el `Appointment` queda agregado."""
        tipo = _FakeServiceType(name="Consulta", duration_minutes=45)
        contacto = _FakeContact(display_name="Ana Pérez")
        contact_id = uuid.uuid4()
        conversation_id = uuid.uuid4()
        # tipo (find_service_type), None (advisory lock, valor ignorado), contacto
        sesion = FakeSession(resultados=[tipo, None, contacto])
        parchear_tenant_session(monkeypatch, modulo, sesion)
        fake_calendar = _FakeCalendarService()
        _parchear_calendar(monkeypatch, fake_calendar)

        resultado = await modulo.create_appointment.ainvoke(
            {
                "datetime_iso": "2026-09-21T10:00:00",
                "service_type_name": "Consulta",
                "notes": "Primera vez",
            },
            config=_config(uuid.uuid4(), conversation_id, contact_id=contact_id),
        )

        assert "Cita creada exitosamente" in resultado
        assert fake_calendar.created["summary"] == "Consulta - Ana Pérez"
        citas = sesion.agregados_de(modulo.Appointment)
        assert len(citas) == 1
        assert citas[0].notes == "Primera vez"
        assert citas[0].conversation_id == conversation_id
        assert citas[0].contact_id == contact_id
        assert citas[0].starts_at.tzinfo is not None

    async def test_lock_se_pide_con_una_clave_por_tenant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El advisory lock se pide con `pg_advisory_xact_lock`, con una clave por tenant."""
        tipo = _FakeServiceType()
        contacto = _FakeContact()
        sesion = FakeSession(resultados=[tipo, None, contacto])
        parchear_tenant_session(monkeypatch, modulo, sesion)
        _parchear_calendar(monkeypatch, _FakeCalendarService())
        client_id = uuid.uuid4()

        await modulo.create_appointment.ainvoke(
            {"datetime_iso": "2026-09-21T10:00:00", "service_type_name": "Consulta"},
            config=_config(client_id),
        )

        # executed[0] es el SELECT de _find_service_type (misma sesion falsa
        # compartida via parchear_tenant_session); executed[1] es el advisory lock.
        lock_stmt = sesion.executed[1]
        assert "pg_advisory_xact_lock" in str(lock_stmt)
        assert lock_stmt.compile().params["clave"] == f"appointments:{client_id}"


# ─── modify_appointment ──────────────────────────────────────────────────────


class TestModifyAppointment:
    """`modify_appointment` reprograma tanto en Calendar como en la base."""

    async def test_cita_inexistente(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin la cita, se le avisa al usuario en vez de crashear."""
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[None]))

        resultado = await modulo.modify_appointment.ainvoke(
            {"appointment_id": str(uuid.uuid4()), "new_datetime_iso": "2026-09-22T11:00:00"},
            config=_config(uuid.uuid4()),
        )

        assert "No encontré esa cita" in resultado

    async def test_cita_ya_cancelada(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Una cita cancelada no se reprograma."""
        apt_id = uuid.uuid4()
        cita = _FakeAppointment(id_=apt_id, status="cancelled")
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[cita]))

        resultado = await modulo.modify_appointment.ainvoke(
            {"appointment_id": str(apt_id), "new_datetime_iso": "2026-09-22T11:00:00"},
            config=_config(uuid.uuid4()),
        )

        assert "ya fue cancelada" in resultado

    async def test_reprograma_en_calendar_y_en_la_base(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La cita queda con la nueva fecha, y Calendar recibe el mismo cambio."""
        apt_id, tipo_id = uuid.uuid4(), uuid.uuid4()
        cita = _FakeAppointment(id_=apt_id, service_type_id=tipo_id, google_event_id="evt-1")
        tipo = _FakeServiceType(id_=tipo_id, duration_minutes=30)
        # El tipo de servicio se busca con select() filtrado por client_id, no
        # con session.get(): sale de `resultados` justo despues de la cita.
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[cita, tipo]))
        fake_calendar = _FakeCalendarService()
        _parchear_calendar(monkeypatch, fake_calendar)

        resultado = await modulo.modify_appointment.ainvoke(
            {"appointment_id": str(apt_id), "new_datetime_iso": "2026-09-22T11:00:00"},
            config=_config(uuid.uuid4()),
        )

        assert "Cita reprogramada" in resultado
        assert cita.starts_at == datetime(2026, 9, 22, 11, 0, tzinfo=ZoneInfo("America/Bogota"))
        # La duracion sale del tipo de servicio (30 min), no del respaldo de 60:
        # si la busqueda del tipo se rompiera, este assert lo delata.
        assert cita.ends_at == cita.starts_at + timedelta(minutes=30)
        assert fake_calendar.modified["event_id"] == "evt-1"


# ─── cancel_appointment ──────────────────────────────────────────────────────


class TestCancelAppointment:
    """`cancel_appointment` cancela en Calendar y marca el status en la base."""

    async def test_cancela_y_registra_el_motivo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """El `Appointment` queda `cancelled` con el motivo dado."""
        apt_id = uuid.uuid4()
        cita = _FakeAppointment(id_=apt_id, google_event_id="evt-9")
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[cita]))
        fake_calendar = _FakeCalendarService()
        _parchear_calendar(monkeypatch, fake_calendar)

        resultado = await modulo.cancel_appointment.ainvoke(
            {"appointment_id": str(apt_id), "reason": "El cliente ya no puede asistir"},
            config=_config(uuid.uuid4()),
        )

        assert "Cita cancelada" in resultado
        assert cita.status == "cancelled"
        assert cita.cancelled_reason == "El cliente ya no puede asistir"
        assert fake_calendar.cancelled_event_id == "evt-9"

    async def test_cita_ya_cancelada_no_repite_el_trabajo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cancelar dos veces no debe volver a tocar la API de Calendar."""
        apt_id = uuid.uuid4()
        cita = _FakeAppointment(id_=apt_id, status="cancelled")
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[cita]))
        fake_calendar = _FakeCalendarService()
        _parchear_calendar(monkeypatch, fake_calendar)

        resultado = await modulo.cancel_appointment.ainvoke(
            {"appointment_id": str(apt_id), "reason": "otra vez"}, config=_config(uuid.uuid4())
        )

        assert "ya estaba cancelada" in resultado
        assert fake_calendar.cancelled_event_id is None


# ─── list_appointments ───────────────────────────────────────────────────────


class TestListAppointments:
    """`list_appointments` lista las citas de un contacto en un rango de fechas."""

    async def test_fechas_invalidas(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un formato de fecha incorrecto no debe romper con ValueError."""
        resultado = await modulo.list_appointments.ainvoke(
            {"date_from": "no-es-fecha", "date_to": "2026-09-22"},
            config=_config(uuid.uuid4()),
        )

        assert "YYYY-MM-DD" in resultado

    async def test_sin_citas_en_el_rango(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin resultados, se le avisa al usuario en vez de devolver una lista vacía muda."""
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[[]]))

        resultado = await modulo.list_appointments.ainvoke(
            {"date_from": "2026-09-21", "date_to": "2026-09-22"},
            config=_config(uuid.uuid4()),
        )

        assert "No hay citas programadas" in resultado

    async def test_lista_las_citas_encontradas(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cada cita aparece con fecha, título, estado e ID."""
        cita = _FakeAppointment(
            starts_at=datetime(2026, 9, 21, 10, 0), title="Consulta - Ana Pérez"
        )
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[[cita]]))

        resultado = await modulo.list_appointments.ainvoke(
            {"date_from": "2026-09-21", "date_to": "2026-09-22"},
            config=_config(uuid.uuid4()),
        )

        assert "Consulta - Ana Pérez" in resultado
        assert "confirmed" in resultado
