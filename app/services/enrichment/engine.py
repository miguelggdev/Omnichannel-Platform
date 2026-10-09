"""Orquestador del enriquecimiento de un lead (Sprint 17).

`enriquecer_lead()` hace el recorrido completo: deduce el dominio, busca la empresa (cache y
proveedores), busca a la persona (solo proveedores: no se cachea), combina, vuelca al lead sin
pisar lo escrito y anota el resultado en el historial. **No recalcula scores ni hace `commit`**:
quien lo llama (la tarea de Celery o el endpoint de Dev B) decide si recalcular el FIT despues
(`lead_score_service.recalcular_fit`, disparador `enrichment`) y cierra la transaccion.

Fallos de un proveedor:

- sin configurar: se omite;
- pasajero (`ProviderTemporaryError`): se sigue con los demas y el resultado dice `retryable`,
  para que la tarea reintente; **no** se guarda un "no encontrado" en la cache, porque no se
  sabe;
- cualquier otro: se omite ese proveedor.

Nunca se envia a un proveedor un lead borrado o anonimizado por RGPD.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead import Lead
from app.models.lead_activity import ACTIVITY_ENRICHED, ACTIVITY_ENRICHMENT_EMPTY
from app.schemas.lead_scoring import CompanyData, EnrichmentResult, PersonData
from app.services.enrichment.base import (
    EnrichmentError,
    EnrichmentProvider,
    PersonQuery,
    ProviderNotConfiguredError,
    ProviderTemporaryError,
)
from app.services.enrichment.cache import CompanyCache
from app.services.enrichment.domain import inferir_dominio
from app.services.enrichment.merge import aplicar_a_lead, combinar
from app.services.lead_activity import registrar_actividad
from app.services.lead_privacy import lead_esta_anonimizado

logger = logging.getLogger(__name__)

SKIP_DELETED = "deleted"
SKIP_ANONYMIZED = "anonymized"
SKIP_NOTHING_TO_LOOK_UP = "nothing_to_look_up"
SKIP_NO_PROVIDERS = "no_providers"

ERROR_NOT_CONFIGURED = "not_configured"
ERROR_TEMPORARY = "temporary"
ERROR_FAILED = "failed"


@dataclass
class EnrichmentOutcome:
    """Que paso al enriquecer un lead (sin datos de la persona: va a logs y a la API).

    Attributes:
        skipped: Motivo si no se intento nada (`SKIP_*`), o `None`.
        providers_used: Proveedores que aportaron algun dato.
        fields_filled: Columnas del lead que se rellenaron.
        company_from_cache: Si la empresa salio de la cache.
        errors: Proveedor -> codigo de error (`ERROR_*`).
        retry_after: Mayor espera pedida por un proveedor con fallo pasajero.
    """

    skipped: str | None = None
    providers_used: list[str] = field(default_factory=list)
    fields_filled: list[str] = field(default_factory=list)
    company_from_cache: bool = False
    errors: dict[str, str] = field(default_factory=dict)
    retry_after: int | None = None

    @property
    def retryable(self) -> bool:
        """Si algun proveedor fallo de forma pasajera (la tarea deberia reintentar)."""
        return ERROR_TEMPORARY in self.errors.values()

    @property
    def found_anything(self) -> bool:
        """Si algun proveedor (o la cache) aporto datos."""
        return bool(self.providers_used) or self.company_from_cache


def _registrar_error(
    salida: EnrichmentOutcome, proveedor: str, exc: EnrichmentError, client_id: UUID
) -> None:
    """Anota el fallo de un proveedor por su tipo (el mensaje puede llevar datos; no se usa)."""
    if isinstance(exc, ProviderNotConfiguredError):
        salida.errors[proveedor] = ERROR_NOT_CONFIGURED
    elif isinstance(exc, ProviderTemporaryError):
        salida.errors[proveedor] = ERROR_TEMPORARY
        if exc.retry_after is not None:
            salida.retry_after = max(salida.retry_after or 0, exc.retry_after)
    else:
        salida.errors[proveedor] = ERROR_FAILED
    logger.warning(
        "Proveedor de enriquecimiento %s fallo (%s) client_id=%s",
        proveedor,
        type(exc).__name__,
        client_id,
    )


async def _buscar_empresa(
    dominio: str,
    proveedores: Sequence[EnrichmentProvider],
    salida: EnrichmentOutcome,
    client_id: UUID,
) -> tuple[list[EnrichmentResult], bool]:
    """Pregunta la empresa a cada proveedor que sabe de empresas.

    Returns:
        `(respuestas, concluyente)`: concluyente si al menos uno respondio y **ninguno** fallo de
        forma pasajera. Es la condicion para guardar el resultado en la cache: si uno fallo, el
        reintento tiene que volver a preguntarle, y una entrada en la cache lo impediria.
    """
    respuestas: list[EnrichmentResult] = []
    alguno_respondio = False
    fallo_pasajero = False
    for proveedor in proveedores:
        if not proveedor.supports_company:
            continue
        try:
            empresa = await proveedor.enrich_company(dominio)
        except EnrichmentError as exc:
            _registrar_error(salida, proveedor.name, exc, client_id)
            fallo_pasajero = fallo_pasajero or isinstance(exc, ProviderTemporaryError)
            continue
        alguno_respondio = True
        respuestas.append(EnrichmentResult(provider=proveedor.name, company=empresa))
    return respuestas, alguno_respondio and not fallo_pasajero


async def _buscar_persona(
    consulta: PersonQuery,
    proveedores: Sequence[EnrichmentProvider],
    salida: EnrichmentOutcome,
    client_id: UUID,
) -> list[EnrichmentResult]:
    """Pregunta la persona a cada proveedor que sabe de personas."""
    respuestas: list[EnrichmentResult] = []
    for proveedor in proveedores:
        if not proveedor.supports_person:
            continue
        try:
            persona = await proveedor.enrich_person(consulta)
        except EnrichmentError as exc:
            _registrar_error(salida, proveedor.name, exc, client_id)
            continue
        respuestas.append(EnrichmentResult(provider=proveedor.name, person=persona))
    return respuestas


async def enriquecer_lead(
    session: AsyncSession,
    lead: Lead,
    proveedores: Sequence[EnrichmentProvider],
    *,
    ahora: datetime,
    cache: CompanyCache | None = None,
    enriquecer_persona: bool = True,
    user_id: UUID | None = None,
) -> EnrichmentOutcome:
    """Enriquece un lead con los proveedores dados (en orden de prioridad).

    Args:
        session: Sesion con el contexto del tenant fijado (para el historial).
        lead: Lead a enriquecer (se modifica en sitio).
        proveedores: Proveedores del tenant, ya construidos (`registry.build_providers`).
        ahora: Instante del enriquecimiento (aware).
        cache: Cache de empresas; `None` para no usarla.
        enriquecer_persona: `False` para consultar solo la empresa (no se envia ningun dato
            personal a terceros).
        user_id: Quien lo pidio; `None` si es el sistema.

    Returns:
        Lo ocurrido. Si `skipped` tiene valor, no se toco el lead.
    """
    salida = EnrichmentOutcome()
    if lead.deleted_at is not None:
        salida.skipped = SKIP_DELETED
        return salida
    if lead_esta_anonimizado(lead):
        salida.skipped = SKIP_ANONYMIZED
        return salida
    if not proveedores and cache is None:
        salida.skipped = SKIP_NO_PROVIDERS
        return salida

    dominio = inferir_dominio(lead.company_domain, lead.email)
    consulta = PersonQuery(
        email=lead.email,
        linkedin_url=lead.linkedin_url,
        domain=dominio,
        full_name=" ".join(p for p in (lead.first_name, lead.last_name) if p) or None,
    )
    buscar_persona = enriquecer_persona and not consulta.vacia
    if dominio is None and not buscar_persona:
        salida.skipped = SKIP_NOTHING_TO_LOOK_UP
        return salida

    empresa: CompanyData | None = None
    persona: PersonData | None = None
    proveedores_empresa: list[str] = []
    if dominio is not None:
        encontrado = await cache.get(lead.client_id, dominio) if cache is not None else None
        if encontrado is not None and encontrado.hit:
            # Un "no encontrado" recordado no aporta datos: no cuenta como acierto de la cache.
            empresa = encontrado.company
            salida.company_from_cache = empresa is not None
        else:
            respuestas, concluyente = await _buscar_empresa(
                dominio, proveedores, salida, lead.client_id
            )
            empresa, _, proveedores_empresa = combinar(respuestas)
            if cache is not None and concluyente:
                await cache.set(lead.client_id, dominio, empresa)

    proveedores_persona: list[str] = []
    if buscar_persona:
        _, persona, proveedores_persona = combinar(
            await _buscar_persona(consulta, proveedores, salida, lead.client_id)
        )
    salida.providers_used = list(dict.fromkeys(proveedores_empresa + proveedores_persona))

    if empresa is None and persona is None:
        registrar_actividad(
            session,
            client_id=lead.client_id,
            lead_id=lead.id,
            tipo=ACTIVITY_ENRICHMENT_EMPTY,
            user_id=user_id,
            errors=dict(salida.errors),
        )
        return salida

    salida.fields_filled = aplicar_a_lead(
        lead,
        empresa,
        persona,
        proveedores=salida.providers_used or (["cache"] if salida.company_from_cache else []),
        ahora=ahora,
    )
    registrar_actividad(
        session,
        client_id=lead.client_id,
        lead_id=lead.id,
        tipo=ACTIVITY_ENRICHED,
        user_id=user_id,
        providers=salida.providers_used,
        fields=salida.fields_filled,
        from_cache=salida.company_from_cache,
        errors=dict(salida.errors),
    )
    return salida
