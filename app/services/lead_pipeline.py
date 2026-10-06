"""Etapas por defecto del pipeline y token de captura de las fuentes (Sprint 16, ADR-082)."""

import hashlib
import secrets
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead_pipeline_stage import LeadPipelineStage

# (slug, nombre, color, es_terminal). Los slugs son los valores del enum `lead_stage_type` del
# spec: las automatizaciones de los Sprints 17-19 los referencian por nombre. Los nombres
# visibles son editables por el tenant; el slug no.
DEFAULT_STAGES: tuple[tuple[str, str, str, bool], ...] = (
    ("new", "Nuevo", "#64748B", False),
    ("enriched", "Enriquecido", "#0EA5E9", False),
    ("qualified", "Calificado", "#6366F1", False),
    ("assigned", "Asignado", "#8B5CF6", False),
    ("follow_up", "Seguimiento", "#F59E0B", False),
    ("meeting_scheduled", "Reunión agendada", "#F97316", False),
    ("proposal", "Propuesta", "#EAB308", False),
    ("negotiation", "Negociación", "#EC4899", False),
    ("won", "Ganado", "#22C55E", True),
    ("lost", "Perdido", "#EF4444", True),
    ("disqualified", "Descalificado", "#94A3B8", True),
)

#: Slug de la etapa a la que va un lead descalificado.
DISQUALIFIED_SLUG = "disqualified"
FIRST_STAGE_SLUG = "new"


async def ensure_default_stages(session: AsyncSession, client_id: UUID) -> list[LeadPipelineStage]:
    """Crea las etapas por defecto si el tenant todavia no tiene ninguna.

    Idempotente y seguro con concurrencia: un candado de transaccion por tenant serializa dos
    llamadas a la vez, y si el tenant ya configuro su pipeline (aunque sea una sola etapa) no
    se toca. Debe llamarse dentro de `tenant_session(client_id)`.

    Args:
        session: Sesion con el contexto del tenant ya fijado.
        client_id: Tenant.

    Returns:
        Las etapas del tenant ordenadas por posicion (las existentes o las recien creadas).
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:clave, 0))"),
        {"clave": f"lead_stages:{client_id}"},
    )
    existentes = (
        await session.execute(
            select(func.count())
            .select_from(LeadPipelineStage)
            .where(LeadPipelineStage.client_id == client_id)
        )
    ).scalar_one()
    if existentes == 0:
        session.add_all(
            LeadPipelineStage(
                client_id=client_id,
                slug=slug,
                name=nombre,
                position=i,
                color=color,
                is_terminal=terminal,
            )
            for i, (slug, nombre, color, terminal) in enumerate(DEFAULT_STAGES)
        )
        await session.flush()
    resultado: list[LeadPipelineStage] = list(
        (
            await session.execute(
                select(LeadPipelineStage)
                .where(LeadPipelineStage.client_id == client_id)
                .order_by(LeadPipelineStage.position)
            )
        ).scalars()
    )
    return resultado


def hash_capture_token(token: str) -> str:
    """SHA-256 del token de captura: lo unico que se guarda y por lo que se busca.

    Es un hash sin sal a proposito: el token es aleatorio de 256 bits, asi que no hay
    diccionario que atacar, y hace falta que sea determinista para buscar por el.

    Args:
        token: Token en claro, tal como llega en la URL.

    Returns:
        Hexadecimal de 64 caracteres.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def generate_capture_token() -> tuple[str, str]:
    """Genera un token de captura nuevo.

    Returns:
        `(token_en_claro, hash)`. El primero se muestra una sola vez; el segundo es lo que se
        guarda en `lead_sources.capture_token_hash`.
    """
    token = secrets.token_urlsafe(32)
    return token, hash_capture_token(token)
