"""Deals contra la base (Sprint 19, slice Dev A, ADR-087).

Alta, edicion, cambio de etapa (con la sincronizacion del lead y su historial), resumen del
pipeline e ingresos por mes, y lo que el RGPD necesita (export y anonimizacion). Las reglas
puras estan en `app/services/deal_pipeline.py`. Todas las funciones reciben una sesion con el
contexto del tenant fijado (`tenant_session`) y filtran ademas por `client_id` explicito.
"""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deal import DEAL_STAGES, DEAL_WON, OPEN_STAGES, Deal
from app.models.lead import Lead
from app.models.lead_activity import ACTIVITY_DEAL_CREATED, ACTIVITY_DEAL_STAGE_CHANGED
from app.schemas.deal import DealCreate, DealUpdate
from app.services.deal_pipeline import (
    PROBABILIDAD_POR_ETAPA,
    ResumenEtapa,
    aplicar_efecto,
    aplicar_etapa,
    efecto_en_lead,
)
from app.services.lead_activity import registrar_actividad
from app.services.lead_privacy import ANONIMIZADO, lead_esta_anonimizado
from app.services.lead_sequence_timing import ventana_del_tenant
from app.services.tenant_refs import usuario_asignable


class DealError(Exception):
    """Operacion no permitida sobre un deal.

    Attributes:
        code: Codigo estable para la API.
    """

    def __init__(self, code: str, message: str) -> None:
        """Crea el error.

        Args:
            code: Codigo estable (`lead_unavailable`...).
            message: Descripcion (sin datos de la persona).
        """
        super().__init__(message)
        self.code = code


async def _validar_responsable(session: AsyncSession, client_id: UUID, user_id: UUID) -> None:
    """El responsable es un usuario activo de este tenant (la FK no mira la RLS)."""
    if not await usuario_asignable(session, client_id, user_id):
        raise DealError(
            "user_unavailable", "El usuario no existe, esta desactivado o no atiende leads"
        )


def _lead_disponible(lead: Lead) -> None:
    """Un deal no se abre sobre un lead borrado o suprimido."""
    if lead.deleted_at is not None or lead_esta_anonimizado(lead):
        raise DealError("lead_unavailable", "El lead esta borrado o suprimido")


async def crear_deal(
    session: AsyncSession,
    lead: Lead,
    datos: DealCreate,
    *,
    ahora: datetime,
    user_id: UUID | None = None,
) -> Deal:
    """Abre un deal sobre un lead.

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead (ya cargado; `datos.lead_id` debe ser el suyo).
        datos: Datos validados.
        ahora: Instante del alta.
        user_id: Quien lo crea.

    Returns:
        El deal (con `id`).

    Raises:
        DealError: `lead_unavailable` si el lead esta borrado o suprimido; `lead_mismatch` si
            `datos.lead_id` no es el lead dado; `user_unavailable` si el responsable no es un
            usuario activo del tenant.
    """
    if datos.lead_id != lead.id:
        raise DealError("lead_mismatch", "El deal no corresponde a ese lead")
    _lead_disponible(lead)
    if datos.assigned_user_id is not None:
        await _validar_responsable(session, lead.client_id, datos.assigned_user_id)
    deal = Deal(
        client_id=lead.client_id,
        lead_id=lead.id,
        assigned_user_id=datos.assigned_user_id or lead.assigned_user_id,
        title=datos.title,
        value=datos.value,
        currency=datos.currency,
        stage=datos.stage,
        probability=(
            datos.probability
            if datos.probability is not None
            else PROBABILIDAD_POR_ETAPA[datos.stage]
        ),
        expected_close_date=datos.expected_close_date,
        notes=datos.notes,
        stage_changed_at=ahora,
        created_at=ahora,
    )
    session.add(deal)
    await session.flush()
    registrar_actividad(
        session,
        client_id=lead.client_id,
        lead_id=lead.id,
        tipo=ACTIVITY_DEAL_CREATED,
        user_id=user_id,
        deal_id=deal.id,
        stage=deal.stage,
    )
    return deal


async def actualizar_deal(session: AsyncSession, deal: Deal, datos: DealUpdate) -> list[str]:
    """Aplica una edicion (sin cambio de etapa).

    Args:
        session: Sesion con el contexto del tenant fijado.
        deal: Deal (se modifica en sitio).
        datos: Campos enviados.

    Returns:
        Nombres de los campos cambiados.

    Raises:
        DealError: `user_unavailable` si el nuevo responsable no es un usuario activo del tenant.
    """
    cambios = datos.model_dump(exclude_unset=True)
    nuevo_responsable = cambios.get("assigned_user_id")
    if nuevo_responsable is not None and nuevo_responsable != deal.assigned_user_id:
        await _validar_responsable(session, deal.client_id, nuevo_responsable)
    cambiados = []
    for campo, valor in cambios.items():
        if getattr(deal, campo) != valor:
            setattr(deal, campo, valor)
            cambiados.append(campo)
    return cambiados


async def cambiar_etapa(
    session: AsyncSession,
    deal: Deal,
    nueva: str,
    *,
    ahora: datetime,
    user_id: UUID | None = None,
    probabilidad: int | None = None,
    motivo_perdida: str | None = None,
) -> bool:
    """Mueve un deal de etapa, sincroniza su lead y lo anota en el historial del lead.

    Bloquea el lead (`FOR UPDATE`) para que dos deals del mismo lead cerrandose a la vez no
    dejen su estado incoherente.

    Args:
        session: Sesion con el contexto del tenant fijado.
        deal: Deal.
        nueva: Etapa nueva.
        ahora: Instante del cambio.
        user_id: Quien lo mueve.
        probabilidad: Probabilidad explicita (solo en etapas abiertas).
        motivo_perdida: Motivo, al perder.

    Returns:
        Si cambio el estado del lead.

    Raises:
        TransicionInvalidaError: Si la transicion no esta permitida.
    """
    lead = (
        await session.execute(
            select(Lead)
            .where(Lead.client_id == deal.client_id, Lead.id == deal.lead_id)
            .with_for_update()
        )
    ).scalar_one()
    ventana = await ventana_del_tenant(session, deal.client_id)
    anterior = aplicar_etapa(
        deal,
        nueva,
        ahora=ahora,
        zona=ventana.tz.key,
        probabilidad=probabilidad,
        motivo_perdida=motivo_perdida,
    )
    otros_ganados = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Deal)
                .where(
                    Deal.client_id == deal.client_id,
                    Deal.lead_id == deal.lead_id,
                    Deal.id != deal.id,
                    Deal.stage == DEAL_WON,
                )
            )
        ).scalar_one()
    )
    cambio_lead = aplicar_efecto(
        lead, efecto_en_lead(lead, anterior, nueva, ahora=ahora, otros_ganados=otros_ganados)
    )
    registrar_actividad(
        session,
        client_id=deal.client_id,
        lead_id=deal.lead_id,
        tipo=ACTIVITY_DEAL_STAGE_CHANGED,
        user_id=user_id,
        deal_id=deal.id,
        stage_from=anterior,
        stage_to=nueva,
        lead_status=lead.status if cambio_lead else None,
    )
    await session.flush()
    return cambio_lead


async def resumen_pipeline(
    session: AsyncSession, client_id: UUID, *, ahora: datetime, solo_abiertos: bool = True
) -> list[ResumenEtapa]:
    """El pipeline agregado en la base, por etapa y moneda.

    Calcula lo mismo que `deal_pipeline.resumir_pipeline` pero en SQL (un tenant puede tener
    miles de deals). El valor ponderado se redondea por deal, igual que en Python.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        ahora: Instante de referencia para los dias en etapa.
        solo_abiertos: Solo etapas abiertas (el pipeline "vivo").

    Returns:
        Una fila por (etapa, moneda), en el orden del embudo.
    """
    ponderado = func.round(Deal.value * Deal.probability / 100, 2)
    dias = extract("epoch", ahora - Deal.stage_changed_at) / 86400
    consulta = (
        select(
            Deal.stage,
            Deal.currency,
            func.count(),
            func.coalesce(func.sum(Deal.value), 0),
            func.coalesce(func.sum(ponderado), 0),
            func.coalesce(func.sum(func.greatest(dias, 0)), 0),
        )
        .where(Deal.client_id == client_id)
        .group_by(Deal.stage, Deal.currency)
    )
    if solo_abiertos:
        consulta = consulta.where(Deal.stage.in_(OPEN_STAGES))
    filas = (await session.execute(consulta)).tuples().all()
    orden = {etapa: i for i, etapa in enumerate(DEAL_STAGES)}
    resumen = [
        ResumenEtapa(
            stage=etapa,
            currency=moneda,
            count=int(cuantos),
            total_value=Decimal(total).quantize(Decimal("0.01")),
            weighted_value=Decimal(pond).quantize(Decimal("0.01")),
            dias_acumulados=float(dias_total),
        )
        for etapa, moneda, cuantos, total, pond, dias_total in filas
    ]
    return sorted(resumen, key=lambda f: (orden[f.stage], f.currency))


async def ingresos_por_mes(
    session: AsyncSession, client_id: UUID, *, desde: date, hasta: date
) -> list[dict[str, Any]]:
    """Ingresos ganados por mes y moneda (deals `closed_won` por `actual_close_date`).

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        desde: Primer dia incluido.
        hasta: Ultimo dia incluido.

    Returns:
        `[{"month": "2026-10", "currency": "USD", "deals": 3, "revenue": Decimal}, ...]`, por mes
        y moneda.
    """
    mes = func.to_char(Deal.actual_close_date, "YYYY-MM")
    filas = await session.execute(
        select(mes, Deal.currency, func.count(), func.sum(Deal.value))
        .where(
            Deal.client_id == client_id,
            Deal.stage == DEAL_WON,
            and_(Deal.actual_close_date >= desde, Deal.actual_close_date <= hasta),
        )
        .group_by(mes, Deal.currency)
        .order_by(mes, Deal.currency)
    )
    return [
        {"month": m, "currency": c, "deals": int(n), "revenue": Decimal(v)}
        for m, c, n, v in filas.tuples().all()
    ]


def deal_a_dict(deal: Deal) -> dict[str, Any]:
    """Un deal para el export RGPD del titular.

    Args:
        deal: Deal.

    Returns:
        Sus datos, serializables.
    """
    return {
        "id": str(deal.id),
        "title": deal.title,
        "value": str(deal.value),
        "currency": deal.currency,
        "stage": deal.stage,
        "probability": deal.probability,
        "expected_close_date": (
            deal.expected_close_date.isoformat() if deal.expected_close_date else None
        ),
        "actual_close_date": deal.actual_close_date.isoformat() if deal.actual_close_date else None,
        "lost_reason": deal.lost_reason,
        "notes": deal.notes,
        "created_at": deal.created_at.isoformat(),
    }


async def deals_para_export(
    session: AsyncSession, client_id: UUID, lead_ids: Sequence[UUID]
) -> dict[UUID, list[dict[str, Any]]]:
    """Los deals de varios leads, para el export RGPD.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        lead_ids: Leads del titular.

    Returns:
        Lead -> sus deals (del mas antiguo al mas reciente).
    """
    resultado: dict[UUID, list[dict[str, Any]]] = {lead_id: [] for lead_id in lead_ids}
    if not lead_ids:
        return resultado
    filas = await session.execute(
        select(Deal)
        .where(Deal.client_id == client_id, Deal.lead_id.in_(list(lead_ids)))
        .order_by(Deal.created_at, Deal.id)
    )
    for deal in filas.scalars().all():
        resultado[deal.lead_id].append(deal_a_dict(deal))
    return resultado


async def anonimizar_deals(session: AsyncSession, client_id: UUID, lead_ids: Sequence[UUID]) -> int:
    """Quita el texto libre de los deals de unos leads (supresion RGPD).

    Se conservan importe, moneda, etapa y fechas: son datos de negocio (los ingresos) y no
    identifican a la persona. El titulo, las notas y el motivo de perdida son texto libre y
    pueden hacerlo ("Propuesta para Ana Perez"): el titulo pasa al marcador, las notas a `None`.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        lead_ids: Leads suprimidos.

    Returns:
        Deals tocados.
    """
    if not lead_ids:
        return 0
    deals = (
        (
            await session.execute(
                select(Deal).where(Deal.client_id == client_id, Deal.lead_id.in_(list(lead_ids)))
            )
        )
        .scalars()
        .all()
    )
    for deal in deals:
        deal.title = ANONIMIZADO
        deal.notes = None
        if deal.lost_reason is not None:
            deal.lost_reason = ANONIMIZADO
        deal.metadata_ = {}
    await session.flush()
    return len(deals)
