"""Cache de datos de empresa por dominio y tenant (Sprint 17: "enrichment cache con TTL").

**Solo empresas.** Lo que un proveedor sabe de una persona es un dato personal: cachearlo
fuera de la base lo dejaria fuera del alcance de la supresion RGPD del lead. Los datos de una
empresa no identifican a nadie y son los caros de repetir (todos los leads de `acme.com`).

La clave lleva el tenant: cada tenant paga sus proveedores y elige cuales usar, y un tenant no
debe ver lo que otro consulto. Tambien se recuerda un "no encontrado", con un TTL mas corto.

Redis caido no rompe el enriquecimiento: se trata como fallo de cache (fail-open), igual que la
deduplicacion de mensajes. Lo peor que pasa es una consulta de mas al proveedor.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from app.core.config import get_settings
from app.schemas.lead_scoring import CompanyData
from app.services.dedup import get_redis

logger = logging.getLogger(__name__)

_VERSION = 1
_PREFIJO = "lead_enrich:company"


@dataclass(frozen=True)
class CacheLookup:
    """Resultado de buscar en la cache.

    Attributes:
        hit: Si habia entrada (positiva o negativa).
        company: Los datos; `None` con `hit=True` es un "no encontrado" recordado.
    """

    hit: bool
    company: CompanyData | None = None


MISS = CacheLookup(hit=False)


class CompanyCache:
    """Cache en Redis de `CompanyData` por (tenant, dominio)."""

    def __init__(
        self,
        redis_client: Any | None = None,
        *,
        ttl_seconds: int | None = None,
        negative_ttl_seconds: int | None = None,
    ) -> None:
        """Crea la cache.

        Args:
            redis_client: Cliente async; por defecto el compartido del servicio.
            ttl_seconds: Vida de una entrada con datos; por defecto
                `ENRICHMENT_CACHE_TTL_SECONDS`.
            negative_ttl_seconds: Vida de un "no encontrado"; por defecto
                `ENRICHMENT_NEGATIVE_CACHE_TTL_SECONDS`.
        """
        settings = get_settings()
        self._redis = redis_client
        self.ttl = ttl_seconds or settings.ENRICHMENT_CACHE_TTL_SECONDS
        self.negative_ttl = negative_ttl_seconds or settings.ENRICHMENT_NEGATIVE_CACHE_TTL_SECONDS

    @property
    def redis(self) -> Any:
        """El cliente de Redis (perezoso, para no conectar al importar)."""
        if self._redis is None:
            self._redis = get_redis()
        return self._redis

    @staticmethod
    def clave(client_id: UUID, dominio: str) -> str:
        """Clave de Redis de un dominio de un tenant.

        Args:
            client_id: Tenant.
            dominio: Dominio normalizado.

        Returns:
            `lead_enrich:company:{client_id}:{dominio}`.
        """
        return f"{_PREFIJO}:{client_id}:{dominio}"

    async def get(self, client_id: UUID, dominio: str) -> CacheLookup:
        """Busca un dominio.

        Args:
            client_id: Tenant.
            dominio: Dominio normalizado.

        Returns:
            `MISS` si no hay entrada, si Redis falla o si la entrada esta corrupta.
        """
        try:
            crudo = await self.redis.get(self.clave(client_id, dominio))
        except Exception as exc:  # cualquier fallo de Redis es no fatal
            logger.warning("Cache de enriquecimiento no disponible: %s", type(exc).__name__)
            return MISS
        if crudo is None:
            return MISS
        try:
            valor = json.loads(crudo)
            if valor.get("v") != _VERSION:
                return MISS
            datos = valor.get("company")
            return CacheLookup(
                hit=True, company=None if datos is None else CompanyData.model_validate(datos)
            )
        except (ValueError, TypeError, AttributeError, ValidationError):
            logger.warning("Entrada de cache de enriquecimiento corrupta; se ignora")
            return MISS

    async def set(self, client_id: UUID, dominio: str, company: CompanyData | None) -> None:
        """Guarda los datos (o el "no encontrado") de un dominio.

        Args:
            client_id: Tenant.
            dominio: Dominio normalizado.
            company: Datos, o `None` para recordar que ningun proveedor la conoce.
        """
        valor = {
            "v": _VERSION,
            "company": None
            if company is None
            else company.model_dump(mode="json", exclude_none=True),
        }
        try:
            await self.redis.set(
                self.clave(client_id, dominio),
                json.dumps(valor),
                ex=self.ttl if company is not None else self.negative_ttl,
            )
        except Exception as exc:
            logger.warning(
                "No se pudo guardar en la cache de enriquecimiento: %s", type(exc).__name__
            )

    async def invalidate(self, client_id: UUID, dominio: str) -> None:
        """Olvida un dominio (p. ej. tras cambiar de proveedores).

        Args:
            client_id: Tenant.
            dominio: Dominio normalizado.
        """
        try:
            await self.redis.delete(self.clave(client_id, dominio))
        except Exception as exc:
            logger.warning(
                "No se pudo invalidar la cache de enriquecimiento: %s", type(exc).__name__
            )
