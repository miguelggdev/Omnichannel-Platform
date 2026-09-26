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

Maquina de estados y concurrencia
----------------------------------
`draft` -> `scheduled` -> `sending` -> `completed`/`failed`. Confirmar un
envio (CRUD o tool) pasa la campana de `draft` a `scheduled`; solo la task la
pasa a `sending`, y solo desde `scheduled`. Cada transicion lee la fila con
`SELECT ... FOR UPDATE` (`bloquear_campana()`): sin el candado, dos llamadas
concurrentes — un doble clic en `/send`, la tool y el CRUD a la vez, o dos
tasks encoladas para la misma campana — leian las dos el estado viejo, las dos
lo pisaban y el segmento entero recibia el mensaje dos veces.

`scheduled_for` es cuando la campana tiene que salir. Confirmar sin fecha la
fija en "ahora"; con fecha futura, nadie la encola en el momento y la saca
`app.tasks.bulk_dispatch_scheduled_campaigns` (Beat) cuando llega la hora.

Lo que tiene que repetirse es la **llamada** en cada camino, no el codigo. Las
dos primeras tenian la consulta copiada, con el comentario de que importarla
habria encadenado un modulo de tasks a uno de tools de LangChain — objecion
correcta, y por eso la definicion vive aca, en `services/`, que es la capa de
la que ya dependen ambos (igual que `segmentation.py`). Nadie depende de nadie
horizontalmente y hay una sola definicion de cada regla.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
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


async def bloquear_campana(
    session: AsyncSession, client_id: UUID, campaign_id: UUID
) -> Campaign | None:
    """Lee una campana del tenant con `SELECT ... FOR UPDATE`.

    El candado de fila dura hasta el fin de la transaccion de `session`: una
    segunda transaccion que quiera la misma campana espera, y al seguir lee la
    fila ya commiteada (en READ COMMITTED, `FOR UPDATE` re-evalua la ultima
    version), asi que ve el estado que dejo la primera y no vuelve a lanzarla.

    Args:
        session: Sesion con el contexto de tenant ya aplicado.
        client_id: Tenant dueno de la campana.
        campaign_id: Campana buscada.

    Returns:
        La campana bloqueada, o `None` si no existe en este tenant.
    """
    return (
        await session.execute(
            select(Campaign)
            .where(Campaign.id == campaign_id, Campaign.client_id == client_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def serializar_lanzamientos(session: AsyncSession, client_id: UUID) -> None:
    """Serializa los lanzamientos de campanas de un tenant hasta el fin de la transaccion.

    `bloquear_campana()` evita lanzar dos veces *la misma* campana, pero el
    anti-duplicado de 24 horas compara contra *otras* campanas: dos campanas
    distintas con el mismo segmento, lanzadas a la vez, no se verian entre si
    (ninguna esta todavia en `sending` para la otra). Este advisory lock por
    tenant hace que la segunda espere a que la primera commitee su `sending`,
    y entonces `campana_duplicada()` si la encuentra. La clave lleva prefijo
    para no compartir el lock del consecutivo de facturas
    (`invoice_tools._siguiente_consecutivo`), que usa `hashtext(client_id)`.

    Args:
        session: Sesion con el contexto de tenant ya aplicado.
        client_id: Tenant cuyos lanzamientos se serializan.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:clave))"),
        {"clave": f"campaigns:{client_id}"},
    )


def debe_salir_ya(campana: Campaign, ahora: datetime | None = None) -> bool:
    """Si a la campana ya le llego la hora de salir.

    Args:
        campana: Campana a evaluar.
        ahora: Momento de referencia; por defecto, el actual en UTC.

    Returns:
        `True` si no tiene fecha programada o si esa fecha ya paso.
    """
    if campana.scheduled_for is None:
        return True
    return campana.scheduled_for <= (ahora or datetime.now(timezone.utc))


async def _config_marketing(session: AsyncSession, client_id: UUID) -> dict[str, Any]:
    """Lee `agent_configs.config.marketing` de la configuracion activa del tenant.

    Args:
        session: Sesion con el contexto de tenant ya aplicado.
        client_id: Tenant dueno de la configuracion.

    Returns:
        El objeto `marketing`, o un dict vacio si el tenant no declaro nada.
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
    return marketing if isinstance(marketing, dict) else {}


async def es_operador_de_marketing(
    session: AsyncSession, client_id: UUID, contact_id: str | None
) -> bool:
    """Si el contacto de la conversacion puede operar el agente de marketing.

    El agente de marketing vive en el mismo grafo que atiende a los clientes
    finales del tenant: habilitarlo en `enabled_agents` no dice nada de *quien*
    le habla. Sin este chequeo, cualquier contacto que escribiera por WhatsApp
    "manda una promo a todos" podia segmentar la base (y ver nombres de otros
    contactos), crear una campana y lanzar un envio masivo (BUG-045).

    Solo operan los contactos declarados en
    `agent_configs.config.marketing.operator_contact_ids` — por ejemplo, el
    WhatsApp del propio administrador del negocio. Sin lista, nadie: es el
    lado correcto en el que equivocarse, igual que con las plantillas
    aprobadas. Los administradores siguen teniendo el CRUD `/api/v1/campaigns`.

    Args:
        session: Sesion con el contexto de tenant ya aplicado.
        client_id: Tenant dueno de la conversacion.
        contact_id: Contacto de la conversacion, si lo hay.

    Returns:
        `True` si el contacto esta en la lista de operadores del tenant.
    """
    if not contact_id:
        return False
    operadores = (await _config_marketing(session, client_id)).get("operator_contact_ids", [])
    if not isinstance(operadores, list):
        return False
    return str(contact_id) in {str(operador) for operador in operadores}


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
    aprobadas = (await _config_marketing(session, client_id)).get("approved_templates", [])
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
