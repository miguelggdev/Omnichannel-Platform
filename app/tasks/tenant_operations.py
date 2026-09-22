"""Task de Celery para clonar un tenant desde un template (Sprint 10).

Nombre de la tarea
-------------------
`app.tasks.bulk_clone_tenant_from_template`, no `clone_tenant_from_template`
de la spec: el routing automatico de `celery_config.py` manda a la cola
`bulk` lo que empieza por `app.tasks.bulk_*` (mismo motivo que
`bulk_auto_close_conversations` en `auto_close.py`). Va en `bulk` porque
regenerar embeddings de documentos puede tardar minutos.

`tenant_templates`/`template_instantiations` no tienen RLS (ver migracion 010
y ADR-064): las consultas de este modulo usan `AsyncSessionLocal` directo,
sin `tenant_session()`. `TenantCloner.instantiate()`/`clone_documents()` si
la usan, para las tablas del tenant nuevo (que si son multi-tenant).
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from celery import shared_task
from sqlalchemy import select
from sqlalchemy import update as sa_update

from app.core.database import AsyncSessionLocal, run_isolated
from app.models.tenant_template import InstantiationStatus, TemplateInstantiation, TenantTemplate
from app.services.tenant_cloner import TenantCloner
from app.tasks.document_ingestion import ingest_document

logger = logging.getLogger(__name__)

# Cuanto de un error se deja en `error_message`: lo lee un super_admin desde
# GET /admin/templates/instantiations/{id}, pero sigue sin ser el lugar para
# un traceback completo (CLAUDE.md, regla 3).
_ERROR_MESSAGE_MAX_CHARS = 500


async def _actualizar_instantiation(instantiation_id: UUID, **campos: Any) -> None:
    """Aplica un UPDATE puntual sobre una instanciacion, en su propia transaccion.

    Args:
        instantiation_id: Instanciacion a actualizar.
        **campos: Columnas a modificar.
    """
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(
            sa_update(TemplateInstantiation)
            .where(TemplateInstantiation.id == instantiation_id)
            .values(**campos)
        )


async def _clone(
    template_id: str,
    instantiation_id: str,
    new_tenant_name: str,
    admin_email: str,
    custom_overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    """Ejecuta la clonacion completa y deja el estado en `template_instantiations`.

    Args:
        template_id: UUID del template, serializado.
        instantiation_id: UUID de la instanciacion a actualizar, serializado.
        new_tenant_name: Nombre visible del tenant nuevo.
        admin_email: Email del usuario admin que se crea para el tenant nuevo.
        custom_overrides: Overrides opcionales de configuracion (ver
            `TenantCloner.instantiate()`).

    Returns:
        Resumen con el estado final y, si tuvo exito, el `client_id` nuevo.
    """
    tpl_id = UUID(template_id)
    inst_id = UUID(instantiation_id)
    cloner = TenantCloner()

    await _actualizar_instantiation(
        inst_id,
        status=InstantiationStatus.PROCESSING.value,
        started_at=datetime.now(timezone.utc),
    )

    try:
        async with AsyncSessionLocal() as session:
            template = (
                await session.execute(select(TenantTemplate).where(TenantTemplate.id == tpl_id))
            ).scalar_one_or_none()
        if template is None:
            raise ValueError(f"Template {tpl_id} no encontrado")

        new_client_id, temp_password = await cloner.instantiate(
            template, new_tenant_name, admin_email, custom_overrides
        )
        await _actualizar_instantiation(
            inst_id,
            target_client_id=new_client_id,
            progress={"step": "configs_copied", "admin_temp_password": temp_password},
        )

        document_ids = await cloner.clone_documents(template, new_client_id)
        await _actualizar_instantiation(
            inst_id,
            progress={
                "step": "documents_queued",
                "documents_total": len(document_ids),
                "admin_temp_password": temp_password,
            },
        )

        # Encolar despues de que el commit de clone_documents ya sea visible
        # para el worker de `documents` (mismo motivo que documents.py).
        for document_id in document_ids:
            ingest_document.delay(str(document_id), str(new_client_id))

        await _actualizar_instantiation(
            inst_id,
            status=InstantiationStatus.COMPLETED.value,
            completed_at=datetime.now(timezone.utc),
        )
        return {"status": "completed", "target_client_id": str(new_client_id)}

    except Exception as exc:
        logger.exception("Clonacion fallida para la instanciacion %s", inst_id)
        await _actualizar_instantiation(
            inst_id,
            status=InstantiationStatus.FAILED.value,
            error_message=str(exc)[:_ERROR_MESSAGE_MAX_CHARS],
            completed_at=datetime.now(timezone.utc),
        )
        raise


@shared_task(
    name="app.tasks.bulk_clone_tenant_from_template",
    bind=True,
    max_retries=1,
    acks_late=True,
    queue="bulk",
    time_limit=1800,
    soft_time_limit=1700,
)
def clone_tenant_from_template(
    self: Any,
    template_id: str,
    instantiation_id: str,
    new_tenant_name: str,
    admin_email: str,
    custom_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Clona un tenant desde un template: configs primero, documentos despues.

    Args:
        self: Instancia de la tarea (bind=True).
        template_id: UUID del template, serializado.
        instantiation_id: UUID de la instanciacion a actualizar, serializado.
        new_tenant_name: Nombre visible del tenant nuevo.
        admin_email: Email del admin del tenant nuevo.
        custom_overrides: Overrides opcionales de configuracion.

    Returns:
        Resumen con el estado final.
    """
    return run_isolated(
        _clone(template_id, instantiation_id, new_tenant_name, admin_email, custom_overrides)
    )
