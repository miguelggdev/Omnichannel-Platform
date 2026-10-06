"""Fuentes de leads y su token de captura publica (Sprint 16, ADR-083).

GET    /api/v1/lead-sources                      lista las fuentes con su conteo de leads
POST   /api/v1/lead-sources                      crea una (con `enable_capture` devuelve el token)
GET    /api/v1/lead-sources/{id}                 detalle
PUT    /api/v1/lead-sources/{id}                 cambia nombre, ajustes, UTM o la activa/desactiva
DELETE /api/v1/lead-sources/{id}                 la borra (los leads que trajo se conservan)
POST   /api/v1/lead-sources/{id}/capture-token   genera o **rota** el token (invalida el anterior)
DELETE /api/v1/lead-sources/{id}/capture-token   apaga la captura publica de la fuente

El token en claro se devuelve **una sola vez**, en la respuesta que lo crea; en la base solo
queda su SHA-256 (`app.services.lead_pipeline.hash_capture_token`). Si se pierde, se rota.
Solo `admin` y `super_admin` configuran fuentes; el resto de roles operativos las lee.
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.dependencies import require_leads_module
from app.core.exceptions import NOT_FOUND, AppException
from app.models.lead import Lead
from app.models.lead_source import LeadSource
from app.schemas.lead import SourceCreate, SourceCreated, SourceResponse, SourceUpdate
from app.services.lead_pipeline import generate_capture_token

logger = logging.getLogger(__name__)

router = APIRouter()

_READ_ROLES = ("super_admin", "admin", "supervisor", "agent")
_CONFIG_ROLES = ("super_admin", "admin")


def _respuesta(fuente: LeadSource, leads_count: int = 0) -> SourceResponse:
    """Arma la respuesta sin exponer el hash del token (solo si la captura esta habilitada)."""
    return SourceResponse(
        id=fuente.id,
        name=fuente.name,
        source_type=fuente.source_type,
        config=fuente.config,
        utm_tracking=fuente.utm_tracking,
        is_active=fuente.is_active,
        capture_enabled=fuente.capture_token_hash is not None,
        leads_count=leads_count,
        created_at=fuente.created_at,
    )


async def _fuente_o_404(session: AsyncSession, source_id: UUID, client_id: UUID) -> LeadSource:
    fuente: LeadSource | None = (
        await session.execute(
            select(LeadSource).where(LeadSource.id == source_id, LeadSource.client_id == client_id)
        )
    ).scalar_one_or_none()
    if fuente is None:
        raise AppException(status_code=404, error_code=NOT_FOUND, message="Fuente no encontrada")
    return fuente


async def _conteos(session: AsyncSession, client_id: UUID) -> dict[UUID, int]:
    """Leads no borrados por fuente, en una sola consulta."""
    filas = (
        await session.execute(
            select(Lead.source_id, func.count())
            .where(
                Lead.client_id == client_id,
                Lead.source_id.is_not(None),
                Lead.deleted_at.is_(None),
            )
            .group_by(Lead.source_id)
        )
    ).all()
    return {f[0]: f[1] for f in filas}


@router.get("", response_model=list[SourceResponse])
async def list_sources(
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> list[SourceResponse]:
    """Lista las fuentes del tenant, de la mas reciente a la mas antigua.

    Args:
        user: Usuario autenticado.

    Returns:
        Las fuentes, cada una con cuantos leads (no borrados) trajo.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        fuentes = (
            await session.execute(
                select(LeadSource)
                .where(LeadSource.client_id == client_id)
                .order_by(LeadSource.created_at.desc(), LeadSource.id)
            )
        ).scalars()
        conteos = await _conteos(session, client_id)
        return [_respuesta(f, conteos.get(f.id, 0)) for f in fuentes]


@router.post("", response_model=SourceCreated, status_code=201)
async def create_source(
    data: SourceCreate,
    user: dict[str, Any] = Depends(require_leads_module(*_CONFIG_ROLES)),
) -> SourceCreated:
    """Crea una fuente; con `enable_capture` genera su token de captura publica.

    Args:
        data: Datos de la fuente.
        user: Usuario autenticado; solo admin o super_admin.

    Returns:
        La fuente y, si se pidio, el token en claro (la unica vez que se ve).
    """
    client_id: UUID = user["client_id"]
    token: str | None = None
    token_hash: str | None = None
    if data.enable_capture:
        token, token_hash = generate_capture_token()
    async with tenant_session(client_id) as session:
        fuente = LeadSource(
            client_id=client_id,
            name=data.name,
            source_type=data.source_type,
            config=data.config,
            utm_tracking=data.utm_tracking,
            capture_token_hash=token_hash,
        )
        session.add(fuente)
        await session.flush()
        base = _respuesta(fuente)
    return SourceCreated(**base.model_dump(), capture_token=token)


@router.get("/{source_id}", response_model=SourceResponse)
async def get_source(
    source_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_READ_ROLES)),
) -> SourceResponse:
    """Detalle de una fuente.

    Args:
        source_id: Fuente a leer.
        user: Usuario autenticado.

    Returns:
        La fuente con su conteo de leads.

    Raises:
        AppException: 404 si no existe en este tenant.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        fuente = await _fuente_o_404(session, source_id, client_id)
        return _respuesta(fuente, (await _conteos(session, client_id)).get(fuente.id, 0))


@router.put("/{source_id}", response_model=SourceResponse)
async def update_source(
    source_id: UUID,
    data: SourceUpdate,
    user: dict[str, Any] = Depends(require_leads_module(*_CONFIG_ROLES)),
) -> SourceResponse:
    """Cambia nombre, ajustes, UTM o el estado activo de la fuente.

    Desactivarla hace que su captura publica deje de aceptar leads sin invalidar el token.

    Args:
        source_id: Fuente a modificar.
        data: Campos a cambiar.
        user: Usuario autenticado; solo admin o super_admin.

    Returns:
        La fuente actualizada.

    Raises:
        AppException: 404 si no existe en este tenant.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        fuente = await _fuente_o_404(session, source_id, client_id)
        for campo in data.model_fields_set:
            valor = getattr(data, campo)
            if valor is not None:  # todas las columnas editables son NOT NULL
                setattr(fuente, campo, valor)
        await session.flush()
        return _respuesta(fuente, (await _conteos(session, client_id)).get(fuente.id, 0))


@router.delete("/{source_id}", status_code=204)
async def delete_source(
    source_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_CONFIG_ROLES)),
) -> None:
    """Borra una fuente; sus leads se conservan con `source_id = NULL`.

    Args:
        source_id: Fuente a borrar.
        user: Usuario autenticado; solo admin o super_admin.

    Raises:
        AppException: 404 si no existe en este tenant.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        await session.delete(await _fuente_o_404(session, source_id, client_id))
    logger.info("Fuente %s borrada por %s (tenant %s)", source_id, user["user_id"], client_id)


@router.post("/{source_id}/capture-token", response_model=SourceCreated)
async def rotate_capture_token(
    source_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_CONFIG_ROLES)),
) -> SourceCreated:
    """Genera un token de captura nuevo; el anterior deja de funcionar al instante.

    Args:
        source_id: Fuente.
        user: Usuario autenticado; solo admin o super_admin.

    Returns:
        La fuente y el token nuevo en claro (la unica vez que se ve).

    Raises:
        AppException: 404 si no existe en este tenant.
    """
    client_id: UUID = user["client_id"]
    token, token_hash = generate_capture_token()
    async with tenant_session(client_id) as session:
        fuente = await _fuente_o_404(session, source_id, client_id)
        fuente.capture_token_hash = token_hash
        await session.flush()
        base = _respuesta(fuente, (await _conteos(session, client_id)).get(fuente.id, 0))
    logger.info("Token de captura de %s rotado por %s", source_id, user["user_id"])
    return SourceCreated(**base.model_dump(), capture_token=token)


@router.delete("/{source_id}/capture-token", response_model=SourceResponse)
async def disable_capture(
    source_id: UUID,
    user: dict[str, Any] = Depends(require_leads_module(*_CONFIG_ROLES)),
) -> SourceResponse:
    """Apaga la captura publica de la fuente (borra el hash del token).

    Args:
        source_id: Fuente.
        user: Usuario autenticado; solo admin o super_admin.

    Returns:
        La fuente con `capture_enabled=false`.

    Raises:
        AppException: 404 si no existe en este tenant.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        fuente = await _fuente_o_404(session, source_id, client_id)
        fuente.capture_token_hash = None
        await session.flush()
        return _respuesta(fuente, (await _conteos(session, client_id)).get(fuente.id, 0))
