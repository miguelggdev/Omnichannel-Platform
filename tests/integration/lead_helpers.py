"""Ayudas compartidas por los tests de integracion del modulo de leads (Sprint 16)."""

import uuid
from typing import Any

from sqlalchemy import text

from app.core.database import tenant_session
from app.models.lead import Lead
from app.models.lead_pipeline_stage import LeadPipelineStage
from app.models.lead_source import LeadSource


async def crear_lead(client_id: uuid.UUID, **campos: Any) -> uuid.UUID:
    """Inserta un lead por el ORM (cifra y calcula los hashes) y devuelve su id."""
    async with tenant_session(client_id) as s:
        lead = Lead(client_id=client_id, **campos)
        s.add(lead)
        await s.flush()
        return lead.id


async def crear_etapa(client_id: uuid.UUID, slug: str, position: int, **campos: Any) -> uuid.UUID:
    """Inserta una etapa del pipeline y devuelve su id."""
    async with tenant_session(client_id) as s:
        etapa = LeadPipelineStage(
            client_id=client_id,
            slug=slug,
            name=campos.pop("name", slug.title()),
            position=position,
            **campos,
        )
        s.add(etapa)
        await s.flush()
        return etapa.id


async def crear_fuente(client_id: uuid.UUID, **campos: Any) -> uuid.UUID:
    """Inserta una fuente y devuelve su id."""
    campos.setdefault("name", "Fuente")
    campos.setdefault("source_type", "manual")
    async with tenant_session(client_id) as s:
        fuente = LeadSource(client_id=client_id, **campos)
        s.add(fuente)
        await s.flush()
        return fuente.id


async def limpiar_leads(client_id: uuid.UUID) -> None:
    """Borra leads, secuencias, fuentes y etapas del tenant (antes de borrar contactos y el tenant)."""
    async with tenant_session(client_id) as s:
        await s.execute(
            text("UPDATE contacts SET lead_id = NULL WHERE client_id = :c"), {"c": str(client_id)}
        )
        # `leads` antes que `lead_sequences`: sus inscripciones caen en cascada con el lead.
        for tabla in ("leads", "lead_sequences", "lead_sources", "lead_pipeline_stages"):
            await s.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :c"),  # noqa: S608
                {"c": str(client_id)},
            )


async def fila_cruda(client_id: uuid.UUID, lead_id: uuid.UUID) -> Any:
    """La fila de `leads` tal como esta en la base (email/telefono sin descifrar)."""
    async with tenant_session(client_id) as s:
        return (
            await s.execute(
                text(
                    "SELECT first_name, last_name, email IS NULL AS sin_email, "
                    "phone IS NULL AS sin_telefono, email_hash, phone_hash, linkedin_url, "
                    "job_title, company_name, disqualified_reason, enrichment_data "
                    "FROM leads WHERE id = :i"
                ),
                {"i": str(lead_id)},
            )
        ).one()


async def activar_modulo(client_id: uuid.UUID, activo: bool = True) -> None:
    """Enciende (o apaga) `lead_management_enabled` directamente en la base."""
    async with tenant_session(client_id) as s:
        await s.execute(
            text("UPDATE clients SET lead_management_enabled = :a WHERE id = :c"),
            {"a": activo, "c": str(client_id)},
        )
