"""Ayudas de los tests de integracion de deals, llamadas agendadas y analitica (Sprint 19)."""

import asyncio
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from app.core.database import tenant_session
from app.models.deal import Deal
from app.services import call_calendar
from app.services.voice_call_provider import (
    OutboundCallRequest,
    ProviderCallRef,
    VoiceCallProvider,
    VoiceProviderError,
    VoiceProviderTemporaryError,
    register_voice_provider,
    unregister_voice_provider,
)


async def crear_deal_db(client_id: uuid.UUID, lead_id: uuid.UUID, **campos: Any) -> uuid.UUID:
    """Inserta un deal por el ORM (abierto salvo que se indique) y devuelve su id."""
    campos.setdefault("title", "Deal de prueba")
    campos.setdefault("value", Decimal("1000"))
    async with tenant_session(client_id) as s:
        deal = Deal(client_id=client_id, lead_id=lead_id, **campos)
        s.add(deal)
        await s.flush()
        return deal.id


class EventoFalso:
    """Un evento de calendario."""

    def __init__(self, event_id: str, inicio: datetime, minutos: int = 30) -> None:
        """Crea el evento."""
        self.event_id = event_id
        self.start = inicio
        self.end = inicio + timedelta(minutes=minutos)


class CalendarioFalso:
    """Google Calendar de mentira: eventos ajenos y registro de lo que se le pide.

    Attributes:
        ajenos: Eventos que ya estaban (reuniones del equipo).
        creados: Eventos creados por nosotros, `id -> kwargs`.
        movidos: `(id, inicio, fin)` de cada movimiento.
        borrados: Ids borrados.
        falla: Excepcion para cualquier operacion, si se quiere simular una caida.
    """

    def __init__(self) -> None:
        """Crea el calendario vacio."""
        self.ajenos: list[EventoFalso] = []
        self.creados: dict[str, dict[str, Any]] = {}
        self.movidos: list[tuple[str, datetime, datetime]] = []
        self.borrados: list[str] = []
        self.falla: Exception | None = None

    async def list_events(
        self, desde: datetime, hasta: datetime, max_results: int = 50
    ) -> list[EventoFalso]:
        """Eventos ajenos y creados que tocan el rango."""
        if self.falla:
            raise self.falla
        propios = [
            EventoFalso(i, kw["start"], int((kw["end"] - kw["start"]).total_seconds() // 60))
            for i, kw in self.creados.items()
            if i not in self.borrados
        ]
        return [e for e in [*self.ajenos, *propios] if e.start < hasta and e.end > desde]

    async def create_event(self, **kw: Any) -> EventoFalso:
        """Crea un evento."""
        if self.falla:
            raise self.falla
        event_id = f"evt-{len(self.creados) + 1}"
        self.creados[event_id] = kw
        return EventoFalso(event_id, kw["start"])

    async def modify_event(
        self, event_id: str, new_start: datetime, new_end: datetime
    ) -> EventoFalso:
        """Mueve un evento."""
        if self.falla:
            raise self.falla
        self.movidos.append((event_id, new_start, new_end))
        if event_id in self.creados:
            self.creados[event_id] = {**self.creados[event_id], "start": new_start, "end": new_end}
        return EventoFalso(event_id, new_start)

    async def cancel_event(self, event_id: str) -> bool:
        """Borra un evento."""
        if self.falla:
            raise self.falla
        self.borrados.append(event_id)
        return True


def instalar_calendario_falso(monkeypatch: pytest.MonkeyPatch) -> CalendarioFalso:
    """Sustituye el servicio de Google Calendar por un `CalendarioFalso`."""
    falso = CalendarioFalso()

    async def servicio(client_id: Any) -> CalendarioFalso:
        return falso

    monkeypatch.setattr(call_calendar, "_servicio", servicio)
    return falso


class VozFalsa(VoiceCallProvider):
    """Proveedor de voz de mentira, con comportamiento configurable.

    Attributes:
        llamadas: Peticiones recibidas.
        cancelaciones: Referencias canceladas.
        modo: `ok`, `temporal`, `rechazo` o `lento`.
    """

    name: ClassVar[str] = "fakevoice"
    llamadas: ClassVar[list[OutboundCallRequest]] = []
    cancelaciones: ClassVar[list[ProviderCallRef]] = []
    modo: ClassVar[str] = "ok"

    async def start_call(self, request: OutboundCallRequest) -> ProviderCallRef:
        """Segun el modo: llama, falla de forma pasajera, rechaza o tarda."""
        if VozFalsa.modo == "lento":
            await asyncio.sleep(0.4)
        if VozFalsa.modo == "temporal":
            raise VoiceProviderTemporaryError("caido")
        if VozFalsa.modo == "rechazo":
            raise VoiceProviderError("rechazada")
        VozFalsa.llamadas.append(request)
        return ProviderCallRef(provider=self.name, external_id=f"CA{len(VozFalsa.llamadas):04d}")

    async def cancel_call(self, ref: ProviderCallRef) -> bool:
        """Anota la cancelacion."""
        VozFalsa.cancelaciones.append(ref)
        return True


def instalar_voz_falsa() -> type[VozFalsa]:
    """Registra `VozFalsa` y limpia su estado. Llamar a `quitar_voz_falsa()` al terminar."""
    VozFalsa.llamadas = []
    VozFalsa.cancelaciones = []
    VozFalsa.modo = "ok"
    register_voice_provider(VozFalsa)
    return VozFalsa


def quitar_voz_falsa() -> None:
    """Quita `VozFalsa` del registro."""
    unregister_voice_provider(VozFalsa.name)


async def crear_llamada_db(client_id: uuid.UUID, lead_id: uuid.UUID, **campos: Any) -> uuid.UUID:
    """Inserta una llamada agendada por el ORM (permite fechas pasadas) y devuelve su id."""
    from app.models.scheduled_call import ScheduledCall

    campos.setdefault("call_type", "human")
    campos.setdefault("status", "pending")
    campos.setdefault("duration_minutes", 30)
    async with tenant_session(client_id) as s:
        llamada = ScheduledCall(client_id=client_id, lead_id=lead_id, **campos)
        s.add(llamada)
        await s.flush()
        return llamada.id
