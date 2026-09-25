"""Tools de campanas masivas que usa el agente de marketing (Sprint 12).

Mismo contrato de seguridad que las demas tools: `client_id` llega por
`config["configurable"]`, nunca como argumento del LLM.

Dos reglas del spec que aca se cumplen en codigo, no solo en el prompt del
agente (un prompt es una sugerencia; el modelo puede ignorarla):

1. **Plantilla de WhatsApp pre-aprobada.** Meta exige que los mensajes masivos
   por WhatsApp usen plantillas aprobadas. Sin integracion con la API de
   plantillas de Meta, la lista aprobada del tenant se declara en
   `agent_configs.config.marketing.approved_templates`. Si el tenant no
   declaro ninguna, ninguna campana de WhatsApp sale — que es el lado correcto
   en el que equivocarse: mandar plantillas no aprobadas hace que Meta bloquee
   el numero del tenant.
2. **Sin campanas duplicadas en 24 horas.** `send_campaign()` rechaza lanzar si
   el mismo canal y los mismos criterios ya salieron en las ultimas 24 horas.

El envio nunca ocurre dentro de la tool: `send_campaign()` encola
`app.tasks.bulk_execute_campaign` (cola `bulk`) y vuelve. Un envio masivo
dentro del grafo dejaria la conversacion del usuario esperando minutos.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.campaign import (
    CAMPAIGN_DRAFT,
    CAMPAIGN_LANZABLE,
    CAMPAIGN_SCHEDULED,
    Campaign,
)
from app.services.campaigns import (
    CANALES_VALIDOS,
    VENTANA_ANTIDUPLICADOS_HORAS,
    campana_duplicada,
    plantillas_aprobadas,
)
from app.services.segmentation import CriterioInvalidoError, contar_segmento, resolver_segmento

logger = logging.getLogger(__name__)

# Ventana en la que no se repite una campana al mismo segmento.

# Cuantos contactos de ejemplo se muestran al segmentar.
EJEMPLOS_SEGMENTO = 5


def _client_id(config: RunnableConfig) -> UUID:
    """Extrae el `client_id` inyectado por el nodo, nunca provisto por el LLM.

    Args:
        config: Config que inyecta LangChain desde `.ainvoke(..., config=...)`.

    Returns:
        UUID del tenant dueno de la conversacion.
    """
    return UUID(config["configurable"]["client_id"])


async def _plantillas_aprobadas(client_id: UUID) -> list[str]:
    """Lee las plantillas de WhatsApp aprobadas del tenant, en su propia sesion.

    La consulta vive en `services/campaigns.py`, compartida con la task; aca
    solo se le abre la sesion con contexto de tenant.

    Args:
        client_id: Tenant dueno de la configuracion.

    Returns:
        Plantillas aprobadas; lista vacia si no declaro ninguna.
    """
    async with tenant_session(client_id) as session:
        return await plantillas_aprobadas(session, client_id)


@tool(parse_docstring=True)
async def segment_contacts(criteria: dict[str, Any], config: RunnableConfig) -> str:
    """Cuenta y muestra los contactos que cumplen unos criterios de segmentacion.

    Args:
        criteria: Criterios de segmentacion. Claves admitidas: tags con una
            lista de nombres de etiqueta que el contacto debe tener todas,
            channel con el canal, last_active_days con los dias de actividad
            reciente, score_min con el score minimo, y metadata con pares
            clave/valor.
    """
    client_id = _client_id(config)

    try:
        async with tenant_session(client_id) as session:
            total = await contar_segmento(session, client_id, criteria)
            ejemplos = await resolver_segmento(
                session, client_id, criteria, limite=EJEMPLOS_SEGMENTO
            )
            nombres = [
                contacto.display_name
                or " ".join(filter(None, [contacto.first_name, contacto.last_name])).strip()
                or "Sin nombre"
                for contacto in ejemplos
            ]
    except CriterioInvalidoError as exc:
        return f"No se pudo segmentar: {exc}."

    if total == 0:
        return "Ningun contacto cumple esos criterios."

    muestra = ", ".join(nombres)
    return f"{total} contacto(s) en el segmento. Algunos: {muestra}."


@tool(parse_docstring=True)
async def create_campaign(
    name: str,
    segment_criteria: dict[str, Any],
    message_template: str,
    config: RunnableConfig,
    channel: str = "whatsapp",
    schedule: str | None = None,
) -> str:
    """Crea una campana de mensajeria masiva, sin enviarla todavia.

    Args:
        name: Nombre descriptivo de la campana.
        segment_criteria: Criterios de segmentacion, los mismos que acepta
            segment_contacts.
        message_template: Texto del mensaje. Admite las variables
            {{contact_name}}, {{first_name}} y {{last_name}}.
        channel: Canal de envio. Uno de whatsapp, telegram, email, instagram o
            facebook.
        schedule: Fecha y hora de envio programado en formato ISO 8601. Si se
            omite, la campana queda en borrador para enviarla cuando se
            confirme.
    """
    client_id = _client_id(config)

    if channel not in CANALES_VALIDOS:
        return f"Canal invalido: {channel}. Validos: {', '.join(CANALES_VALIDOS)}."

    if channel == "whatsapp":
        aprobadas = await _plantillas_aprobadas(client_id)
        if message_template not in aprobadas:
            return (
                "El template de WhatsApp no esta aprobado por Meta. "
                "Usa uno de los aprobados del tenant o solicita la aprobacion antes de enviar."
            )

    programada: datetime | None = None
    if schedule:
        try:
            programada = datetime.fromisoformat(schedule.replace("Z", "+00:00"))
        except ValueError:
            return f"Fecha de programacion invalida: {schedule}. Usa formato ISO 8601."
        if programada.tzinfo is None:
            programada = programada.replace(tzinfo=timezone.utc)

    campaign_id = uuid4()
    try:
        async with tenant_session(client_id) as session:
            total = await contar_segmento(session, client_id, segment_criteria)
            session.add(
                Campaign(
                    id=campaign_id,
                    client_id=client_id,
                    name=name,
                    channel=channel,
                    segment_criteria=segment_criteria,
                    message_template=message_template,
                    status=CAMPAIGN_SCHEDULED if programada else CAMPAIGN_DRAFT,
                    target_count=total,
                    scheduled_for=programada,
                )
            )
    except CriterioInvalidoError as exc:
        return f"No se creo la campana: {exc}."

    cuando = (
        f" Programada para el {programada:%d/%m/%Y %H:%M} UTC."
        if programada
        else " Queda en borrador: confirmala para enviarla."
    )
    return (
        f"Campana '{name}' creada por {channel} con {total} contacto(s) en el segmento.{cuando} "
        f"Su identificador es {campaign_id}."
    )


@tool(parse_docstring=True)
async def send_campaign(campaign_id: str, config: RunnableConfig) -> str:
    """Lanza el envio de una campana ya creada.

    Args:
        campaign_id: Identificador de la campana a enviar.
    """
    client_id = _client_id(config)

    try:
        campana_uuid = UUID(campaign_id)
    except ValueError:
        return f"Identificador de campana invalido: {campaign_id}."

    async with tenant_session(client_id) as session:
        campana = (
            await session.execute(
                select(Campaign).where(Campaign.id == campana_uuid, Campaign.client_id == client_id)
            )
        ).scalar_one_or_none()

        if campana is None:
            return f"No encontre la campana {campaign_id}."
        if campana.status not in CAMPAIGN_LANZABLE:
            return (
                f"La campana '{campana.name}' esta en estado {campana.status} "
                "y ya no se puede lanzar."
            )

        duplicada = await campana_duplicada(session, client_id, campana)

        if duplicada is not None:
            return (
                f"Ese mismo segmento ya recibio la campana '{duplicada.name}' en las ultimas "
                f"{VENTANA_ANTIDUPLICADOS_HORAS} horas. No se envia otra para no saturar a los "
                "contactos."
            )

        nombre = campana.name
        objetivo = campana.target_count

    # Fuera de la transaccion: el worker abre su propia conexion y necesita ver
    # la campana (mismo motivo que la ingesta de documentos).
    from app.tasks.campaign_tasks import execute_campaign

    execute_campaign.delay(str(client_id), campaign_id)

    return (
        f"Campana '{nombre}' encolada para envio a {objetivo} contacto(s). "
        "El envio va por lotes; consulta las metricas para ver el avance."
    )


@tool(parse_docstring=True)
async def get_campaign_metrics(campaign_id: str, config: RunnableConfig) -> str:
    """Consulta el avance y los resultados de una campana.

    Args:
        campaign_id: Identificador de la campana.
    """
    client_id = _client_id(config)

    try:
        campana_uuid = UUID(campaign_id)
    except ValueError:
        return f"Identificador de campana invalido: {campaign_id}."

    async with tenant_session(client_id) as session:
        campana = (
            await session.execute(
                select(Campaign).where(Campaign.id == campana_uuid, Campaign.client_id == client_id)
            )
        ).scalar_one_or_none()

        if campana is None:
            return f"No encontre la campana {campaign_id}."

        partes = [
            f"Campana '{campana.name}' ({campana.channel}): estado {campana.status}.",
            f"Objetivo {campana.target_count}, enviados {campana.delivered_count}, "
            f"fallidos {campana.failed_count}.",
        ]
        if campana.read_count or campana.replied_count:
            partes.append(f"Leidos {campana.read_count}, respondieron {campana.replied_count}.")
        if campana.started_at:
            partes.append(f"Empezo el {campana.started_at:%d/%m/%Y %H:%M} UTC.")
        if campana.error_log:
            partes.append(f"Errores registrados: {len(campana.error_log)}.")
        return " ".join(partes)


#: Tools que el nodo de marketing le ofrece al LLM.
MARKETING_TOOLS = [segment_contacts, create_campaign, send_campaign, get_campaign_metrics]
