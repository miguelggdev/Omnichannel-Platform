"""Gestion de clientes (tenants) desde la plataforma — solo `super_admin` (Sprint 15, fase 2).

GET /api/v1/platform/clients               lista paginada con cifras de uso
GET /api/v1/platform/clients/{id}          detalle de un cliente
PUT /api/v1/platform/clients/{id}/status   suspende o reactiva

Es el unico sitio de la API que mira **entre** tenants, asi que cada decision esta acotada:

- **El listado** usa `public.admin_list_clients()` (migracion 024), una funcion `SECURITY DEFINER`
  que devuelve solo nombre, plan, estado y conteos; nunca contenido.
- **El detalle y el cambio de estado** no se saltan la RLS: abren `tenant_session(id)` con el id
  del tenant elegido, como hace la publicacion del sandbox, y leen o escriben solo esa fila.
- Los sandbox (`is_sandbox`) no son clientes y no aparecen ni se pueden tocar.
- Un `super_admin` no puede suspender **su propio** tenant: se quedaria sin acceso al panel.
- Suspender corta `/auth/login` y `/auth/refresh`; un access token ya emitido sigue valiendo
  hasta que caduque (`JWT_EXPIRATION_MINUTES`).
"""

import logging
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, text

from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import NOT_FOUND, VALIDATION_ERROR, AppException
from app.models.client import Client
from app.schemas.platform import (
    ClientDetail,
    ClientListResponse,
    ClientStatusUpdate,
    ClientSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_ROLES = ("super_admin",)

_LISTAR = text(
    "SELECT * FROM public.admin_list_clients("
    "CAST(:search AS text), CAST(:active AS boolean), CAST(:limit AS integer), "
    "CAST(:offset AS integer))"
)

# Cifras de un tenant, leidas desde su propio contexto (RLS aplicada) con `client_id` explicito.
_DETALLE = text(
    """
    SELECT
      (SELECT count(*) FROM users WHERE client_id = :cid) AS users_total,
      (SELECT count(*) FROM users WHERE client_id = :cid AND is_active) AS users_active,
      (SELECT count(*) FROM contacts WHERE client_id = :cid AND merged_into_id IS NULL)
        AS contacts_total,
      (SELECT count(*) FROM conversations WHERE client_id = :cid
         AND status NOT IN ('resolved', 'archived')) AS conversations_open,
      (SELECT count(*) FROM conversations WHERE client_id = :cid
         AND created_at >= now() - interval '30 days') AS conversations_30d,
      (SELECT count(*) FROM messages WHERE client_id = :cid
         AND created_at >= now() - interval '30 days') AS messages_30d,
      (SELECT max(created_at) FROM messages WHERE client_id = :cid) AS last_message_at,
      (SELECT count(*) FROM documents WHERE client_id = :cid AND status = 'completed')
        AS documents_ready,
      EXISTS (SELECT 1 FROM agent_configs WHERE client_id = :cid AND is_active) AS has_agent
    """
)
_PRESUPUESTO = text(
    "SELECT used_tokens, total_budget FROM token_budgets "
    "WHERE client_id = :cid AND month = to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM')"
)


def _escapar(texto: str) -> str:
    """Escapa `\\`, `%` y `_` para que el texto del usuario no actue como patron de `ILIKE`."""
    return texto.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _detalle(client_id: UUID) -> ClientDetail:
    """Lee el cliente y su uso desde el contexto de ese tenant.

    Args:
        client_id: Tenant a leer.

    Returns:
        El detalle.

    Raises:
        AppException: 404 si no existe o es un sandbox.
    """
    async with tenant_session(client_id) as session:
        cliente: Client | None = (
            await session.execute(select(Client).where(Client.id == client_id))
        ).scalar_one_or_none()
        if cliente is None or cliente.is_sandbox:
            raise AppException(
                status_code=404, error_code=NOT_FOUND, message="Cliente no encontrado"
            )
        cifras = (await session.execute(_DETALLE, {"cid": client_id})).mappings().one()
        presupuesto = (
            (await session.execute(_PRESUPUESTO, {"cid": client_id})).mappings().one_or_none()
        )
        return ClientDetail(
            id=cliente.id,
            name=cliente.name,
            slug=cliente.slug,
            plan=str(cliente.plan),
            is_active=cliente.is_active,
            created_at=cliente.created_at,
            suspended_at=cliente.suspended_at,
            alert_message=cliente.alert_message,
            token_used=presupuesto["used_tokens"] if presupuesto else None,
            token_budget=presupuesto["total_budget"] if presupuesto else None,
            **dict(cifras),
        )


@router.get("/clients", response_model=ClientListResponse)
async def list_clients(
    search: str | None = Query(default=None, max_length=100, description="Nombre o slug"),
    status: Literal["all", "active", "inactive"] = Query(default="all"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> ClientListResponse:
    """Lista los clientes de la plataforma, del mas reciente al mas antiguo.

    Args:
        search: Texto a buscar en nombre o slug (sin distinguir mayusculas).
        status: `active`, `inactive` o `all`.
        page: Pagina, empezando en 1.
        page_size: Tamano de pagina, maximo 100.
        user: Usuario autenticado; solo super_admin.

    Returns:
        Una pagina con sus cifras de uso y el total de coincidencias.
    """
    async with tenant_session(user["client_id"]) as session:
        filas = (
            (
                await session.execute(
                    _LISTAR,
                    {
                        "search": _escapar(search.strip()) if search and search.strip() else None,
                        "active": None if status == "all" else status == "active",
                        "limit": page_size,
                        "offset": (page - 1) * page_size,
                    },
                )
            )
            .mappings()
            .all()
        )
    total = int(filas[0]["total_count"]) if filas else 0
    return ClientListResponse(
        items=[ClientSummary(**{k: v for k, v in f.items() if k != "total_count"}) for f in filas],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/clients/{client_id}", response_model=ClientDetail)
async def get_client(
    client_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> ClientDetail:
    """Detalle de un cliente: datos, estado y uso (solo conteos, nada de su contenido).

    Args:
        client_id: Tenant a consultar.
        user: Usuario autenticado; solo super_admin.

    Returns:
        El detalle.

    Raises:
        AppException: 404 si no existe o es un sandbox.
    """
    return await _detalle(client_id)


@router.put("/clients/{client_id}/status", response_model=ClientDetail)
async def update_client_status(
    client_id: UUID,
    data: ClientStatusUpdate,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> ClientDetail:
    """Suspende o reactiva a un cliente.

    Suspender impide que sus usuarios inicien sesion o la renueven. El access token que ya
    tengan sigue valiendo hasta que caduque.

    Args:
        client_id: Tenant a modificar.
        data: Nuevo estado y, al suspender, el motivo que se guarda.
        user: Usuario autenticado; solo super_admin.

    Returns:
        El detalle ya actualizado.

    Raises:
        AppException: 404 si no existe o es un sandbox; 400 si intenta suspender su propio tenant.
    """
    if client_id == user["client_id"] and not data.is_active:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message="No puedes suspender tu propio tenant: te quedarias sin acceso al panel",
        )

    async with tenant_session(client_id) as session:
        cliente: Client | None = (
            await session.execute(select(Client).where(Client.id == client_id))
        ).scalar_one_or_none()
        if cliente is None or cliente.is_sandbox:
            raise AppException(
                status_code=404, error_code=NOT_FOUND, message="Cliente no encontrado"
            )
        if cliente.is_active != data.is_active:
            cliente.is_active = data.is_active
            cliente.suspended_at = None if data.is_active else func.now()
        # El motivo solo tiene sentido mientras esta suspendido.
        cliente.alert_message = None if data.is_active else (data.reason or None)
        await session.flush()

    logger.warning(
        "Cliente %s %s por el super_admin %s",
        client_id,
        "reactivado" if data.is_active else "suspendido",
        user.get("user_id"),
    )
    return await _detalle(client_id)
