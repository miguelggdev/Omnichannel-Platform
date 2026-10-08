"""Contrato de un proveedor de enriquecimiento (Sprint 17).

Cada integracion (Apollo, Hunter, Clearbit...) es una subclase que traduce la respuesta de su
API a `CompanyData` / `PersonData`. Lo que el resto del modulo necesita saber de un fallo esta en
el tipo de excepcion, no en el mensaje:

- `ProviderNotConfiguredError`: falta la API key o la configuracion. No se reintenta.
- `ProviderTemporaryError`: 429, 5xx, timeout. Reintentable (la tarea de Celery decide).
- Cualquier otra `EnrichmentError`: la respuesta no se pudo usar. No se reintenta.

Los mensajes de error **no deben llevar el email ni el dominio consultado**: acaban en logs.
"""

from abc import ABC
from dataclasses import dataclass
from typing import ClassVar

from app.schemas.lead_scoring import CompanyData, PersonData


class EnrichmentError(Exception):
    """Fallo de un proveedor de enriquecimiento."""


class ProviderNotConfiguredError(EnrichmentError):
    """El proveedor no tiene credenciales o configuracion: no se puede usar."""


class ProviderTemporaryError(EnrichmentError):
    """Fallo pasajero (limite de peticiones, caida, timeout): se puede reintentar.

    Attributes:
        retry_after: Segundos que pidio esperar el proveedor, si lo dijo.
    """

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class PersonQuery:
    """Lo que se le pasa a un proveedor para buscar a la persona.

    Attributes:
        email: Email del lead.
        linkedin_url: Perfil de LinkedIn (canonico).
        domain: Dominio de su empresa, si se conoce.
        full_name: Nombre y apellido, si se conocen.
    """

    email: str | None = None
    linkedin_url: str | None = None
    domain: str | None = None
    full_name: str | None = None

    @property
    def vacia(self) -> bool:
        """Si no hay nada por lo que buscar a la persona."""
        return not (self.email or self.linkedin_url)


class EnrichmentProvider(ABC):
    """Un proveedor de enriquecimiento.

    Un proveedor puede saber de empresas, de personas o de ambas; lo declara con
    `supports_company` / `supports_person` y sobrescribe el metodo correspondiente. Por eso no
    hay metodos abstractos: uno que solo verifica emails (Hunter) no implementa empresas.

    Attributes:
        name: Identificador estable ("apollo"); es lo que el tenant guarda en su configuracion.
        supports_company: Si implementa `enrich_company()`.
        supports_person: Si implementa `enrich_person()`.
    """

    name: ClassVar[str]
    supports_company: ClassVar[bool] = False
    supports_person: ClassVar[bool] = False

    async def enrich_company(self, domain: str) -> CompanyData | None:
        """Datos de la empresa de un dominio.

        Args:
            domain: Dominio normalizado (`acme.com`).

        Returns:
            Los datos, o `None` si el proveedor no la conoce.

        Raises:
            EnrichmentError: Si falla (ver las subclases para saber si se reintenta).
        """
        raise NotImplementedError(f"{self.name} no enriquece empresas")

    async def enrich_person(self, query: PersonQuery) -> PersonData | None:
        """Datos de la persona.

        Args:
            query: Email, LinkedIn y lo demas que se sepa.

        Returns:
            Los datos, o `None` si el proveedor no la conoce.

        Raises:
            EnrichmentError: Si falla.
        """
        raise NotImplementedError(f"{self.name} no enriquece personas")
