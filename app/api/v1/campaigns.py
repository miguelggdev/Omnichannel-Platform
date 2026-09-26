"""CRUD de campanas de marketing (Sprint 12, Dev B).

GET    /api/v1/campaigns             lista paginada, filtrable por estado
POST   /api/v1/campaigns             alta en estado `draft`, con el segmento ya contado
GET    /api/v1/campaigns/{id}        detalle con los contadores del envio
POST   /api/v1/campaigns/{id}/send   encola el envio en la cola `bulk`
DELETE /api/v1/campaigns/{id}        borra, solo en `draft`

El path es `/api/v1/campaigns` y no cae bajo `WEBHOOK_PATHS_PREFIX`
(`/api/v1/webhooks/`), la trampa que casi deja sin autenticacion al CRUD del
Sprint 11: cualquier ruta bajo ese prefijo se salta el JWT porque ahi vive el
receptor de webhooks entrantes, que se autentica por HMAC.

Este modulo administra; quien envia es `app.tasks.bulk_execute_campaign`
(Dev A, ADR-067). Las dos reglas que protegen de un envio indebido — plantilla
de WhatsApp aprobada y anti-duplicado de 24 horas — se comprueban aca para
poder responder 400 en el acto, y **se vuelven a comprobar** dentro de la task,
que es el chokepoint real: entre este endpoint y el arranque del worker pasa
tiempo, y en ese rato el tenant puede haber lanzado la misma campana por la
tool del agente.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import NOT_FOUND, VALIDATION_ERROR, AppException
from app.models.campaign import CAMPAIGN_DRAFT, CAMPAIGN_SCHEDULED, CAMPAIGN_STATUSES, Campaign
from app.schemas.campaign import (
    CANALES_VALIDOS,
    CampaignCreate,
    CampaignListResponse,
    CampaignResponse,
    CampaignSendResponse,
    estado_invalido,
)
from app.services.campaigns import (
    VENTANA_ANTIDUPLICADOS_HORAS,
    bloquear_campana,
    campana_duplicada,
    debe_salir_ya,
    plantilla_aprobada,
)
from app.services.segmentation import CriterioInvalidoError, contar_segmento

logger = logging.getLogger(__name__)

router = APIRouter()

#: Una campana masiva la lanza un administrador, no un agente de atencion.
_ROLES = ("super_admin", "admin")


async def _campana_o_404(
    session: AsyncSession, campaign_id: UUID, client_id: UUID, bloquear: bool = False
) -> Campaign:
    """Carga una campana del tenant activo o levanta 404.

    El `client_id` va explicito en el WHERE ademas de RLS, que es la convencion
    del repo desde la revision del PR #43.

    Args:
        session: Sesion con el contexto de tenant ya aplicado.
        campaign_id: Campana buscada.
        client_id: Tenant activo.
        bloquear: Si se lee con `FOR UPDATE` porque se va a cambiar su estado
            (ver `services/campaigns.py::bloquear_campana`).

    Returns:
        La campana.

    Raises:
        AppException: 404 si no existe en este tenant.
    """
    if bloquear:
        campana = await bloquear_campana(session, client_id, campaign_id)
    else:
        campana = (
            await session.execute(
                select(Campaign).where(Campaign.id == campaign_id, Campaign.client_id == client_id)
            )
        ).scalar_one_or_none()

    if campana is None:
        raise AppException(status_code=404, error_code=NOT_FOUND, message="Campana no encontrada")
    return campana


@router.get("", response_model=CampaignListResponse)
async def list_campaigns(
    status: str | None = Query(default=None, description="Filtra por estado"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> CampaignListResponse:
    """Lista las campanas del tenant, de la mas reciente a la mas vieja.

    Args:
        status: Estado por el que filtrar, opcional.
        page: Pagina, desde 1.
        page_size: Tamano de pagina.
        user: Usuario autenticado.

    Returns:
        La pagina de campanas.

    Raises:
        AppException: 400 si el estado del filtro no existe.
    """
    client_id: UUID = user["client_id"]

    if status is not None and estado_invalido(status):
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message=f"Estado invalido: {status}. Validos: {', '.join(CAMPAIGN_STATUSES)}",
        )

    filtros = [Campaign.client_id == client_id]
    if status is not None:
        filtros.append(Campaign.status == status)

    async with tenant_session(client_id) as session:
        total = await session.scalar(select(func.count()).select_from(Campaign).where(*filtros))
        campanas = (
            (
                await session.execute(
                    select(Campaign)
                    .where(*filtros)
                    .order_by(Campaign.created_at.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )

        return CampaignListResponse(
            items=[CampaignResponse.model_validate(c) for c in campanas],
            total=int(total or 0),
            page=page,
            page_size=page_size,
        )


@router.post("", status_code=201, response_model=CampaignResponse)
async def create_campaign(
    data: CampaignCreate,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> CampaignResponse:
    """Crea una campana en estado `draft` y cuenta su segmento.

    Los criterios se resuelven ya mismo contra la base para dos cosas: dejar
    `target_count` cargado, y **rechazar un criterio que no se entienda** en vez
    de descubrirlo cuando el worker la ejecute. Un criterio desconocido que se
    ignora en silencio manda la campana a mas gente de la que el tenant creia
    haber elegido (ver el docstring de `services/segmentation.py`).

    Args:
        data: Datos de la campana.
        user: Usuario autenticado.

    Returns:
        La campana creada.

    Raises:
        AppException: 400 si el canal o los criterios no son validos.
    """
    client_id: UUID = user["client_id"]

    if data.channel not in CANALES_VALIDOS:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message=f"Canal invalido: {data.channel}. Validos: {', '.join(CANALES_VALIDOS)}",
        )

    async with tenant_session(client_id) as session:
        try:
            objetivo = await contar_segmento(session, client_id, data.segment_criteria)
        except CriterioInvalidoError as exc:
            raise AppException(
                status_code=400, error_code=VALIDATION_ERROR, message=str(exc)
            ) from exc

        campana = Campaign(
            client_id=client_id,
            name=data.name,
            channel=data.channel,
            segment_criteria=data.segment_criteria,
            message_template=data.message_template,
            status=CAMPAIGN_DRAFT,
            target_count=objetivo,
            scheduled_for=data.scheduled_for,
            created_by=user.get("user_id"),
            # Explicitos y no por `server_default`: una campana recien creada
            # tiene cero de todo sin ambiguedad, y asi la respuesta es correcta
            # sin depender de que el refresh() traiga los defaults de vuelta.
            delivered_count=0,
            read_count=0,
            replied_count=0,
            failed_count=0,
            error_log=[],
        )
        session.add(campana)
        await session.flush()
        await session.refresh(campana)
        respuesta = CampaignResponse.model_validate(campana)

    logger.info(
        "Campana %s creada para el tenant %s: %d contacto(s) en el segmento",
        respuesta.id,
        client_id,
        objetivo,
    )
    return respuesta


@router.get("/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(
    campaign_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> CampaignResponse:
    """Devuelve una campana con sus contadores de envio.

    Args:
        campaign_id: Campana buscada.
        user: Usuario autenticado.

    Returns:
        La campana.

    Raises:
        AppException: 404 si no existe en este tenant.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        campana = await _campana_o_404(session, campaign_id, client_id)
        return CampaignResponse.model_validate(campana)


@router.post("/{campaign_id}/send", status_code=202, response_model=CampaignSendResponse)
async def send_campaign(
    campaign_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> CampaignSendResponse:
    """Confirma el envio de una campana y, si ya es la hora, lo encola en `bulk`.

    La campana se lee con `FOR UPDATE`: dos `/send` concurrentes (un doble
    clic) se serializan y el segundo la encuentra ya en `scheduled` y responde
    400, en vez de encolar un segundo envio al mismo segmento.

    Con `scheduled_for` en el futuro no se encola nada: la campana queda en
    `scheduled` y la saca `bulk_dispatch_scheduled_campaigns` cuando llegue la
    hora. Sin fecha, se fija `scheduled_for` en ahora, que ademas deja que esa
    misma pasada de Beat la recupere si esta task se pierde.

    El `status` se commitea **antes** de encolar, no despues: el worker de
    `bulk` puede levantar el mensaje apenas se publica, y si todavia viera la
    campana en `draft` con la transaccion sin cerrar, `_marcar_en_envio()`
    trabajaria sobre un estado que este endpoint esta por pisar.

    Args:
        campaign_id: Campana a enviar.
        user: Usuario autenticado.

    Returns:
        Acuse con el estado y el tamano del segmento.

    Raises:
        AppException: 404 si no existe; 400 si no esta en `draft`, si la
            plantilla de WhatsApp no esta aprobada o si el mismo segmento ya
            recibio una campana en las ultimas 24 horas.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        campana = await _campana_o_404(session, campaign_id, client_id, bloquear=True)

        if campana.status != CAMPAIGN_DRAFT:
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message=(
                    f"La campana esta en estado '{campana.status}'; "
                    f"solo se puede enviar desde '{CAMPAIGN_DRAFT}'"
                ),
            )

        if campana.channel == "whatsapp" and not await plantilla_aprobada(
            session, client_id, campana.message_template
        ):
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message=(
                    "La plantilla no esta en las plantillas de WhatsApp aprobadas del tenant "
                    "(agent_configs.config.marketing.approved_templates)"
                ),
            )

        duplicada = await campana_duplicada(session, client_id, campana)
        if duplicada is not None:
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message=(
                    f"El mismo segmento y canal ya recibio la campana '{duplicada.name}' "
                    f"en las ultimas {VENTANA_ANTIDUPLICADOS_HORAS} horas"
                ),
            )

        campana.status = CAMPAIGN_SCHEDULED
        if campana.scheduled_for is None:
            campana.scheduled_for = datetime.now(timezone.utc)
        objetivo = campana.target_count
        programada = campana.scheduled_for
        sale_ya = debe_salir_ya(campana)

    if sale_ya:
        # Fuera de la transaccion, ya commiteada: el worker abre su propia
        # conexion y tiene que poder ver la campana en `scheduled`.
        from app.tasks.campaign_tasks import execute_campaign

        execute_campaign.delay(str(client_id), str(campaign_id))
        logger.info("Campana %s encolada para %d contacto(s)", campaign_id, objetivo)
    else:
        logger.info(
            "Campana %s programada para %s (%d contacto(s))", campaign_id, programada, objetivo
        )

    return CampaignSendResponse(
        campaign_id=campaign_id,
        status=CAMPAIGN_SCHEDULED,
        target_count=objetivo,
        scheduled_for=programada,
    )


@router.delete("/{campaign_id}", status_code=204)
async def delete_campaign(
    campaign_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> None:
    """Borra una campana que todavia no salio.

    Solo en `draft`: una campana que ya se envio es el unico rastro de a quien
    se le mando que, y borrarla dejaria sin explicacion los mensajes que los
    contactos ya recibieron.

    Args:
        campaign_id: Campana a borrar.
        user: Usuario autenticado.

    Raises:
        AppException: 404 si no existe; 400 si ya no esta en `draft`.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        # Con candado: sin el, un `/send` concurrente podia confirmar la
        # campana y encolarla justo mientras este DELETE la borraba.
        campana = await _campana_o_404(session, campaign_id, client_id, bloquear=True)

        if campana.status != CAMPAIGN_DRAFT:
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message=(
                    f"Solo se pueden borrar campanas en estado '{CAMPAIGN_DRAFT}'; "
                    f"esta en '{campana.status}'"
                ),
            )

        await session.delete(campana)

    logger.info("Campana %s borrada del tenant %s", campaign_id, client_id)
