"""Task de Celery que ejecuta una campana masiva (Sprint 12).

Nombre de la tarea
-------------------
`app.tasks.bulk_execute_campaign`: el routing de `celery_config.py` manda a la
cola `bulk` lo que empieza por `app.tasks.bulk_*`, y esta tarea **tiene** que
ir ahi (nota tecnica del spec) — un envio a 50.000 contactos en la cola de los
mensajes entrantes dejaria sin atender a los clientes reales mientras dura.

Throttling
-----------
Se envia en lotes de `CAMPAIGN_MAX_MESSAGES_PER_SECOND` y cada lote espera a
completar su segundo antes del siguiente, asi que el ritmo nunca supera el
limite configurado. `asyncio.sleep()` cede el control del loop, no bloquea.

Nada dentro de la transaccion
------------------------------
Los envios se hacen con la sesion cerrada: mandar miles de mensajes con una
transaccion abierta retendria una conexion del pooler durante minutos u horas
(la misma leccion de ADR-064/ADR-065). El progreso se persiste cada lote, en
su propia transaccion corta, para que el CRUD de Dev B pueda mostrarlo mientras
la campana corre.

Que NO hace
------------
No escribe los mensajes en `messages`: una campana no abre una conversacion
con cada contacto. Si el contacto responde, el webhook entrante crea la
conversacion como con cualquier otro mensaje. El rastro de la campana son sus
contadores y `error_log`.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from celery import shared_task
from sqlalchemy import select
from sqlalchemy import update as sa_update

from app.agents.nodes._tenant import (
    ChannelNotConfiguredError,
    get_channel_config,
    get_contact_identifier,
)
from app.core.config import get_settings
from app.core.database import run_isolated, tenant_session
from app.models.campaign import (
    CAMPAIGN_COMPLETED,
    CAMPAIGN_FAILED,
    CAMPAIGN_LANZABLE,
    CAMPAIGN_SENDING,
    MAX_ERRORES_GUARDADOS,
    Campaign,
)
from app.models.contact import Contact
from app.services.messaging.base import MessageContent
from app.services.messaging.factory import get_messaging_provider
from app.services.segmentation import CriterioInvalidoError, resolver_segmento

logger = logging.getLogger(__name__)


def resolver_plantilla(plantilla: str, contacto: Contact) -> str:
    """Sustituye las variables de la plantilla con los datos del contacto.

    Args:
        plantilla: Texto con variables `{{contact_name}}`, `{{first_name}}`.
        contacto: Contacto destinatario.

    Returns:
        El texto con las variables resueltas. Una variable sin dato se
        reemplaza por cadena vacia, nunca se deja el `{{...}}` a la vista.
    """
    nombre = (
        contacto.display_name
        or " ".join(filter(None, [contacto.first_name, contacto.last_name])).strip()
    )
    valores = {
        "contact_name": nombre or "",
        "first_name": contacto.first_name or "",
        "last_name": contacto.last_name or "",
    }
    resuelta = plantilla
    for clave, valor in valores.items():
        resuelta = resuelta.replace(f"{{{{{clave}}}}}", valor)
    return resuelta


async def _marcar_en_envio(client_id: UUID, campaign_id: UUID) -> Campaign | None:
    """Pasa la campana a `sending` si todavia se puede lanzar.

    Args:
        client_id: Tenant dueno de la campana.
        campaign_id: Campana a lanzar.

    Returns:
        Una copia de la campana con lo que hace falta para enviarla, o None si
        ya no es lanzable (otra ejecucion se le adelanto, o la cancelaron).
    """
    async with tenant_session(client_id) as session:
        campana = (
            await session.execute(
                select(Campaign).where(Campaign.id == campaign_id, Campaign.client_id == client_id)
            )
        ).scalar_one_or_none()

        if campana is None:
            logger.warning("Campana %s no encontrada para el tenant %s", campaign_id, client_id)
            return None
        if campana.status not in CAMPAIGN_LANZABLE:
            # Una reentrega de la task (acks_late) no vuelve a enviarla: la
            # campana ya paso por `sending`.
            logger.info(
                "Campana %s en estado %s; no se lanza otra vez", campaign_id, campana.status
            )
            return None

        campana.status = CAMPAIGN_SENDING
        campana.started_at = datetime.now(timezone.utc)
        session.expunge_all()
        return campana


async def _guardar_progreso(client_id: UUID, campaign_id: UUID, **campos: Any) -> None:
    """Escribe el avance de la campana en su propia transaccion corta.

    Args:
        client_id: Tenant dueno de la campana.
        campaign_id: Campana a actualizar.
        **campos: Columnas a modificar.
    """
    async with tenant_session(client_id) as session:
        await session.execute(
            sa_update(Campaign)
            .where(Campaign.id == campaign_id, Campaign.client_id == client_id)
            .values(**campos)
            .execution_options(synchronize_session=False)
        )


async def _enviar_a_contacto(
    client_id: UUID, contacto: Contact, campana: Campaign, provider: Any, channel_config: Any
) -> None:
    """Manda el mensaje de la campana a un contacto.

    Args:
        client_id: Tenant dueno del contacto.
        contacto: Destinatario.
        campana: Campana en curso.
        provider: Proveedor de mensajeria del canal.
        channel_config: Credenciales del canal.

    Raises:
        ContactIdentifierNotFoundError: Si el contacto no tiene identificador
            en el canal de la campana; lo maneja quien llama, que lo cuenta
            como fallido y sigue con el resto del segmento.
    """
    identificador = await get_contact_identifier(client_id, contacto.id, campana.channel)
    await provider.send_message(
        to=identificador,
        content=MessageContent(text=resolver_plantilla(campana.message_template, contacto)),
        channel_config=channel_config,
    )


async def _ejecutar(client_id_str: str, campaign_id_str: str) -> dict[str, Any]:
    """Resuelve el segmento y envia la campana con throttling.

    Args:
        client_id_str: Tenant dueno de la campana, serializado.
        campaign_id_str: Campana a ejecutar, serializada.

    Returns:
        Resumen con enviados, fallidos y estado final.
    """
    client_id = UUID(client_id_str)
    campaign_id = UUID(campaign_id_str)

    campana = await _marcar_en_envio(client_id, campaign_id)
    if campana is None:
        return {"status": "skipped"}

    por_segundo = max(1, get_settings().CAMPAIGN_MAX_MESSAGES_PER_SECOND)
    enviados = 0
    fallidos = 0
    errores: list[dict[str, str]] = []

    try:
        async with tenant_session(client_id) as session:
            contactos = await resolver_segmento(session, client_id, campana.segment_criteria)
            session.expunge_all()

        await _guardar_progreso(client_id, campaign_id, target_count=len(contactos))

        provider_name, channel_config = get_channel_config(campana.channel)
        provider = get_messaging_provider(provider_name, {"channel": campana.channel})

        for inicio in range(0, len(contactos), por_segundo):
            lote = contactos[inicio : inicio + por_segundo]
            comienzo = time.monotonic()

            for contacto in lote:
                try:
                    await _enviar_a_contacto(client_id, contacto, campana, provider, channel_config)
                    enviados += 1
                except Exception as exc:
                    # Un contacto que falla no detiene la campana: se anota y
                    # se sigue con el resto del segmento.
                    fallidos += 1
                    if len(errores) < MAX_ERRORES_GUARDADOS:
                        errores.append({"contact_id": str(contacto.id), "error": str(exc)[:300]})

            await _guardar_progreso(
                client_id, campaign_id, delivered_count=enviados, failed_count=fallidos
            )

            transcurrido = time.monotonic() - comienzo
            if transcurrido < 1.0 and inicio + por_segundo < len(contactos):
                await asyncio.sleep(1.0 - transcurrido)

    except (CriterioInvalidoError, ChannelNotConfiguredError) as exc:
        # La campana no se puede ejecutar tal como esta configurada: no es un
        # fallo transitorio y reintentarla daria el mismo resultado.
        logger.warning("Campana %s no ejecutable: %s", campaign_id, exc)
        await _guardar_progreso(
            client_id,
            campaign_id,
            status=CAMPAIGN_FAILED,
            completed_at=datetime.now(timezone.utc),
            error_log=[{"error": str(exc)[:300]}],
        )
        return {"status": CAMPAIGN_FAILED, "error": str(exc)}

    except Exception as exc:
        logger.exception("Fallo inesperado ejecutando la campana %s", campaign_id)
        await _guardar_progreso(
            client_id,
            campaign_id,
            status=CAMPAIGN_FAILED,
            completed_at=datetime.now(timezone.utc),
            delivered_count=enviados,
            failed_count=fallidos,
            error_log=[*errores, {"error": str(exc)[:300]}][:MAX_ERRORES_GUARDADOS],
        )
        raise

    await _guardar_progreso(
        client_id,
        campaign_id,
        status=CAMPAIGN_COMPLETED,
        completed_at=datetime.now(timezone.utc),
        delivered_count=enviados,
        failed_count=fallidos,
        error_log=errores,
    )
    logger.info("Campana %s terminada: %d enviados, %d fallidos", campaign_id, enviados, fallidos)
    return {"status": CAMPAIGN_COMPLETED, "delivered": enviados, "failed": fallidos}


@shared_task(
    name="app.tasks.bulk_execute_campaign",
    bind=True,
    acks_late=True,
    queue="bulk",
    time_limit=7200,
    soft_time_limit=7000,
)
def execute_campaign(self: Any, client_id: str, campaign_id: str) -> dict[str, Any]:
    """Ejecuta una campana masiva respetando el limite de mensajes por segundo.

    No se reintenta sola: un reintento volveria a enviarle a los contactos que
    ya recibieron el mensaje. `_marcar_en_envio()` ademas corta la reentrega
    del broker, porque la campana ya no esta en un estado lanzable.

    Args:
        self: Instancia de la tarea (bind=True).
        client_id: Tenant dueno de la campana, serializado.
        campaign_id: Campana a ejecutar, serializada.

    Returns:
        Resumen del envio.
    """
    return run_isolated(_ejecutar(client_id, campaign_id))
