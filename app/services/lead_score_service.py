"""Aplicar un score a un lead y guardar su historia (Sprint 17, slice Dev A).

Es el unico camino para cambiar `fit_score`, `behavioral_score` o `ai_score`: actualiza la
columna, recalcula `total_score` con los pesos del tenant, guarda la fila de `lead_scores` y
anota el cambio en el historial del lead. Asi el endpoint de recalculo, la tarea de
enriquecimiento y el AI score (Dev B) no pueden olvidar ninguno de los cuatro pasos.

Ningun calculo hace `commit`: todo va en la transaccion de quien llama (`tenant_session()`).
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.models.lead import Lead
from app.models.lead_activity import ACTIVITY_SCORE_CHANGED
from app.models.lead_score import SCORE_BEHAVIORAL, SCORE_FIT, SCORE_TYPES, LeadScore
from app.schemas.lead_scoring import IcpConfig, LeadScoreEntry, LeadScoresDetail, ScoreResult
from app.services.lead_activity import instante_monotono, registrar_actividad
from app.services.lead_behavioral import calcular_behavioral, recolectar_senales
from app.services.lead_fit import calcular_fit, cargar_icp, fit_input_de_lead
from app.services.lead_scoring import (
    DEFAULT_WEIGHTS,
    InvalidWeightsError,
    compute_total_score,
    normalizar_pesos,
)

logger = logging.getLogger(__name__)

_COLUMNA: dict[str, str] = {tipo: f"{tipo}_score" for tipo in SCORE_TYPES}


async def pesos_del_tenant(session: AsyncSession, client_id: UUID) -> dict[str, int]:
    """Los pesos del score total del tenant, o los de por defecto si los guardados no sirven.

    Un JSON invalido solo puede venir de una escritura directa en la base: se registra y se usan
    40/30/30, porque un recalculo en segundo plano no debe romperse por eso.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.

    Returns:
        `{"fit": n, "behavioral": n, "ai": n}`.
    """
    raw = (
        await session.execute(select(Client.lead_scoring_weights).where(Client.id == client_id))
    ).scalar_one_or_none()
    try:
        return normalizar_pesos(raw)
    except InvalidWeightsError:
        logger.warning("lead_scoring_weights invalidos para el tenant; se usan los de por defecto")
        return dict(DEFAULT_WEIGHTS)


async def icp_del_tenant(session: AsyncSession, client_id: UUID) -> IcpConfig | None:
    """El ICP del tenant, o `None` si no tiene (o el guardado no es valido).

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.

    Returns:
        El ICP, o `None`.
    """
    raw = (
        await session.execute(select(Client.icp_config).where(Client.id == client_id))
    ).scalar_one_or_none()
    return cargar_icp(raw)


def recalcular_total(lead: Lead, pesos: dict[str, int]) -> int:
    """Pone en `lead.total_score` la combinacion de sus tres scores con los pesos dados.

    Args:
        lead: Lead (se modifica en sitio).
        pesos: Pesos del tenant (ver `pesos_del_tenant()`).

    Returns:
        El total nuevo.
    """
    lead.total_score = compute_total_score(
        lead.fit_score, lead.behavioral_score, lead.ai_score, pesos
    )
    return lead.total_score


def aplicar_score(
    session: AsyncSession,
    lead: Lead,
    score_type: str,
    resultado: ScoreResult,
    *,
    trigger: str,
    pesos: dict[str, int],
    user_id: UUID | None = None,
    forzar_historial: bool = False,
) -> LeadScore | None:
    """Aplica el resultado de un calculador al lead.

    Si el resultado no es aplicable (p. ej. FIT sin ICP) no toca nada. Si el valor no cambia, no
    escribe historial salvo con `forzar_historial` (un recalculo pedido a mano debe dejar
    constancia aunque de lo mismo). Cuando cambia: columna, total, fila de `lead_scores` y
    actividad `score_changed` (con ids y numeros, sin datos personales).

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead a puntuar (se modifica en sitio).
        score_type: `fit`, `behavioral` o `ai`.
        resultado: Salida del calculador.
        trigger: Una de las constantes `TRIGGER_*` de `app.models.lead_score`.
        pesos: Pesos del total del tenant.
        user_id: Quien lo pidio; `None` si es el sistema.
        forzar_historial: Guardar la fila aunque el valor no cambie.

    Returns:
        La fila de historial creada, o `None` si no se escribio ninguna.

    Raises:
        ValueError: Si `score_type` no es uno de los tres.
    """
    columna = _COLUMNA.get(score_type)
    if columna is None:
        raise ValueError(f"Tipo de score desconocido: {score_type!r}")
    if not resultado.aplicable:
        return None

    anterior: int = getattr(lead, columna)
    cambia = resultado.score != anterior
    if not cambia and not forzar_historial:
        return None

    setattr(lead, columna, resultado.score)
    total_anterior = lead.total_score
    recalcular_total(lead, pesos)

    fila = LeadScore(
        client_id=lead.client_id,
        lead_id=lead.id,
        user_id=user_id,
        score_type=score_type,
        score=resultado.score,
        previous_score=anterior,
        trigger=trigger,
        factors=resultado.factors,
        created_at=instante_monotono(),
    )
    session.add(fila)
    if cambia:
        registrar_actividad(
            session,
            client_id=lead.client_id,
            lead_id=lead.id,
            tipo=ACTIVITY_SCORE_CHANGED,
            user_id=user_id,
            score_type=score_type,
            score_from=anterior,
            score_to=resultado.score,
            total_from=total_anterior,
            total_to=lead.total_score,
            trigger=trigger,
        )
    return fila


async def recalcular_fit(
    session: AsyncSession,
    lead: Lead,
    *,
    trigger: str,
    user_id: UUID | None = None,
    icp: IcpConfig | None = None,
    pesos: dict[str, int] | None = None,
    forzar_historial: bool = False,
) -> LeadScore | None:
    """Calcula y aplica el FIT del lead contra el ICP del tenant.

    Para recalcular muchos leads (cambio de ICP), pasar `icp` y `pesos` ya leidos evita dos
    consultas por lead.

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead.
        trigger: Disparador (`TRIGGER_*`).
        user_id: Quien lo pidio.
        icp: ICP ya cargado; si no, se lee.
        pesos: Pesos ya cargados; si no, se leen.
        forzar_historial: Ver `aplicar_score()`.

    Returns:
        La fila de historial creada, o `None`.
    """
    if icp is None:
        icp = await icp_del_tenant(session, lead.client_id)
    if pesos is None:
        pesos = await pesos_del_tenant(session, lead.client_id)
    resultado = calcular_fit(
        fit_input_de_lead(
            industry=lead.industry,
            company_size=lead.company_size,
            job_title=lead.job_title,
            enrichment_data=lead.enrichment_data,
        ),
        icp,
    )
    return aplicar_score(
        session,
        lead,
        SCORE_FIT,
        resultado,
        trigger=trigger,
        pesos=pesos,
        user_id=user_id,
        forzar_historial=forzar_historial,
    )


async def recalcular_behavioral(
    session: AsyncSession,
    lead: Lead,
    *,
    trigger: str,
    ahora: datetime,
    user_id: UUID | None = None,
    pesos: dict[str, int] | None = None,
    forzar_historial: bool = False,
) -> LeadScore | None:
    """Calcula y aplica el score de comportamiento del lead.

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead.
        trigger: Disparador (`TRIGGER_*`).
        ahora: Instante de referencia (aware).
        user_id: Quien lo pidio.
        pesos: Pesos ya cargados; si no, se leen.
        forzar_historial: Ver `aplicar_score()`.

    Returns:
        La fila de historial creada, o `None`.
    """
    if pesos is None:
        pesos = await pesos_del_tenant(session, lead.client_id)
    senales = await recolectar_senales(session, lead, ahora)
    return aplicar_score(
        session,
        lead,
        SCORE_BEHAVIORAL,
        calcular_behavioral(senales, ahora),
        trigger=trigger,
        pesos=pesos,
        user_id=user_id,
        forzar_historial=forzar_historial,
    )


async def scores_detallados(session: AsyncSession, lead: Lead) -> LeadScoresDetail:
    """Valores vigentes del lead y el ultimo calculo guardado de cada tipo.

    Es la respuesta de `GET /leads/{id}/score` (Dev B).

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead.

    Returns:
        El detalle; `latest` solo trae los tipos que tienen historial.
    """
    ultimos = (
        (
            await session.execute(
                select(LeadScore)
                .where(LeadScore.client_id == lead.client_id, LeadScore.lead_id == lead.id)
                .order_by(LeadScore.score_type, LeadScore.created_at.desc(), LeadScore.id.desc())
                .distinct(LeadScore.score_type)
            )
        )
        .scalars()
        .all()
    )
    return LeadScoresDetail(
        lead_id=lead.id,
        fit_score=lead.fit_score,
        behavioral_score=lead.behavioral_score,
        ai_score=lead.ai_score,
        total_score=lead.total_score,
        weights=await pesos_del_tenant(session, lead.client_id),
        latest={f.score_type: LeadScoreEntry.model_validate(f) for f in ultimos},
    )


async def historial_scores(
    session: AsyncSession,
    client_id: UUID,
    lead_id: UUID,
    *,
    score_type: str | None = None,
    limite: int = 50,
) -> list[LeadScore]:
    """Historial de scores de un lead, del mas reciente al mas antiguo.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        lead_id: Lead.
        score_type: Solo un tipo; `None` para todos.
        limite: Maximo de filas.

    Returns:
        Las filas.
    """
    consulta = select(LeadScore).where(
        LeadScore.client_id == client_id, LeadScore.lead_id == lead_id
    )
    if score_type is not None:
        consulta = consulta.where(LeadScore.score_type == score_type)
    filas = await session.execute(
        consulta.order_by(LeadScore.created_at.desc(), LeadScore.id.desc()).limit(limite)
    )
    return list(filas.scalars().all())


def score_a_dict(fila: LeadScore) -> dict[str, Any]:
    """Una fila de historial para el export RGPD del titular.

    Args:
        fila: Fila de `lead_scores`.

    Returns:
        Sus campos serializables.
    """
    return LeadScoreEntry.model_validate(fila).model_dump(mode="json")


async def scores_para_export(
    session: AsyncSession, client_id: UUID, lead_ids: Sequence[UUID]
) -> dict[UUID, list[dict[str, Any]]]:
    """Historial de scores de varios leads, para el export RGPD.

    Un score es perfilado de la persona: el derecho de acceso lo incluye.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        lead_ids: Leads del titular.

    Returns:
        Lead -> sus filas (de la mas antigua a la mas reciente).
    """
    resultado: dict[UUID, list[dict[str, Any]]] = {lead_id: [] for lead_id in lead_ids}
    if not lead_ids:
        return resultado
    filas = await session.execute(
        select(LeadScore)
        .where(LeadScore.client_id == client_id, LeadScore.lead_id.in_(list(lead_ids)))
        .order_by(LeadScore.created_at, LeadScore.id)
    )
    for fila in filas.scalars().all():
        resultado[fila.lead_id].append(score_a_dict(fila))
    return resultado


async def borrar_historial_scores(
    session: AsyncSession, client_id: UUID, lead_ids: Sequence[UUID]
) -> int:
    """Borra el historial de scores de unos leads (supresion RGPD).

    Los factores del FIT y del comportamiento no llevan datos del lead, pero los del AI score
    pueden llevar razonamiento en texto sobre sus conversaciones: la supresion los quita todos.
    Los valores vigentes en `leads` se conservan (son estadisticas, como la etapa).

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        lead_ids: Leads a limpiar.

    Returns:
        Filas borradas.
    """
    if not lead_ids:
        return 0
    # `Any`: `execute()` tipa `Result[Any]` y `rowcount` solo existe en `CursorResult`.
    borradas: Any = await session.execute(
        delete(LeadScore).where(
            LeadScore.client_id == client_id, LeadScore.lead_id.in_(list(lead_ids))
        )
    )
    return int(borradas.rowcount or 0)
