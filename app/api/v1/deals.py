"""API de deals: oportunidades de venta con valor y probabilidad (Sprint 19, Dev B).

    GET    /api/v1/deals                lista con filtros y paginacion
    POST   /api/v1/deals                abre un deal sobre un lead
    GET    /api/v1/deals/pipeline       pipeline por etapa y moneda (abiertos; `include_closed`)
    GET    /api/v1/deals/{id}           detalle
    PUT    /api/v1/deals/{id}           edita titulo, importe, responsable... (no la etapa)
    PATCH  /api/v1/deals/{id}/stage     mueve de etapa (cerrar = ganar o perder)
    DELETE /api/v1/deals/{id}           borra un deal ABIERTO

Reglas (las del servicio de Dev A, `deals.py`/`deal_pipeline.py`):

- Un deal nace abierto y se **cierra** cambiando su etapa; de cerrado solo se sale **reabriendo**.
- Ganar convierte al lead; reabrir el unico ganado lo devuelve a `active`.
- **Nunca se suman monedas distintas:** el pipeline va por etapa **y** moneda.
- **No se borra un deal cerrado:** la historia de ingresos no se reescribe (se reabre y se edita,
  y queda en el historial del lead). Borrar es de `supervisor` para arriba.
- Un `agent` solo toca deals suyos o sin responsable; `supervisor` y `admin` cualquiera.
- Al ganar un deal grande sale una alerta por Telegram (`deal_alerts`), despues del commit.
"""

import logging
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.dependencies import require_leads_module
from app.core.exceptions import (
    CONFLICT,
    FORBIDDEN,
    NOT_FOUND,
    VALIDATION_ERROR,
    AppException,
)
from app.models.deal import DEAL_WON, OPEN_STAGES, Deal
from app.models.lead import Lead
from app.schemas.common import PaginatedResponse
from app.schemas.deal import (
    DealCreate,
    DealResponse,
    DealStage,
    DealStageChange,
    DealUpdate,
    PipelineStageSummary,
)
from app.services.deal_alerts import alertar_deal_ganado
from app.services.deal_pipeline import TransicionInvalidaError
from app.services.deals import (
    DealError,
    actualizar_deal,
    cambiar_etapa,
    crear_deal,
    resumen_pipeline,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_READ_ROLES = ("super_admin", "admin", "supervisor", "agent")
_WRITE_ROLES = ("super_admin", "admin", "supervisor", "agent")
_DELETE_ROLES = ("super_admin", "admin", "supervisor")

_ESTADO_HTTP: dict[str, int] = {
    "lead_unavailable": 409,
    "lead_mismatch": 400,
    "user_unavailable": 400,
    "same_stage": 409,
    "closed_to_closed": 409,
    "unknown_stage": 400,
}


def _uid(user: dict[str, Any]) -> UUID:
    return UUID(str(user["user_id"]))


def _como_http(exc: DealError | TransicionInvalidaError) -> AppException:
    """Traduce un error del servicio de deals a la respuesta de la API."""
    estado = _ESTADO_HTTP.get(exc.code, 409)
    codigo = VALIDATION_ERROR if estado == 400 else CONFLICT
    return AppException(status_code=estado, error_code=codigo, message=str(exc))


async def _deal_o_404(
    session: AsyncSession, deal_id: UUID, user: dict[str, Any], *, bloquear: bool = False
) -> Deal:
    """Un deal del tenant sobre el que este usuario puede actuar.

    Un `agent` solo actua sobre deals suyos o sin responsable.

    Raises:
        AppException: 404 si no existe; 403 si es de otro y el usuario es `agent`.
    """
    client_id: UUID = user["client_id"]
    consulta = select(Deal).where(Deal.id == deal_id, Deal.client_id == client_id)
    if bloquear:
        consulta = consulta.with_for_update()
    deal: Deal | None = (await session.execute(consulta)).scalar_one_or_none()
    if deal is None:
        raise AppException(status_code=404, error_code=NOT_FOUND, message="Deal no encontrado")
    if user["role"] == "agent" and deal.assigned_user_id not in (None, _uid(user)):
        raise AppException(
            status_code=403,
            error_code=FORBIDDEN,
            message="Un agente solo puede actuar sobre deals suyos o sin responsable",
        )
    return deal


@router.get("", response_model=PaginatedResponse[DealResponse])
async def list_deals(
    stage: DealStage | None = None,
    lead_id: UUID | None = None,
    assigned_user_id: UUID | None = None,
    currency: str | None = Query(default=None, min_length=3, max_length=3),
    close_from: date | None = None,
    close_to: date | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> PaginatedResponse[DealResponse]:
    """Lista los deals del tenant, los mas recientes primero.

    Args:
        stage: Solo esta etapa.
        lead_id: Solo los de este lead.
        assigned_user_id: Solo los de este responsable.
        currency: Solo esta moneda (ISO).
        close_from: Cierre real (o previsto, si siguen abiertos) desde este dia.
        close_to: Idem, hasta este dia.
        page: Pagina, desde 1.
        page_size: Tamano de pagina, hasta 100.
        user: Usuario autenticado.

    Returns:
        La pagina.
    """
    client_id: UUID = user["client_id"]
    filtros: list[Any] = [Deal.client_id == client_id]
    if stage is not None:
        filtros.append(Deal.stage == stage)
    if lead_id is not None:
        filtros.append(Deal.lead_id == lead_id)
    if assigned_user_id is not None:
        filtros.append(Deal.assigned_user_id == assigned_user_id)
    if currency is not None:
        filtros.append(Deal.currency == currency.upper())
    fecha = func.coalesce(Deal.actual_close_date, Deal.expected_close_date)
    if close_from is not None:
        filtros.append(fecha >= close_from)
    if close_to is not None:
        filtros.append(fecha <= close_to)
    async with tenant_session(client_id) as session:
        total = (
            await session.execute(select(func.count()).select_from(Deal).where(*filtros))
        ).scalar_one()
        filas = (
            (
                await session.execute(
                    select(Deal)
                    .where(*filtros)
                    .order_by(Deal.created_at.desc(), Deal.id)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return PaginatedResponse[DealResponse](
            items=[DealResponse.model_validate(d) for d in filas],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=(total + page_size - 1) // page_size,
        )


@router.post("", response_model=DealResponse, status_code=201)
async def create_deal(
    data: DealCreate,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> DealResponse:
    """Abre un deal sobre un lead.

    Args:
        data: Datos del deal (nace abierto).
        user: Usuario autenticado.

    Returns:
        El deal creado.

    Raises:
        AppException: 404 si el lead no existe; 403 si un agente abre un deal sobre un lead de
            otro o lo asigna a otro; 409 si el lead esta borrado o suprimido; 400 si el
            responsable no es valido.
    """
    client_id: UUID = user["client_id"]
    uid = _uid(user)
    async with tenant_session(client_id, user_id=uid) as session:
        lead: Lead | None = (
            await session.execute(
                select(Lead).where(
                    Lead.id == data.lead_id, Lead.client_id == client_id, Lead.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if lead is None:
            raise AppException(status_code=404, error_code=NOT_FOUND, message="Lead no encontrado")
        if user["role"] == "agent" and (
            lead.assigned_user_id not in (None, uid) or data.assigned_user_id not in (None, uid)
        ):
            raise AppException(
                status_code=403,
                error_code=FORBIDDEN,
                message="Un agente solo abre deals sobre leads suyos o sin responsable, para si",
            )
        try:
            deal = await crear_deal(
                session, lead, data, ahora=datetime.now(timezone.utc), user_id=uid
            )
        except DealError as exc:
            raise _como_http(exc) from exc
        await session.refresh(deal)
        return DealResponse.model_validate(deal)


@router.get("/pipeline", response_model=list[PipelineStageSummary])
async def get_pipeline(
    include_closed: bool = False,
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> list[PipelineStageSummary]:
    """El pipeline agregado por etapa **y moneda** (nunca se suman monedas distintas).

    Args:
        include_closed: Incluir tambien `closed_won` y `closed_lost` (por defecto, solo abiertos).
        user: Usuario autenticado.

    Returns:
        Una fila por (etapa, moneda) con deals, en el orden del embudo.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        resumen = await resumen_pipeline(
            session, client_id, ahora=datetime.now(timezone.utc), solo_abiertos=not include_closed
        )
    return [
        PipelineStageSummary(
            stage=f.stage,
            currency=f.currency,
            count=f.count,
            total_value=f.total_value,
            weighted_value=f.weighted_value,
            avg_days_in_stage=f.avg_days_in_stage,
        )
        for f in resumen
    ]


@router.get("/{deal_id}", response_model=DealResponse)
async def get_deal(
    deal_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> DealResponse:
    """Detalle de un deal.

    Args:
        deal_id: Deal.
        user: Usuario autenticado.

    Returns:
        El deal.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        consulta = await session.execute(
            select(Deal).where(Deal.id == deal_id, Deal.client_id == client_id)
        )
        deal = consulta.scalar_one_or_none()
        if deal is None:
            raise AppException(status_code=404, error_code=NOT_FOUND, message="Deal no encontrado")
        return DealResponse.model_validate(deal)


@router.put("/{deal_id}", response_model=DealResponse)
async def update_deal(
    deal_id: UUID,
    data: DealUpdate,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> DealResponse:
    """Edita los campos que se envien (la etapa se cambia con `PATCH /stage`).

    Un `agent` no puede reasignar un deal a otra persona.

    Args:
        deal_id: Deal.
        data: Campos a cambiar.
        user: Usuario autenticado.

    Returns:
        El deal actualizado.

    Raises:
        AppException: 404 si no existe; 403 si es de otro agente o un agente lo reasigna; 400 si el
            responsable no es valido.
    """
    client_id: UUID = user["client_id"]
    uid = _uid(user)
    async with tenant_session(client_id, user_id=uid) as session:
        deal = await _deal_o_404(session, deal_id, user, bloquear=True)
        if (
            user["role"] == "agent"
            and "assigned_user_id" in data.model_fields_set
            and data.assigned_user_id not in (None, uid)
        ):
            raise AppException(
                status_code=403,
                error_code=FORBIDDEN,
                message="Un agente no puede asignar el deal a otra persona",
            )
        try:
            await actualizar_deal(session, deal, data)
        except DealError as exc:
            raise _como_http(exc) from exc
        await session.flush()
        await session.refresh(deal)
        return DealResponse.model_validate(deal)


@router.patch("/{deal_id}/stage", response_model=DealResponse)
async def change_stage(
    deal_id: UUID,
    data: DealStageChange,
    user: dict[str, Any] = Depends(require_leads_module(*_WRITE_ROLES)),
) -> DealResponse:
    """Mueve el deal de etapa; ganarlo convierte al lead y puede disparar una alerta.

    Args:
        deal_id: Deal.
        data: Etapa de destino, probabilidad (solo abiertas) y motivo (solo al perder).
        user: Usuario autenticado.

    Returns:
        El deal movido.

    Raises:
        AppException: 404 si no existe; 403 si es de otro agente; 409 si la transicion no esta
            permitida (misma etapa, cerrado a cerrado).
    """
    client_id: UUID = user["client_id"]
    uid = _uid(user)
    async with tenant_session(client_id, user_id=uid) as session:
        deal = await _deal_o_404(session, deal_id, user, bloquear=True)
        try:
            await cambiar_etapa(
                session,
                deal,
                data.stage,
                ahora=datetime.now(timezone.utc),
                user_id=uid,
                probabilidad=data.probability,
                motivo_perdida=data.lost_reason,
            )
        except TransicionInvalidaError as exc:
            raise _como_http(exc) from exc
        await session.refresh(deal)
        respuesta = DealResponse.model_validate(deal)
    if respuesta.stage == DEAL_WON:
        # Despues del commit y sin propagar: la alerta no puede costar el cierre del deal.
        await alertar_deal_ganado(client_id, deal_id)
    return respuesta


@router.delete("/{deal_id}", status_code=204)
async def delete_deal(
    deal_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_DELETE_ROLES)),
) -> None:
    """Borra un deal abierto. Un deal cerrado no se borra: la historia de ingresos se conserva.

    Args:
        deal_id: Deal.
        user: Usuario autenticado; supervisor, admin o super_admin.

    Raises:
        AppException: 404 si no existe; 409 si esta cerrado (se reabre y se edita).
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id, user_id=_uid(user)) as session:
        deal = await _deal_o_404(session, deal_id, user, bloquear=True)
        if deal.stage not in OPEN_STAGES:
            raise AppException(
                status_code=409,
                error_code=CONFLICT,
                message="Un deal cerrado no se borra: reabrelo si fue un error",
            )
        await session.delete(deal)
