"""Combinar respuestas de proveedores y volcarlas al lead (Sprint 17).

Dos reglas:

1. **Entre proveedores, campo a campo, gana el primero** de la lista del tenant que trae el
   dato. Asi el tenant pone delante al proveedor en el que mas confia y los demas solo rellenan
   huecos.
2. **Lo enriquecido nunca pisa lo escrito.** Una columna del lead que ya tiene valor (la escribio
   una persona, un formulario o una importacion) no se toca. Lo enriquecido completo queda en
   `enrichment_data["company"]` y `["person"]` aunque no haya llegado a ninguna columna.

`linkedin_url` no se rellena desde un proveedor: tiene indice unico por tenant (ADR-084) y un
proveedor que devuelve el perfil equivocado chocaria con otro lead. Queda en
`enrichment_data["person"]["linkedin_url"]` para que una persona lo confirme.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, TypeVar

from pydantic import BaseModel

from app.models.lead import Lead
from app.schemas.lead_scoring import CompanyData, EnrichmentResult, PersonData
from app.services.lead_fit import tramo_por_empleados

ENRICHMENT_VERSION = 1

_M = TypeVar("_M", bound=BaseModel)


def _combinar_modelos(modelos: Sequence[_M | None], tipo: type[_M]) -> _M | None:
    """Campo a campo, el primer valor no nulo en el orden dado."""
    presentes = [m for m in modelos if m is not None]
    if not presentes:
        return None
    campos: dict[str, Any] = {}
    for nombre in tipo.model_fields:
        for modelo in presentes:
            valor = getattr(modelo, nombre)
            if valor is not None:
                campos[nombre] = valor
                break
    return tipo.model_validate(campos) if campos else None


def combinar(
    resultados: Sequence[EnrichmentResult],
) -> tuple[CompanyData | None, PersonData | None, list[str]]:
    """Junta las respuestas de varios proveedores.

    Args:
        resultados: Respuestas en orden de prioridad del tenant.

    Returns:
        `(empresa, persona, proveedores_que_aportaron)`.
    """
    empresa = _combinar_modelos([r.company for r in resultados], CompanyData)
    persona = _combinar_modelos([r.person for r in resultados], PersonData)
    aportaron = [r.provider for r in resultados if not r.vacio]
    return empresa, persona, aportaron


def _tamano(empresa: CompanyData) -> str | None:
    """Tramo de empleados de la empresa (el rango del proveedor o, si no, su numero)."""
    if empresa.employee_range is not None:
        return empresa.employee_range
    if empresa.employees is not None:
        return tramo_por_empleados(empresa.employees)
    return None


def aplicar_a_lead(
    lead: Lead,
    empresa: CompanyData | None,
    persona: PersonData | None,
    *,
    proveedores: Sequence[str],
    ahora: datetime,
) -> list[str]:
    """Vuelca lo enriquecido al lead sin pisar lo que ya tenia.

    Conserva el resto de `enrichment_data` (la captura, sus UTM...) y asigna un dict nuevo, que
    es lo que hace que SQLAlchemy detecte el cambio en una columna JSONB.

    Args:
        lead: Lead (se modifica en sitio).
        empresa: Datos de la empresa combinados.
        persona: Datos de la persona combinados.
        proveedores: Proveedores que aportaron algo.
        ahora: Instante del enriquecimiento (aware).

    Returns:
        Los nombres de las columnas del lead que se rellenaron (sin sus valores).
    """
    rellenados: list[str] = []

    def rellenar(columna: str, valor: str | None) -> None:
        actual = getattr(lead, columna)
        if valor and not (isinstance(actual, str) and actual.strip()):
            setattr(lead, columna, valor)
            rellenados.append(columna)

    if empresa is not None:
        rellenar("company_name", empresa.name)
        rellenar("company_domain", empresa.domain)
        rellenar("industry", empresa.industry)
        rellenar("company_size", _tamano(empresa))
    if persona is not None:
        rellenar("job_title", persona.job_title)

    datos = dict(lead.enrichment_data or {})
    if empresa is not None:
        datos["company"] = empresa.model_dump(mode="json", exclude_none=True)
    if persona is not None:
        datos["person"] = persona.model_dump(mode="json", exclude_none=True)
    datos["enrichment"] = {
        "version": ENRICHMENT_VERSION,
        "providers": list(proveedores),
        "at": ahora.isoformat(),
    }
    lead.enrichment_data = datos
    lead.enriched_at = ahora
    return rellenados
