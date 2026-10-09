"""Registro de proveedores de enriquecimiento por nombre (Sprint 17).

Las integraciones se registran con `@register_provider` y el tenant elige cuales usar por su
`name`. Construir un proveedor sin credenciales no es un error del lead: se omite y se avisa.
"""

import logging
from collections.abc import Sequence
from uuid import UUID

from app.services.enrichment.base import EnrichmentProvider, ProviderNotConfiguredError

logger = logging.getLogger(__name__)

_REGISTRO: dict[str, type[EnrichmentProvider]] = {}


class UnknownProviderError(ValueError):
    """Se pidio un proveedor que no esta registrado."""


def register_provider(cls: type[EnrichmentProvider]) -> type[EnrichmentProvider]:
    """Registra una clase de proveedor por su `name` (uso como decorador).

    Args:
        cls: Subclase de `EnrichmentProvider` con `name` definido.

    Returns:
        La misma clase.

    Raises:
        ValueError: Si no tiene `name`, no declara que soporta nada o el nombre ya esta tomado
            por otra clase.
    """
    nombre = getattr(cls, "name", None)
    if not nombre:
        raise ValueError("Un proveedor de enriquecimiento necesita `name`")
    if not (cls.supports_company or cls.supports_person):
        raise ValueError(f"{nombre}: declara supports_company o supports_person")
    existente = _REGISTRO.get(nombre)
    if existente is not None and existente is not cls:
        raise ValueError(f"Ya hay un proveedor llamado {nombre!r}")
    _REGISTRO[nombre] = cls
    return cls


def unregister_provider(name: str) -> None:
    """Quita un proveedor del registro (para tests).

    Args:
        name: Nombre registrado.
    """
    _REGISTRO.pop(name, None)


def available_providers() -> tuple[str, ...]:
    """Nombres registrados, en orden alfabetico.

    Returns:
        Los nombres.
    """
    return tuple(sorted(_REGISTRO))


def build_providers(
    names: Sequence[str], *, client_id: UUID | None = None
) -> list[EnrichmentProvider]:
    """Instancia los proveedores pedidos, en ese orden (el orden es la prioridad al fusionar).

    Un proveedor cuyo constructor lanza `ProviderNotConfiguredError` (falta su API key) se omite
    con un aviso: no puede impedir que los demas enriquezcan.

    Args:
        names: Nombres en orden de prioridad; los repetidos se ignoran.
        client_id: Tenant, solo para el log.

    Returns:
        Los proveedores utilizables.

    Raises:
        UnknownProviderError: Si algun nombre no esta registrado (es un error de configuracion,
            mejor verlo que ignorarlo).
    """
    desconocidos = [n for n in names if n not in _REGISTRO]
    if desconocidos:
        raise UnknownProviderError(f"Proveedores de enriquecimiento desconocidos: {desconocidos}")
    proveedores: list[EnrichmentProvider] = []
    vistos: set[str] = set()
    for nombre in names:
        if nombre in vistos:
            continue
        vistos.add(nombre)
        try:
            proveedores.append(_REGISTRO[nombre]())
        except ProviderNotConfiguredError:
            logger.warning(
                "Proveedor de enriquecimiento %s sin configurar; se omite (client_id=%s)",
                nombre,
                client_id,
            )
    return proveedores
