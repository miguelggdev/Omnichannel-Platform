"""Contrato de los proveedores de llamadas con voz IA (Sprint 19, `VoiceCallProvider`).

El spec propone Vapi.ai (principal) y Bland.ai (respaldo). **No se eligio proveedor:** igual que
el enriquecimiento (ADR-085), queda la interfaz y el registro. Tambien es una opcion el canal de
voz propio sobre Twilio del Sprint 13 (`app/services/voice/`), que ya hace STT, TTS y barge-in;
decidirlo es del usuario (PROGRESS.md, "Lead Management: pendientes").

Cada integracion es una subclase registrada con `@register_voice_provider` que traduce su API.
Lo que el resto necesita saber de un fallo esta en el tipo de excepcion:

- `VoiceProviderNotConfiguredError`: falta la API key o el numero. No se reintenta.
- `VoiceProviderTemporaryError`: 429, 5xx, timeout. Reintentable.
- Cualquier otra `VoiceProviderError`: la peticion no sirvio. No se reintenta.

**Los mensajes de error no llevan el telefono marcado**: acaban en logs.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar
from uuid import UUID

logger = logging.getLogger(__name__)


class VoiceProviderError(Exception):
    """Fallo de un proveedor de voz."""


class VoiceProviderNotConfiguredError(VoiceProviderError):
    """Sin credenciales o configuracion: no se puede usar."""


class VoiceProviderTemporaryError(VoiceProviderError):
    """Fallo pasajero: se puede reintentar.

    Attributes:
        retry_after: Segundos que pidio esperar el proveedor, si lo dijo.
    """

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        """Crea el error.

        Args:
            message: Descripcion (sin el telefono).
            retry_after: Espera sugerida.
        """
        super().__init__(message)
        self.retry_after = retry_after


class UnknownVoiceProviderError(ValueError):
    """Se pidio un proveedor que no esta registrado."""


@dataclass(frozen=True)
class OutboundCallRequest:
    """Lo que se le pide al proveedor para una llamada saliente.

    Attributes:
        scheduled_call_id: Nuestra llamada agendada (para casar el webhook de vuelta).
        client_id: Tenant.
        to_phone: Telefono del lead en E.164 (dato personal: nunca a los logs).
        language: Idioma de la conversacion (`es`, `en`...).
        timezone: Zona del lead.
        max_duration_minutes: Duracion maxima.
        context: Lo que el agente de voz necesita saber (sin datos sensibles innecesarios).
        config: `scheduled_calls.ai_voice_config` (voz, guion, asistente del proveedor).
    """

    scheduled_call_id: UUID
    client_id: UUID
    to_phone: str
    language: str = "es"
    timezone: str = "America/Bogota"
    max_duration_minutes: int = 30
    context: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Sin el telefono: el repr acaba en logs y trazas."""
        return (
            f"OutboundCallRequest(scheduled_call_id={self.scheduled_call_id}, "
            f"client_id={self.client_id}, to_phone='***')"
        )


@dataclass(frozen=True)
class ProviderCallRef:
    """Referencia de la llamada en el proveedor.

    Attributes:
        provider: Nombre del proveedor.
        external_id: Id de la llamada en el proveedor.
        status: Estado que informo (texto del proveedor, solo informativo).
    """

    provider: str
    external_id: str
    status: str | None = None


class VoiceCallProvider(ABC):
    """Un proveedor de llamadas con voz IA.

    Attributes:
        name: Nombre con el que se registra y se guarda en `scheduled_calls.ai_voice_provider`.
    """

    name: ClassVar[str] = ""

    @abstractmethod
    async def start_call(self, request: OutboundCallRequest) -> ProviderCallRef:
        """Inicia la llamada.

        Args:
            request: Lo que hay que llamar.

        Returns:
            La referencia en el proveedor.

        Raises:
            VoiceProviderError: Si no se pudo (ver tipos arriba).
        """

    @abstractmethod
    async def cancel_call(self, ref: ProviderCallRef) -> bool:
        """Cuelga o cancela una llamada en curso o programada en el proveedor.

        Args:
            ref: Referencia devuelta por `start_call`.

        Returns:
            `True` si el proveedor la cancelo; `False` si ya no estaba activa.

        Raises:
            VoiceProviderError: Si no se pudo.
        """


_REGISTRO: dict[str, type[VoiceCallProvider]] = {}


def register_voice_provider(cls: type[VoiceCallProvider]) -> type[VoiceCallProvider]:
    """Registra una clase de proveedor por su `name` (uso como decorador).

    Args:
        cls: Subclase con `name`.

    Returns:
        La misma clase.

    Raises:
        ValueError: Sin `name`, nombre de mas de 30 caracteres (la columna) o ya tomado.
    """
    nombre = cls.name
    if not nombre:
        raise ValueError("Un proveedor de voz necesita `name`")
    if len(nombre) > 30:
        raise ValueError("El nombre del proveedor no cabe en ai_voice_provider (30)")
    existente = _REGISTRO.get(nombre)
    if existente is not None and existente is not cls:
        raise ValueError(f"Ya hay un proveedor de voz llamado {nombre!r}")
    _REGISTRO[nombre] = cls
    return cls


def unregister_voice_provider(name: str) -> None:
    """Quita un proveedor del registro (para tests).

    Args:
        name: Nombre registrado.
    """
    _REGISTRO.pop(name, None)


def available_voice_providers() -> tuple[str, ...]:
    """Nombres registrados, en orden alfabetico.

    Returns:
        Los nombres.
    """
    return tuple(sorted(_REGISTRO))


def build_voice_provider(name: str) -> VoiceCallProvider:
    """Instancia un proveedor.

    Args:
        name: Nombre registrado.

    Returns:
        El proveedor.

    Raises:
        UnknownVoiceProviderError: Si no esta registrado.
        VoiceProviderNotConfiguredError: Si su constructor no encuentra credenciales.
    """
    clase = _REGISTRO.get(name)
    if clase is None:
        raise UnknownVoiceProviderError(f"Proveedor de voz desconocido: {name!r}")
    return clase()
