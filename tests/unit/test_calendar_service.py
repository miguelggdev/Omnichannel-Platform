"""Tests de `app/services/calendar.py` — sin red, sin Google real.

`googleapiclient`/`google.oauth2` se sustituyen por dobles: ningun test de este
archivo abre una conexion real a la API de Google Calendar.
"""

import base64
import json
from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.services import calendar as modulo
from app.services.calendar import (
    CalendarCredentialsError,
    GoogleCalendarService,
    SchedulingNotConfiguredError,
    _load_credentials,
)
from tests.unit.agent_doubles import FakeSession, parchear_tenant_session

# ─── Dobles ──────────────────────────────────────────────────────────────────


class _FakeAgentConfig:
    """Sustituto mínimo de `AgentConfig` con lo que lee `from_tenant()`."""

    def __init__(self, config: dict[str, Any] | None) -> None:
        """Guarda el `config` JSONB que devolverá la consulta."""
        self.config = config


class _FakeSettings:
    """Sustituto de `Settings` con solo los campos que usa `calendar.py`."""

    def __init__(self, credentials_json: str = "", calendar_id: str = "primary") -> None:
        """Guarda los dos campos que consulta este módulo."""
        self.GOOGLE_CALENDAR_CREDENTIALS_JSON = credentials_json
        self.GOOGLE_CALENDAR_ID = calendar_id


class _FakeCredentials:
    """Sustituto de `service_account.Credentials`."""


def _service_account_json(client_email: str = "bot@proyecto.iam.gserviceaccount.com") -> str:
    """Arma un JSON de service account de juguete, codificado en base64."""
    payload = {"type": "service_account", "client_email": client_email, "private_key": "x"}
    return base64.b64encode(json.dumps(payload).encode()).decode()


class _Ejecutable:
    """Simula el `.execute()` final de cualquier llamada de la API."""

    def __init__(self, respuesta: Any) -> None:
        self._respuesta = respuesta

    def execute(self) -> Any:
        """Devuelve la respuesta preparada."""
        return self._respuesta


class _FakeEventsResource:
    """Doble de `service.events()`."""

    def __init__(self) -> None:
        self.llamadas: list[tuple[str, dict[str, Any]]] = []
        self.get_respuesta: dict[str, Any] = {}
        self.insert_respuesta: dict[str, Any] = {}
        self.update_respuesta: dict[str, Any] = {}
        self.list_respuesta: dict[str, Any] = {"items": []}

    def insert(self, **kwargs: Any) -> _Ejecutable:
        """Registra la llamada y devuelve la respuesta preparada de `insert`."""
        self.llamadas.append(("insert", kwargs))
        return _Ejecutable(self.insert_respuesta)

    def get(self, **kwargs: Any) -> _Ejecutable:
        """Registra la llamada y devuelve la respuesta preparada de `get`."""
        self.llamadas.append(("get", kwargs))
        return _Ejecutable(self.get_respuesta)

    def update(self, **kwargs: Any) -> _Ejecutable:
        """Registra la llamada y devuelve la respuesta preparada de `update`."""
        self.llamadas.append(("update", kwargs))
        return _Ejecutable(self.update_respuesta)

    def delete(self, **kwargs: Any) -> _Ejecutable:
        """Registra la llamada y devuelve una respuesta vacía."""
        self.llamadas.append(("delete", kwargs))
        return _Ejecutable(None)

    def list(self, **kwargs: Any) -> _Ejecutable:
        """Registra la llamada y devuelve la respuesta preparada de `list`."""
        self.llamadas.append(("list", kwargs))
        return _Ejecutable(self.list_respuesta)


class _FakeFreebusyResource:
    """Doble de `service.freebusy()`."""

    def __init__(self, respuesta: dict[str, Any]) -> None:
        self._respuesta = respuesta
        self.body_recibido: dict[str, Any] | None = None

    def query(self, body: dict[str, Any]) -> _Ejecutable:
        """Registra el body y devuelve la respuesta preparada."""
        self.body_recibido = body
        return _Ejecutable(self._respuesta)


class _FakeGoogleService:
    """Doble del objeto que devuelve `googleapiclient.discovery.build()`."""

    def __init__(self, freebusy_respuesta: dict[str, Any] | None = None) -> None:
        self._events = _FakeEventsResource()
        self._freebusy = _FakeFreebusyResource(freebusy_respuesta or {"calendars": {}})

    def events(self) -> _FakeEventsResource:
        """Devuelve el doble de `events()`."""
        return self._events

    def freebusy(self) -> _FakeFreebusyResource:
        """Devuelve el doble de `freebusy()`."""
        return self._freebusy


def _servicio(
    fake_service: _FakeGoogleService, calendar_id: str = "cal-1"
) -> GoogleCalendarService:
    """Arma un `GoogleCalendarService` con el cliente HTTP ya inyectado (sin `build()`)."""
    servicio = GoogleCalendarService(
        calendar_id=calendar_id, timezone="America/Bogota", credentials=_FakeCredentials()
    )
    servicio._service = fake_service  # evita pasar por asyncio.to_thread(build, ...)
    return servicio


# ─── _load_credentials ───────────────────────────────────────────────────────


class TestLoadCredentials:
    """`_load_credentials()` decodifica el service account de `.env`."""

    def test_sin_variable_configurada_falla(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin `GOOGLE_CALENDAR_CREDENTIALS_JSON` no hay con qué autenticar."""
        monkeypatch.setattr(modulo, "get_settings", lambda: _FakeSettings(credentials_json=""))

        with pytest.raises(CalendarCredentialsError):
            _load_credentials()

    def test_base64_invalido_falla(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un valor que no decodifica a JSON válido se reporta como error claro."""
        monkeypatch.setattr(
            modulo, "get_settings", lambda: _FakeSettings(credentials_json="no-es-base64-valido!!")
        )

        with pytest.raises(CalendarCredentialsError):
            _load_credentials()

    def test_service_account_valido_se_decodifica(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un JSON de service account válido llega intacto a `from_service_account_info`."""
        monkeypatch.setattr(
            modulo,
            "get_settings",
            lambda: _FakeSettings(credentials_json=_service_account_json("x@y.com")),
        )
        capturado: dict[str, Any] = {}

        def _from_info(info: dict[str, Any], scopes: list[str]) -> _FakeCredentials:
            capturado["info"] = info
            capturado["scopes"] = scopes
            return _FakeCredentials()

        monkeypatch.setattr(
            modulo.service_account.Credentials,
            "from_service_account_info",
            staticmethod(_from_info),
        )

        _load_credentials()

        assert capturado["info"]["client_email"] == "x@y.com"
        assert capturado["scopes"] == list(modulo.SCOPES)


# ─── from_tenant ─────────────────────────────────────────────────────────────


class TestFromTenant:
    """`GoogleCalendarService.from_tenant()` resuelve calendar_id/timezone."""

    def _parchear_credenciales(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(modulo, "_load_credentials", lambda: _FakeCredentials())

    async def test_sin_config_ni_fallback_falla(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin `agent_configs` activo y sin `GOOGLE_CALENDAR_ID`, no hay calendario."""
        self._parchear_credenciales(monkeypatch)
        monkeypatch.setattr(modulo, "get_settings", lambda: _FakeSettings(calendar_id=""))
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[None]))

        with pytest.raises(SchedulingNotConfiguredError):
            await GoogleCalendarService.from_tenant(uuid4())

    async def test_usa_el_calendario_del_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`agent_configs.config.scheduling` manda sobre el default global."""
        self._parchear_credenciales(monkeypatch)
        monkeypatch.setattr(modulo, "get_settings", lambda: _FakeSettings(calendar_id="primary"))
        config = _FakeAgentConfig({"scheduling": {"calendar_id": "tenant-cal", "timezone": "UTC"}})
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[config]))

        servicio = await GoogleCalendarService.from_tenant(uuid4())

        assert servicio.calendar_id == "tenant-cal"
        assert servicio.timezone == "UTC"

    async def test_sin_config_de_scheduling_cae_al_calendario_global(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un tenant sin `config.scheduling` usa `GOOGLE_CALENDAR_ID` y el timezone default."""
        self._parchear_credenciales(monkeypatch)
        monkeypatch.setattr(modulo, "get_settings", lambda: _FakeSettings(calendar_id="primary"))
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[_FakeAgentConfig({})]))

        servicio = await GoogleCalendarService.from_tenant(uuid4())

        assert servicio.calendar_id == "primary"
        assert servicio.timezone == modulo.DEFAULT_TIMEZONE


# ─── check_availability ──────────────────────────────────────────────────────


class TestCheckAvailability:
    """`check_availability()` calcula los huecos libres del horario comercial."""

    async def test_dia_completamente_libre(self) -> None:
        """Sin eventos ocupados, los slots cubren todo el horario comercial."""
        fake = _FakeGoogleService(freebusy_respuesta={"calendars": {"cal-1": {"busy": []}}})
        servicio = _servicio(fake)

        slots = await servicio.check_availability(
            date=datetime(2026, 9, 21), duration_minutes=60, buffer_minutes=15
        )

        # 9:00-18:00 (540') con citas de 60' + 15' de buffer (75' por slot): 7 caben.
        assert len(slots) == 7
        assert slots[0].start.hour == 9
        assert slots[0].end.hour == 10
        assert slots[1].start.minute == 15  # 10:00 + 15' de buffer

    async def test_evento_ocupado_descarta_el_slot_que_pisa(self) -> None:
        """Un evento ocupado a media mañana corta la secuencia de slots libres."""
        tz = ZoneInfo("America/Bogota")
        busy_start = datetime(2026, 9, 21, 10, 0, tzinfo=tz).isoformat()
        busy_end = datetime(2026, 9, 21, 11, 0, tzinfo=tz).isoformat()
        fake = _FakeGoogleService(
            freebusy_respuesta={
                "calendars": {"cal-1": {"busy": [{"start": busy_start, "end": busy_end}]}}
            }
        )
        servicio = _servicio(fake)

        slots = await servicio.check_availability(
            date=datetime(2026, 9, 21), duration_minutes=60, buffer_minutes=15
        )

        assert all(
            not (
                slot.start < datetime.fromisoformat(busy_end)
                and slot.end > datetime.fromisoformat(busy_start)
            )
            for slot in slots
        )

    async def test_body_de_la_consulta_usa_el_calendario_correcto(self) -> None:
        """El `freebusy().query()` se llama con el `calendar_id` de la instancia."""
        fake = _FakeGoogleService(freebusy_respuesta={"calendars": {"cal-1": {"busy": []}}})
        servicio = _servicio(fake, calendar_id="cal-1")

        await servicio.check_availability(date=datetime(2026, 9, 21), duration_minutes=30)

        assert fake._freebusy.body_recibido["items"] == [{"id": "cal-1"}]


# ─── create_event / modify_event / cancel_event / list_events ───────────────


class TestEventos:
    """Las cuatro operaciones sobre eventos delegan en `service.events()`."""

    async def test_create_event_arma_el_body_y_parsea_la_respuesta(self) -> None:
        """`create_event` manda el body correcto y devuelve un `CalendarEvent`."""
        fake = _FakeGoogleService()
        fake.events().insert_respuesta = {
            "id": "evt-1",
            "summary": "Corte de cabello",
            "start": {"dateTime": "2026-09-21T10:00:00-05:00"},
            "end": {"dateTime": "2026-09-21T11:00:00-05:00"},
            "htmlLink": "https://calendar.google.com/evt-1",
        }
        servicio = _servicio(fake)

        evento = await servicio.create_event(
            summary="Corte de cabello",
            start=datetime.fromisoformat("2026-09-21T10:00:00-05:00"),
            end=datetime.fromisoformat("2026-09-21T11:00:00-05:00"),
            attendee_email="cliente@example.com",
        )

        assert evento.event_id == "evt-1"
        assert evento.html_link == "https://calendar.google.com/evt-1"
        accion, kwargs = fake.events().llamadas[0]
        assert accion == "insert"
        assert kwargs["body"]["attendees"] == [{"email": "cliente@example.com"}]
        assert kwargs["sendUpdates"] == "all"

    async def test_create_event_sin_invitado_no_pide_sendupdates(self) -> None:
        """Sin `attendee_email` no tiene sentido pedirle a Google que notifique a nadie."""
        fake = _FakeGoogleService()
        fake.events().insert_respuesta = {
            "id": "evt-2",
            "summary": "Bloqueo interno",
            "start": {"dateTime": "2026-09-21T10:00:00-05:00"},
            "end": {"dateTime": "2026-09-21T11:00:00-05:00"},
        }
        servicio = _servicio(fake)

        await servicio.create_event(
            summary="Bloqueo interno",
            start=datetime.fromisoformat("2026-09-21T10:00:00-05:00"),
            end=datetime.fromisoformat("2026-09-21T11:00:00-05:00"),
        )

        _, kwargs = fake.events().llamadas[0]
        assert kwargs["sendUpdates"] == "none"
        assert "attendees" not in kwargs["body"]

    async def test_modify_event_solo_cambia_los_campos_dados(self) -> None:
        """Un `modify_event` parcial no debe perder los campos que no se tocaron."""
        fake = _FakeGoogleService()
        fake.events().get_respuesta = {
            "id": "evt-3",
            "summary": "Original",
            "start": {"dateTime": "2026-09-21T10:00:00-05:00"},
            "end": {"dateTime": "2026-09-21T11:00:00-05:00"},
        }
        fake.events().update_respuesta = {
            "id": "evt-3",
            "summary": "Nuevo título",
            "start": {"dateTime": "2026-09-21T10:00:00-05:00"},
            "end": {"dateTime": "2026-09-21T11:00:00-05:00"},
        }
        servicio = _servicio(fake)

        evento = await servicio.modify_event(event_id="evt-3", new_summary="Nuevo título")

        assert evento.summary == "Nuevo título"
        _, kwargs_update = fake.events().llamadas[1]
        assert kwargs_update["body"]["start"]["dateTime"] == "2026-09-21T10:00:00-05:00"

    async def test_cancel_event_devuelve_true(self) -> None:
        """Cancelar un evento existente responde `True`."""
        fake = _FakeGoogleService()
        servicio = _servicio(fake)

        assert await servicio.cancel_event("evt-4") is True
        assert fake.events().llamadas[0][0] == "delete"

    async def test_list_events_parsea_cada_item(self) -> None:
        """`list_events` devuelve un `CalendarEvent` por cada item de la respuesta."""
        fake = _FakeGoogleService()
        fake.events().list_respuesta = {
            "items": [
                {
                    "id": "evt-5",
                    "summary": "Consulta",
                    "start": {"dateTime": "2026-09-21T10:00:00-05:00"},
                    "end": {"dateTime": "2026-09-21T11:00:00-05:00"},
                }
            ]
        }
        servicio = _servicio(fake)

        eventos = await servicio.list_events(
            time_min=datetime(2026, 9, 21), time_max=datetime(2026, 9, 22)
        )

        assert len(eventos) == 1
        assert eventos[0].event_id == "evt-5"

    async def test_error_de_la_api_se_relanza(self) -> None:
        """Un `HttpError` de Google no se traga: sube para que la tool lo reporte."""
        fake = _FakeGoogleService()
        resp_falso = type("Resp", (), {"reason": "Bad Request", "status": 400})()

        def _falla(**kwargs: Any) -> _Ejecutable:
            raise modulo.HttpError(resp=resp_falso, content=b"boom")

        fake.events().delete = _falla  # type: ignore[method-assign]
        servicio = _servicio(fake)

        with pytest.raises(modulo.HttpError):
            await servicio.cancel_event("evt-6")
