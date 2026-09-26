"""CRUD de templates de tenant (Sprint 10, Dev B).

POST /api/v1/admin/templates                              snapshot de un tenant   (super_admin)
GET  /api/v1/admin/templates                              listado con resumen     (super_admin, admin)
GET  /api/v1/admin/templates/{id}                         detalle con snapshot    (super_admin)
POST /api/v1/admin/templates/{id}/instantiate             crea un tenant (202)    (super_admin)
GET  /api/v1/admin/templates/instantiations/{id}          avance de la clonacion  (super_admin)

Sobre lo que dejo Dev A (ADR-064): `TenantCloner` arma el snapshot y crea el
tenant, y `app.tasks.bulk_clone_tenant_from_template` hace la clonacion en la
cola `bulk`. Este modulo solo administra.

`tenant_templates` y `template_instantiations` no tienen `client_id` ni RLS
(segunda excepcion a la Regla 1, igual que `clients`): el control de acceso es
enteramente RBAC, y por eso vive aca, explicito en cada endpoint. Se leen con
`AsyncSessionLocal`, sin `tenant_session()`, como hace la task.

Visibilidad para `admin`
-------------------------
El listado le muestra los templates publicos y los tomados de su propio
tenant, y solo con su resumen: el snapshot lleva prompts de sistema y rutas de
documentos del tenant origen, que no son de otro tenant para leer (ver
`app/schemas/template.py`).
"""

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select, text

from app.core.database import AsyncSessionLocal, tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import DUPLICATE, INTERNAL_ERROR, NOT_FOUND, AppException
from app.models.client import Client
from app.models.tenant_template import InstantiationStatus, TemplateInstantiation, TenantTemplate
from app.schemas.template import (
    InstantiationAccepted,
    InstantiationResponse,
    TemplateCreate,
    TemplateDetailResponse,
    TemplateInstantiate,
    TemplateListResponse,
    TemplateResponse,
    TemplateSummary,
)
from app.services.tenant_cloner import TenantCloner

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement

logger = logging.getLogger(__name__)

router = APIRouter()

_SUPER = ("super_admin",)
_LISTADO = ("super_admin", "admin")

#: Mismo lookup que el login (ADR-046): `users` tiene RLS y el email es unico
#: en toda la plataforma, asi que no hay otra forma de saber si ya existe.
_EMAIL_EN_USO = text("SELECT 1 FROM public.auth_lookup_user(:email)")


def _a_respuesta(template: TenantTemplate) -> TemplateResponse:
    """Convierte un template en su representacion de listado.

    Args:
        template: Fila de `tenant_templates`.

    Returns:
        Metadatos y resumen, sin el snapshot.
    """
    return TemplateResponse(
        id=template.id,
        name=template.name,
        description=template.description,
        source_client_id=template.source_client_id,
        is_public=template.is_public,
        version=template.version,
        summary=TemplateSummary.del_snapshot(template.config or {}),
        created_at=template.created_at,
    )


async def _template_o_404(template_id: UUID) -> TenantTemplate:
    """Carga un template o levanta 404.

    Args:
        template_id: Template buscado.

    Returns:
        El template.

    Raises:
        AppException: 404 si no existe.
    """
    async with AsyncSessionLocal() as session:
        template = (
            await session.execute(select(TenantTemplate).where(TenantTemplate.id == template_id))
        ).scalar_one_or_none()
    if template is None:
        raise AppException(status_code=404, error_code=NOT_FOUND, message="Template no encontrado")
    return template


@router.post("", status_code=201, response_model=TemplateDetailResponse)
async def create_template(
    data: TemplateCreate,
    user: dict[str, Any] = Depends(require_role(*_SUPER)),
) -> TemplateDetailResponse:
    """Toma un snapshot de un tenant y lo guarda como template.

    Args:
        data: Nombre, descripcion y tenant origen.
        user: `super_admin` autenticado.

    Returns:
        El template creado, con su snapshot.

    Raises:
        AppException: 404 si el tenant origen no existe o esta inactivo.
    """
    # `clients` tiene RLS: se consulta con el contexto del propio tenant
    # origen, que es la unica forma de verlo sin un rol con BYPASSRLS.
    async with tenant_session(data.source_client_id) as session:
        origen = (
            await session.execute(
                select(Client.id).where(
                    Client.id == data.source_client_id, Client.is_active.is_(True)
                )
            )
        ).scalar_one_or_none()
    if origen is None:
        raise AppException(
            status_code=404, error_code=NOT_FOUND, message="Tenant origen no encontrado"
        )

    snapshot = await TenantCloner().create_snapshot(data.source_client_id)

    template = TenantTemplate(
        name=data.name,
        description=data.description,
        source_client_id=data.source_client_id,
        config=snapshot,
        created_by=user["user_id"],
        is_public=data.is_public,
    )
    async with AsyncSessionLocal() as session, session.begin():
        session.add(template)
        await session.flush()
        await session.refresh(template)
        respuesta = TemplateDetailResponse(
            **_a_respuesta(template).model_dump(),
            created_by=template.created_by,
            config=template.config,
        )

    logger.info(
        "Template %s creado desde el tenant %s por %s",
        respuesta.id,
        data.source_client_id,
        user["user_id"],
    )
    return respuesta


@router.get("", response_model=TemplateListResponse)
async def list_templates(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_role(*_LISTADO)),
) -> TemplateListResponse:
    """Lista los templates visibles para el usuario, del mas nuevo al mas viejo.

    Args:
        page: Pagina, desde 1.
        page_size: Tamano de pagina.
        user: Usuario autenticado.

    Returns:
        La pagina, con el resumen de cada template.
    """
    filtros: list[ColumnElement[bool]] = []
    if user["role"] != "super_admin":
        filtros.append(
            or_(
                TenantTemplate.is_public.is_(True),
                TenantTemplate.source_client_id == user["client_id"],
            )
        )

    async with AsyncSessionLocal() as session:
        total = await session.scalar(
            select(func.count()).select_from(TenantTemplate).where(*filtros)
        )
        templates = (
            (
                await session.execute(
                    select(TenantTemplate)
                    .where(*filtros)
                    .order_by(TenantTemplate.created_at.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )

    return TemplateListResponse(
        items=[_a_respuesta(t) for t in templates],
        total=int(total or 0),
        page=page,
        page_size=page_size,
    )


@router.get("/instantiations/{instantiation_id}", response_model=InstantiationResponse)
async def get_instantiation(
    instantiation_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_SUPER)),
) -> InstantiationResponse:
    """Devuelve el avance de una clonacion.

    Args:
        instantiation_id: Instanciacion buscada.
        user: `super_admin` autenticado.

    Returns:
        Estado, avance y, si ya existe, el tenant creado.

    Raises:
        AppException: 404 si no existe.
    """
    async with AsyncSessionLocal() as session:
        instanciacion = (
            await session.execute(
                select(TemplateInstantiation).where(TemplateInstantiation.id == instantiation_id)
            )
        ).scalar_one_or_none()
    if instanciacion is None:
        raise AppException(
            status_code=404, error_code=NOT_FOUND, message="Instanciacion no encontrada"
        )
    return InstantiationResponse.model_validate(instanciacion)


@router.get("/{template_id}", response_model=TemplateDetailResponse)
async def get_template(
    template_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_SUPER)),
) -> TemplateDetailResponse:
    """Devuelve un template con su snapshot completo.

    Args:
        template_id: Template buscado.
        user: `super_admin` autenticado.

    Returns:
        El template con su snapshot.

    Raises:
        AppException: 404 si no existe.
    """
    template = await _template_o_404(template_id)
    return TemplateDetailResponse(
        **_a_respuesta(template).model_dump(),
        created_by=template.created_by,
        config=template.config,
    )


@router.post("/{template_id}/instantiate", status_code=202, response_model=InstantiationAccepted)
async def instantiate_template(
    template_id: UUID,
    data: TemplateInstantiate,
    user: dict[str, Any] = Depends(require_role(*_SUPER)),
) -> InstantiationAccepted:
    """Encola la creacion de un tenant nuevo a partir de un template.

    El email del admin se valida aca y no en la task: `users.email` es unico en
    toda la plataforma, y descubrirlo en el worker dejaria una instanciacion
    en `failed` con un error de integridad, minutos despues de un 202.

    Args:
        template_id: Template del que se clona.
        data: Nombre del tenant, email del admin y overrides.
        user: `super_admin` autenticado.

    Returns:
        El id de la instanciacion, para consultar el avance.

    Raises:
        AppException: 404 si el template no existe; 409 si el email ya esta en
            uso; 503 si no se pudo encolar la clonacion.
    """
    await _template_o_404(template_id)

    async with AsyncSessionLocal() as session:
        en_uso = (await session.execute(_EMAIL_EN_USO, {"email": str(data.admin_email)})).first()
    if en_uso is not None:
        raise AppException(
            status_code=409,
            error_code=DUPLICATE,
            message="Ya existe un usuario con ese email en la plataforma",
        )

    async with AsyncSessionLocal() as session, session.begin():
        instanciacion = TemplateInstantiation(
            template_id=template_id, status=InstantiationStatus.PENDING.value
        )
        session.add(instanciacion)
        await session.flush()
        instantiation_id = instanciacion.id

    # Despues del commit: el worker abre su propia conexion y tiene que ver la
    # instanciacion (mismo motivo que la ingesta de documentos).
    from app.tasks.tenant_operations import clone_tenant_from_template

    try:
        clone_tenant_from_template.delay(
            template_id=str(template_id),
            instantiation_id=str(instantiation_id),
            new_tenant_name=data.tenant_name,
            admin_email=str(data.admin_email),
            custom_overrides=data.overrides,
        )
    except Exception as exc:
        logger.exception("No se pudo encolar la instanciacion %s", instantiation_id)
        async with AsyncSessionLocal() as session, session.begin():
            fila = await session.get(TemplateInstantiation, instantiation_id)
            if fila is not None:
                fila.status = InstantiationStatus.FAILED.value
                fila.error_message = "No se pudo encolar la clonacion"
        raise AppException(
            status_code=503,
            error_code=INTERNAL_ERROR,
            message="No se pudo encolar la clonacion; intenta de nuevo",
        ) from exc

    logger.info(
        "Instanciacion %s del template %s encolada por %s",
        instantiation_id,
        template_id,
        user["user_id"],
    )
    return InstantiationAccepted(
        instantiation_id=instantiation_id, status=InstantiationStatus.PENDING.value
    )
