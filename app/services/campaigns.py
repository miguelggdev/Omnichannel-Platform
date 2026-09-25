"""Reglas de negocio compartidas de las campanas de marketing (Sprint 12).

Por que este modulo existe
---------------------------
Las dos protecciones de una campana — que la plantilla de WhatsApp siga
aprobada, y que el mismo segmento y canal no reciba dos campanas en 24 horas —
tienen que evaluarse en **cada** camino que pueda disparar un envio, no en uno
solo. Hoy hay tres:

1. `marketing_tools.send_campaign()`, la tool que usa el agente.
2. `campaign_tasks._marcar_en_envio()`, el chokepoint por el que pasa todo
   envio real, incluido el que encole un scheduler futuro saltandose las tools.
3. `api/v1/campaigns.py::send_campaign`, el CRUD.

Lo que tiene que repetirse es la **llamada** en cada camino, no el codigo. Las
dos primeras tenian la consulta copiada, con el comentario de que importarla
habria encadenado un modulo de tasks a uno de tools de LangChain — objecion
correcta, y por eso la definicion vive aca, en `services/`, que es la capa de
la que ya dependen ambos (igual que `segmentation.py`). Nadie depende de nadie
horizontalmente y hay una sola definicion de cada regla.
"""

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_config import AgentConfig
from app.models.campaign import CAMPAIGN_COMPLETED, CAMPAIGN_SENDING, Campaign

logger = logging.getLogger(__name__)

#: Ventana en la que no se repite una campana al mismo segmento y canal.
VENTANA_ANTIDUPLICADOS_HORAS = 24

#: Canales que soporta una campana masiva. Una sola definicion para que el
#: CRUD (`api/v1/campaigns.py`, vía `schemas/campaign.py`) y la tool del
#: agente (`marketing_tools.py`) no puedan divergir sobre que canal aceptan.
#: Hallazgo de /code-review sobre el PR #46: cada uno tenia su propia copia.
CANALES_VALIDOS: tuple[str, ...] = ("whatsapp", "telegram", "email", "instagram", "facebook")


async def plantillas_aprobadas(session: AsyncSession, client_id: UUID) -> list[str]:
    """Lee las plantillas de WhatsApp que el tenant declaro aprobadas.

    Sin integracion con la API de plantillas de Meta, esta lista en
    `agent_configs.config.marketing.approved_templates` es la fuente de verdad.
    Si el tenant no declaro ninguna, no sale ninguna campana de WhatsApp: es el
    lado correcto en el que equivocarse, porque mandar plantillas sin aprobar
    hace que Meta bloquee el numero.

    Args:
        session: Sesion con el contexto de tenant ya aplicado.
        client_id: Tenant dueno de la configuracion.

    Returns:
        Plantillas aprobadas; lista vacia si no declaro ninguna.
    """
    config = (
        await session.execute(
            select(AgentConfig)
            .where(AgentConfig.client_id == client_id, AgentConfig.is_active.is_(True))
            .order_by(AgentConfig.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()

    marketing = ((config.config or {}) if config else {}).get("marketing", {})
    aprobadas = marketing.get("approved_templates", [])
    return [str(plantilla) for plantilla in aprobadas] if isinstance(aprobadas, list) else []


async def plantilla_aprobada(session: AsyncSession, client_id: UUID, template: str) -> bool:
    """Si `template` esta en la lista de plantillas aprobadas del tenant.

    Args:
        session: Sesion con el contexto de tenant ya aplicado.
        client_id: Tenant dueno de la configuracion.
        template: Texto de la plantilla de la campana.

    Returns:
        `True` si la plantilla sigue aprobada.
    """
    return template in await plantillas_aprobadas(session, client_id)


async def campana_duplicada(
    session: AsyncSession, client_id: UUID, campana: Campaign, ahora: datetime | None = None
) -> Campaign | None:
    """Si el mismo segmento y canal ya recibio una campana en la ventana.

    La comparacion de `segment_criteria` es de igualdad JSONB, que en
    PostgreSQL es semantica: `{"tags": ["vip"], "channel": "whatsapp"}` y el
    mismo objeto con las claves al reves son iguales, como corresponde.

    Args:
        session: Sesion con el contexto de tenant ya aplicado.
        client_id: Tenant dueno de las campanas.
        campana: Campana que se quiere lanzar.
        ahora: Momento de referencia; por defecto, el actual en UTC.

    Returns:
        Una campana duplicada de la ventana, o `None` si no hay ninguna.
    """
    momento = ahora or datetime.now(timezone.utc)
    desde = momento - timedelta(hours=VENTANA_ANTIDUPLICADOS_HORAS)

    # `.limit(1)` + `.first()`, no `scalar_one_or_none()`: con dos campanas
    # previas que coincidan — perfectamente posible, la ventana son 24h — el
    # `scalar_one_or_none()` original levantaba MultipleResultsFound y tumbaba
    # el envio con un error que no decia nada. Basta con saber que hay alguna.
    # Hallazgo de /code-review sobre el PR #42.
    return (
        await session.execute(
            select(Campaign)
            .where(
                Campaign.client_id == client_id,
                Campaign.id != campana.id,
                Campaign.channel == campana.channel,
                Campaign.segment_criteria == campana.segment_criteria,
                Campaign.status.in_((CAMPAIGN_SENDING, CAMPAIGN_COMPLETED)),
                Campaign.started_at >= desde,
            )
            .order_by(Campaign.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
