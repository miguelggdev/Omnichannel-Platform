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

Campanas programadas
---------------------
`bulk_dispatch_scheduled_campaigns` corre cada minuto por Beat y encola las
campanas `scheduled` cuya `scheduled_for` ya llego. Tambien recupera la que se
confirmo para "ahora" y cuya task se perdio (worker caido antes de arrancarla):
sin esta pasada quedaba en `scheduled` para siempre, sin poder reenviarse ni
borrarse. Encolar dos veces la misma campana es inocuo: `_marcar_en_envio()`
la bloquea con `FOR UPDATE` y solo una task la pasa a `sending`.

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
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.nodes._tenant import (
    ChannelNotConfiguredError,
    ContactIdentifierNotFoundError,
    get_channel_config,
)
from app.core.config import get_settings
from app.core.database import run_isolated, tenant_session
from app.core.events import EVENT_MESSAGE_SENT, EventEmitter
from app.core.metrics import record_message
from app.models.campaign import (
    CAMPAIGN_COMPLETED,
    CAMPAIGN_FAILED,
    CAMPAIGN_SCHEDULED,
    CAMPAIGN_SENDING,
    MAX_ERRORES_GUARDADOS,
    Campaign,
)
from app.models.contact import Contact
from app.models.contact_identifier import ContactIdentifier
from app.services.campaigns import (
    VENTANA_ANTIDUPLICADOS_HORAS,
    bloquear_campana,
    campana_duplicada,
    debe_salir_ya,
    plantilla_aprobada,
    serializar_lanzamientos,
)
from app.services.messaging.base import MessageContent
from app.services.messaging.factory import get_messaging_provider
from app.services.segmentation import CriterioInvalidoError, resolver_segmento
from app.tasks.auto_close import _load_active_client_ids

# Las dos reglas (plantilla aprobada, anti-duplicado) se siguen evaluando aca,
# en el chokepoint por el que pasa cualquier envio; lo que ya no se repite es la
# consulta. Vive en `services/campaigns.py`, la capa de la que dependen tanto
# este modulo como las tools, asi que sigue sin encadenar tasks con LangChain.


class CampanaNoEnviableError(RuntimeError):
    """La campana no se puede lanzar tal como esta configurada ahora mismo.

    No es un fallo transitorio: reintentar con la misma configuracion da el
    mismo resultado. `_ejecutar()` la trata igual que `CriterioInvalidoError`.
    """


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

    Solo desde `scheduled` (confirmada) y con la fila bloqueada
    (`bloquear_campana()`, `SELECT ... FOR UPDATE`): dos tasks de la misma
    campana — doble clic en `/send`, la tool y el CRUD a la vez, la pasada de
    Beat sobre una que ya estaba en cola — se serializan, y la segunda lee la
    fila ya en `sending` y no la vuelve a enviar. Antes era un SELECT sin
    candado: las dos leian `scheduled`, las dos escribian `sending` y el
    segmento recibia el mensaje dos veces.

    Una campana cuya `scheduled_for` todavia no llego se deja como esta: la
    saca `bulk_dispatch_scheduled_campaigns` cuando sea la hora.

    Vuelve a comprobar la plantilla aprobada y el anti-duplicado de 24h aca,
    no solo en `marketing_tools.create_campaign()`/`send_campaign()`: esas dos
    tools son el unico camino de hoy hacia `execute_campaign.delay()`, pero el
    modelo ya tiene `scheduled_for`/`CAMPAIGN_SCHEDULED` pensando en un
    scheduler futuro que llamaria a la task directo, saltandose las tools por
    completo. Sin este chequeo aca, ese scheduler mandaria una plantilla que
    Meta ya no aprueba, o repetiria el mismo segmento sin ningun freno.
    Hallazgo de /code-review sobre el PR #42.

    Args:
        client_id: Tenant dueno de la campana.
        campaign_id: Campana a lanzar.

    Returns:
        Una copia de la campana con lo que hace falta para enviarla, o None si
        ya no es lanzable (otra ejecucion se le adelanto, o la cancelaron).

    Raises:
        CampanaNoEnviableError: Si la plantilla ya no esta aprobada o el mismo
            segmento/canal ya recibio una campana en las ultimas 24 horas.
    """
    async with tenant_session(client_id) as session:
        # Primero el lock del tenant y despues el de la fila, siempre en ese
        # orden: ver `serializar_lanzamientos()`.
        await serializar_lanzamientos(session, client_id)
        campana = await bloquear_campana(session, client_id, campaign_id)

        if campana is None:
            logger.warning("Campana %s no encontrada para el tenant %s", campaign_id, client_id)
            return None
        if campana.status != CAMPAIGN_SCHEDULED:
            # `draft` no esta confirmada; cualquier otro estado ya paso por
            # `sending` (reentrega con acks_late, o una task duplicada).
            logger.info("Campana %s en estado %s; no se lanza", campaign_id, campana.status)
            return None
        if not debe_salir_ya(campana):
            logger.info(
                "Campana %s programada para %s; todavia no sale",
                campaign_id,
                campana.scheduled_for,
            )
            return None

        if campana.channel == "whatsapp" and not await plantilla_aprobada(
            session, client_id, campana.message_template
        ):
            raise CampanaNoEnviableError(
                "La plantilla de WhatsApp ya no esta aprobada para este tenant."
            )

        duplicada = await campana_duplicada(session, client_id, campana)
        if duplicada is not None:
            raise CampanaNoEnviableError(
                f"El mismo segmento y canal ya recibio la campana '{duplicada.name}' "
                f"en las ultimas {VENTANA_ANTIDUPLICADOS_HORAS} horas."
            )

        campana.status = CAMPAIGN_SENDING
        campana.started_at = datetime.now(timezone.utc)
        # flush() antes de expunge_all(), no al reves: expunge() saca el objeto
        # del unit-of-work de la sesion, y el commit() que hace tenant_session()
        # al salir del `async with` solo escribe lo que sigue *adentro* de esa
        # sesion. Sin este flush, el UPDATE de status/started_at se pierde en
        # silencio — la campana nunca queda realmente en `sending` en la base,
        # y ni la reentrega del broker ni un segundo llamado la detectan como
        # ya lanzada. Hallazgo de /code-review sobre el PR #42.
        await session.flush()
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


async def _resolver_identificadores(
    session: AsyncSession, client_id: UUID, contact_ids: list[UUID], channel: str
) -> dict[UUID, str]:
    """Trae de una sola consulta el identificador de canal de varios contactos.

    `get_contact_identifier()` (`app/agents/nodes/_tenant.py`) resuelve un
    contacto a la vez y abre su propia `tenant_session()` — bien para una
    respuesta puntual, pero repetido por cada contacto de una campana masiva
    son N transacciones completas contra el pooler (N idas y vueltas a
    Supabase) en el camino caliente del envio. Hallazgo de /code-review sobre
    el PR #42. Se llama una sola vez, dentro de la misma sesion corta que ya
    abre `resolver_segmento()`.

    Args:
        session: Sesion con contexto de tenant ya aplicado.
        client_id: Tenant propietario.
        contact_ids: Contactos del segmento.
        channel: Canal de la campana.

    Returns:
        Mapa `contact_id -> identifier_value`. Un contacto sin identificador
        para este canal simplemente no aparece.
    """
    if not contact_ids:
        return {}
    stmt = select(ContactIdentifier.contact_id, ContactIdentifier.identifier_value).where(
        ContactIdentifier.client_id == client_id,
        ContactIdentifier.contact_id.in_(contact_ids),
        ContactIdentifier.channel == channel,
    )
    filas = (await session.execute(stmt)).all()
    return {contact_id: str(valor) for contact_id, valor in filas}


async def _enviar_a_contacto(
    client_id: UUID,
    contacto: Contact,
    campana: Campaign,
    provider: Any,
    channel_config: Any,
    identificador: str | None,
) -> None:
    """Manda el mensaje de la campana a un contacto.

    No pasa por `deliver_message()` (`app/agents/nodes/_delivery.py`): esa
    funcion tambien persiste en `messages`, y una campana no abre conversacion
    por contacto (ver docstring del modulo). Pero las metricas y el evento de
    dominio si aplican igual que a cualquier otro mensaje saliente —
    omitirlos no es parte de esa decision, era un efecto colateral de saltarse
    `deliver_message()` entero. Hallazgo de /code-review sobre el PR #42:
    sin esto, un dashboard de volumen por canal subcuenta cada campana, y un
    tenant con un webhook saliente suscrito a `message.sent` (Sprint 11) nunca
    se entera de que se mando una campana.

    Args:
        client_id: Tenant dueno del contacto.
        contacto: Destinatario.
        campana: Campana en curso.
        provider: Proveedor de mensajeria del canal.
        channel_config: Credenciales del canal.
        identificador: Identificador del contacto en el canal de la campana,
            ya resuelto por `_resolver_identificadores()`; `None` si no tiene.

    Raises:
        ContactIdentifierNotFoundError: Si el contacto no tiene identificador
            en el canal de la campana; lo maneja quien llama, que lo cuenta
            como fallido y sigue con el resto del segmento.
    """
    if identificador is None:
        raise ContactIdentifierNotFoundError(
            f"El contacto {contacto.id} no tiene identificador en el canal {campana.channel}"
        )
    texto = resolver_plantilla(campana.message_template, contacto)
    external_id = await provider.send_message(
        to=identificador,
        content=MessageContent(text=texto),
        channel_config=channel_config,
    )

    record_message(str(client_id), campana.channel, "outbound")
    await EventEmitter.emit(
        EVENT_MESSAGE_SENT,
        client_id,
        {
            "campaign_id": str(campana.id),
            "contact_id": str(contacto.id),
            "channel": campana.channel,
            "content": texto,
            "direction": "outgoing",
            "external_message_id": external_id or None,
        },
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

    por_segundo = max(1, get_settings().CAMPAIGN_MAX_MESSAGES_PER_SECOND)
    enviados = 0
    fallidos = 0
    errores: list[dict[str, str]] = []

    try:
        # Adentro del try: si la plantilla ya no esta aprobada o el segmento
        # esta duplicado, _marcar_en_envio() levanta CampanaNoEnviableError, y se
        # maneja igual que CriterioInvalidoError/ChannelNotConfiguredError mas
        # abajo (marca CAMPAIGN_FAILED con el motivo, no reintenta).
        campana = await _marcar_en_envio(client_id, campaign_id)
        if campana is None:
            return {"status": "skipped"}

        async with tenant_session(client_id) as session:
            contactos = await resolver_segmento(session, client_id, campana.segment_criteria)
            identificadores = await _resolver_identificadores(
                session, client_id, [c.id for c in contactos], campana.channel
            )
            session.expunge_all()

        await _guardar_progreso(client_id, campaign_id, target_count=len(contactos))

        provider_name, channel_config = get_channel_config(campana.channel)
        provider = get_messaging_provider(provider_name, {"channel": campana.channel})

        for inicio in range(0, len(contactos), por_segundo):
            lote = contactos[inicio : inicio + por_segundo]
            comienzo = time.monotonic()

            for contacto in lote:
                try:
                    await _enviar_a_contacto(
                        client_id,
                        contacto,
                        campana,
                        provider,
                        channel_config,
                        identificadores.get(contacto.id),
                    )
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

    except (CriterioInvalidoError, ChannelNotConfiguredError, CampanaNoEnviableError) as exc:
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


async def _campanas_vencidas(client_id: UUID) -> list[UUID]:
    """Campanas `scheduled` de un tenant a las que ya les llego la hora.

    Args:
        client_id: Tenant a barrer.

    Returns:
        Ids de las campanas a encolar.
    """
    async with tenant_session(client_id) as session:
        filas = await session.execute(
            select(Campaign.id).where(
                Campaign.client_id == client_id,
                Campaign.status == CAMPAIGN_SCHEDULED,
                Campaign.scheduled_for <= datetime.now(timezone.utc),
            )
        )
        return [campaign_id for (campaign_id,) in filas.all()]


async def _despachar_programadas() -> dict[str, int]:
    """Encola las campanas programadas que ya tienen que salir, en todos los tenants.

    Mismo patron que `auto_close._auto_close()`: un tenant con error no corta
    el recorrido de los demas.

    Returns:
        Cuantas campanas se encolaron y cuantos tenants se recorrieron.
    """
    client_ids = await _load_active_client_ids()
    encoladas = 0
    tenants_con_error = 0

    for client_id in client_ids:
        try:
            for campaign_id in await _campanas_vencidas(client_id):
                execute_campaign.delay(str(client_id), str(campaign_id))
                encoladas += 1
        except Exception:
            tenants_con_error += 1
            logger.exception("Despacho de campanas programadas fallido para %s", client_id)

    if encoladas:
        logger.info("%d campana(s) programada(s) encolada(s)", encoladas)
    return {"enqueued": encoladas, "tenants": len(client_ids), "tenants_failed": tenants_con_error}


@shared_task(
    name="app.tasks.bulk_dispatch_scheduled_campaigns",
    queue="bulk",
    acks_late=True,
    time_limit=120,
    soft_time_limit=100,
)
def dispatch_scheduled_campaigns() -> dict[str, int]:
    """Encola las campanas `scheduled` cuya hora de salida ya llego.

    Corre por Celery Beat cada minuto. No reintenta: la siguiente pasada
    vuelve a encontrar las mismas filas mientras sigan en `scheduled`.

    Returns:
        Conteo de campanas encoladas y tenants recorridos.
    """
    return run_isolated(_despachar_programadas())


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
