"""Calificacion automatica de leads por umbrales de score (Sprint 17, Dev B).

Cuando cambia el score total de un lead, `calificar_automaticamente()` lo pasa a la etapa de la
regla mas alta que alcance. Decisiones (PROGRESS.md, "Calificacion automatica"):

- **Apagada por defecto y por tenant** (`clients.lead_automation_config.qualification`).
- **Solo hacia delante:** el destino debe estar despues de la etapa actual. Un lead nunca
  retrocede solo, aunque su score baje.
- **Nunca deshace a una persona:** si el ultimo cambio de etapa lo hizo un usuario, la
  automatizacion no vuelve a tocar el lead (un humano que lo movio a mano manda).
- **Solo leads activos** y **solo a etapas no terminales**: ganar, perder o descalificar es
  siempre una decision humana.
- El cambio queda en el historial como `stage_changed` con `automatic=True` y el umbral aplicado,
  sin `user_id`.

Ningun paso hace `commit`: va en la transaccion de quien llama.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.models.lead import Lead
from app.models.lead_activity import ACTIVITY_STAGE_CHANGED, LeadActivity
from app.models.lead_pipeline_stage import LeadPipelineStage
from app.schemas.lead_automation import LeadAutomationConfig, QualificationConfig
from app.services.lead_activity import registrar_actividad
from app.services.lead_privacy import lead_esta_anonimizado

logger = logging.getLogger(__name__)


def cargar_automatizacion(raw: Any, *, client_id: UUID) -> LeadAutomationConfig:
    """La configuracion de automatizacion guardada, o la de por defecto si no sirve.

    Un JSON invalido solo puede venir de una escritura directa en la base: se registra y se usa
    todo apagado, porque un proceso en segundo plano no debe romperse por eso.

    Args:
        raw: Valor de `clients.lead_automation_config`.
        client_id: Tenant, para el log.

    Returns:
        La configuracion validada.
    """
    if not raw:
        return LeadAutomationConfig()
    try:
        return LeadAutomationConfig.model_validate(
            {k: v for k, v in raw.items() if k in LeadAutomationConfig.model_fields}
        )
    except (ValidationError, AttributeError):
        logger.warning("lead_automation_config invalido (client_id=%s); se usa apagado", client_id)
        return LeadAutomationConfig()


async def config_del_tenant(session: AsyncSession, client_id: UUID) -> LeadAutomationConfig:
    """Lee la configuracion de automatizacion del tenant.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.

    Returns:
        La configuracion (todo apagado si no hay o no es valida).
    """
    raw = (
        await session.execute(select(Client.lead_automation_config).where(Client.id == client_id))
    ).scalar_one_or_none()
    return cargar_automatizacion(raw, client_id=client_id)


async def validar_reglas(
    session: AsyncSession, client_id: UUID, config: QualificationConfig
) -> list[str]:
    """Comprueba que las etapas de las reglas existan en el tenant y no sean terminales.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        config: Configuracion a validar.

    Returns:
        Los problemas encontrados (lista vacia si todo esta bien).
    """
    if not config.rules:
        return []
    etapas = {
        e.slug: e
        for e in (
            await session.execute(
                select(LeadPipelineStage).where(LeadPipelineStage.client_id == client_id)
            )
        ).scalars()
    }
    problemas: list[str] = []
    for regla in config.rules:
        etapa = etapas.get(regla.stage_slug)
        if etapa is None:
            problemas.append(f"La etapa '{regla.stage_slug}' no existe")
        elif etapa.is_terminal:
            problemas.append(
                f"La etapa '{regla.stage_slug}' es terminal: eso lo decide una persona"
            )
    return problemas


async def _movido_a_mano(session: AsyncSession, lead: Lead) -> bool:
    """Si el ultimo cambio de etapa del lead lo hizo una persona."""
    ultimo = (
        await session.execute(
            select(LeadActivity.user_id)
            .where(
                LeadActivity.client_id == lead.client_id,
                LeadActivity.lead_id == lead.id,
                LeadActivity.activity_type == ACTIVITY_STAGE_CHANGED,
            )
            .order_by(LeadActivity.created_at.desc(), LeadActivity.id.desc())
            .limit(1)
        )
    ).first()
    return ultimo is not None and ultimo[0] is not None


async def calificar_automaticamente(session: AsyncSession, lead: Lead) -> str | None:
    """Mueve el lead de etapa si su score total alcanza una regla del tenant.

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead con `total_score` ya calculado (se modifica en sitio). El cambio de etapa se
            anota como hecho del sistema (sin `user_id`), aunque lo haya disparado una persona.

    Returns:
        El slug de la etapa de destino si lo movio; `None` si no hizo nada.
    """
    if lead.deleted_at is not None or lead_esta_anonimizado(lead) or lead.status != "active":
        return None
    config = (await config_del_tenant(session, lead.client_id)).qualification
    if not config.enabled:
        return None
    total = lead.total_score or 0
    alcanzadas = [r for r in config.rules if total >= r.min_score]
    if not alcanzadas:
        return None
    regla = max(alcanzadas, key=lambda r: r.min_score)

    etapas = {
        e.id: e
        for e in (
            await session.execute(
                select(LeadPipelineStage).where(LeadPipelineStage.client_id == lead.client_id)
            )
        ).scalars()
    }
    destino = next((e for e in etapas.values() if e.slug == regla.stage_slug), None)
    actual = etapas.get(lead.pipeline_stage_id) if lead.pipeline_stage_id else None
    if destino is None or destino.is_terminal or destino.id == lead.pipeline_stage_id:
        return None
    if actual is not None and (actual.is_terminal or destino.position <= actual.position):
        return None
    if await _movido_a_mano(session, lead):
        return None

    lead.pipeline_stage_id = destino.id
    lead.last_activity_at = datetime.now(timezone.utc)
    await session.flush()
    registrar_actividad(
        session,
        client_id=lead.client_id,
        lead_id=lead.id,
        tipo=ACTIVITY_STAGE_CHANGED,
        user_id=None,
        from_stage=actual.slug if actual is not None else None,
        to_stage=destino.slug,
        status=lead.status,
        automatic=True,
        rule_min_score=regla.min_score,
    )
    # Import perezoso: el servicio de secuencias importa servicios que dependen de este.
    from app.services.lead_sequence_triggers import disparar_inscripciones

    await disparar_inscripciones(session, lead, "stage_changed")
    return str(destino.slug)
