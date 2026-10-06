"""RGPD / Ley 1581 sobre leads: exportar y anonimizar (Sprint 16, ADR-083).

Un lead guarda datos personales (nombre, email, telefono, LinkedIn, cargo) que el export y la
supresion de un contacto (`app/api/v1/admin.py`) no alcanzaban. Este modulo es el contrato
comun: el export y la anonimizacion de un lead suelto y los de los leads vinculados a un
contacto pasan por las mismas dos funciones.

Que se anonimiza: nombre, apellido, **email y telefono (a NULL**, que ademas libera la
deduplicacion), LinkedIn, cargo, el motivo de descalificacion (texto libre) y `enrichment_data`
(lo que un servicio externo supo de la persona). **Que se conserva, a proposito:** empresa,
sector, tamano, etapa, scores y fechas: son datos de negocio y de las estadisticas, no
identifican a la persona una vez quitado lo anterior.

El soft delete (`DELETE /leads/{id}`) **no** es una supresion: el lead sigue en la base con sus
datos. Por eso estos dos caminos incluyen tambien los leads con `deleted_at`.
"""

from typing import Any

from app.models.lead import Lead

ANONIMIZADO = "[ELIMINADO]"
MOTIVO_ANONIMIZADO = "[MOTIVO ELIMINADO POR SOLICITUD RGPD]"


def lead_esta_anonimizado(lead: Lead) -> bool:
    """Si el lead ya paso por la anonimizacion (para responder 400 en vez de repetirla).

    Args:
        lead: Lead a comprobar.

    Returns:
        `True` si el nombre es el marcador y no queda email ni telefono.
    """
    return lead.first_name == ANONIMIZADO and lead.email is None and lead.phone is None


def anonimizar_lead(lead: Lead) -> None:
    """Quita los datos personales de un lead, sin borrar la fila.

    Pasa por el ORM, asi que el listener de `Lead` recalcula (a `None`) `email_hash` y
    `phone_hash`. Un `UPDATE` masivo no lo haria: no usar uno aqui.

    Args:
        lead: Lead a anonimizar; se modifica en sitio.
    """
    lead.first_name = ANONIMIZADO
    lead.last_name = ANONIMIZADO
    lead.email = None
    lead.phone = None
    lead.linkedin_url = None
    lead.job_title = None
    if lead.disqualified_reason is not None:
        lead.disqualified_reason = MOTIVO_ANONIMIZADO
    lead.enrichment_data = {}


def lead_a_dict(lead: Lead) -> dict[str, Any]:
    """Todo lo que la plataforma guarda sobre un lead, para el export del titular.

    Args:
        lead: Lead a exportar.

    Returns:
        Un diccionario serializable en JSON (fechas ISO, importe como texto).
    """

    def iso(valor: Any) -> str | None:
        return valor.isoformat() if valor else None

    return {
        "id": str(lead.id),
        "contact_id": str(lead.contact_id) if lead.contact_id else None,
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "email": lead.email,
        "phone": lead.phone,
        "linkedin_url": lead.linkedin_url,
        "company_name": lead.company_name,
        "company_domain": lead.company_domain,
        "company_size": lead.company_size,
        "industry": lead.industry,
        "job_title": lead.job_title,
        "scores": {
            "fit": lead.fit_score,
            "behavioral": lead.behavioral_score,
            "ai": lead.ai_score,
            "total": lead.total_score,
        },
        "status": lead.status,
        "temperature": lead.temperature,
        "estimated_value": str(lead.estimated_value) if lead.estimated_value is not None else None,
        "currency": lead.currency,
        "disqualified_reason": lead.disqualified_reason,
        "enrichment_data": lead.enrichment_data or {},
        "created_at": iso(lead.created_at),
        "deleted_at": iso(lead.deleted_at),
    }
