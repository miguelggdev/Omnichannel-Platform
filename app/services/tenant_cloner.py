"""TenantCloner — snapshot e instanciacion de tenants desde templates (Sprint 10).

Que entra y que no en el snapshot
----------------------------------
Entra lo que es configuracion reutilizable: `agent_configs`, `quick_replies`,
`tags`, metadata de `documents` (el contenido se regenera, nunca se copia el
embedding) y el presupuesto de tokens del mes en curso. NO entra nada de
contactos, conversaciones ni mensajes -- eso es dato de negocio del tenant
origen, no configuracion clonable.

`channel_configs` (la spec original de este sprint la incluye) todavia no
existe como tabla: las credenciales de canal son globales via `.env`
(ADR-030, `DEFAULT_CLIENT_ID`) hasta que Fase 2 le de una tabla propia por
tenant. Mientras tanto no hay nada que clonar ahi -- el admin del tenant
nuevo configura sus canales igual que cualquier otro, a mano.

Por que los embeddings se regeneran y no se copian
----------------------------------------------------
Son dependientes del modelo de embedding (`OPENAI_EMBEDDING_MODEL`): si el
tenant origen los genero con un modelo distinto al que este desplegado tenga
configurado hoy, copiarlos daria vectores incompatibles con las nuevas
busquedas. `clone_documents()` copia el archivo crudo a una ruta nueva y
reencola la misma tarea de ingesta que usa una subida manual
(`app.tasks.document_ingest_document`), no un pipeline aparte.

Por que no hay flujo de invitacion todavia
--------------------------------------------
`users.password_hash` es NOT NULL y este repo no tiene (todavia) un flujo de
invitacion/reset de password. `instantiate()` genera uno aleatorio, lo
hashea para la fila y devuelve el valor en claro una sola vez para que la
task lo dejeen `template_instantiations.progress` (visible solo a
`super_admin`). Es una solucion de paso: la forma correcta es un token de
invitacion de un solo uso, fuera del alcance de este sprint.
"""

import logging
import secrets
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.security import hash_password
from app.models.agent_config import AgentConfig
from app.models.client import Client
from app.models.document import Document
from app.models.quick_reply import QuickReply
from app.models.tag import Tag
from app.models.tenant_template import TenantTemplate
from app.models.token_budget import TokenBudget
from app.models.user import User
from app.services.storage import (
    StorageError,
    build_object_path,
    download_from_storage,
    upload_to_storage,
)

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = "1.0"

# Longitud del slug del tenant nuevo antes del sufijo de desambiguacion.
_SLUG_MAX_LEN = 80


def _slugify(nombre: str) -> str:
    """Deriva un slug legible a partir de un nombre de tenant.

    Args:
        nombre: Nombre visible del tenant.

    Returns:
        Slug en minusculas, solo `[a-z0-9-]`, nunca vacio.
    """
    crudo = "".join(c if c.isalnum() else "-" for c in nombre.lower())
    while "--" in crudo:
        crudo = crudo.replace("--", "-")
    limpio = crudo.strip("-")[:_SLUG_MAX_LEN]
    return limpio or "tenant"


def _mes_actual() -> str:
    """Mes en curso en formato `YYYY-MM` (UTC), igual que `token_budget.py`."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


class TenantCloner:
    """Servicio para crear snapshots de tenants y clonarlos."""

    async def create_snapshot(self, client_id: UUID) -> dict[str, Any]:
        """Arma el snapshot clonable de un tenant.

        Args:
            client_id: Tenant origen.

        Returns:
            Diccionario JSON-serializable con `agent_configs`, `quick_replies`,
            `tags`, `documents`, `token_budget` y `client_settings`. Nunca
            incluye contactos, conversaciones, mensajes ni secretos.
        """
        async with tenant_session(client_id) as session:
            agent_configs = await self._snapshot_agent_configs(session, client_id)
            quick_replies = await self._snapshot_quick_replies(session, client_id)
            tags = await self._snapshot_tags(session, client_id)
            documents = await self._snapshot_documents(session, client_id)
            token_budget = await self._snapshot_token_budget(session, client_id)
            client_settings = await self._snapshot_client_settings(session, client_id)

        return {
            "version": SNAPSHOT_VERSION,
            "snapshot_date": datetime.now(timezone.utc).isoformat(),
            "source_client_id": str(client_id),
            "agent_configs": agent_configs,
            "quick_replies": quick_replies,
            "tags": tags,
            "documents": documents,
            "token_budget": token_budget,
            "client_settings": client_settings,
        }

    async def _snapshot_agent_configs(
        self, session: AsyncSession, client_id: UUID
    ) -> list[dict[str, Any]]:
        rows = (
            await session.execute(select(AgentConfig).where(AgentConfig.client_id == client_id))
        ).scalars()
        return [
            {
                "name": ac.name,
                "system_prompt": ac.system_prompt,
                "welcome_message": ac.welcome_message,
                "model": ac.model,
                "temperature": ac.temperature,
                "max_tokens": ac.max_tokens,
                "training_mode": ac.training_mode,
                "similarity_threshold": ac.similarity_threshold,
                "handoff_message": ac.handoff_message,
                "config": ac.config,
                "is_active": ac.is_active,
            }
            for ac in rows
        ]

    async def _snapshot_quick_replies(
        self, session: AsyncSession, client_id: UUID
    ) -> list[dict[str, Any]]:
        rows = (
            await session.execute(select(QuickReply).where(QuickReply.client_id == client_id))
        ).scalars()
        return [
            {
                "shortcut": qr.shortcut,
                "title": qr.title,
                "content": qr.content,
                "category": qr.category,
            }
            for qr in rows
        ]

    async def _snapshot_tags(self, session: AsyncSession, client_id: UUID) -> list[dict[str, Any]]:
        rows = (await session.execute(select(Tag).where(Tag.client_id == client_id))).scalars()
        return [{"name": tag.name, "color": tag.color} for tag in rows]

    async def _snapshot_documents(
        self, session: AsyncSession, client_id: UUID
    ) -> list[dict[str, Any]]:
        rows = (
            await session.execute(
                select(Document).where(
                    Document.client_id == client_id, Document.status == "completed"
                )
            )
        ).scalars()
        return [
            {
                "title": doc.title,
                "file_url": doc.file_url,
                "file_type": doc.file_type,
                "file_size": doc.file_size,
                "metadata": doc.metadata_,
            }
            for doc in rows
            if doc.file_url
        ]

    async def _snapshot_token_budget(
        self, session: AsyncSession, client_id: UUID
    ) -> dict[str, Any] | None:
        budget = (
            await session.execute(
                select(TokenBudget).where(
                    TokenBudget.client_id == client_id, TokenBudget.month == _mes_actual()
                )
            )
        ).scalar_one_or_none()
        if budget is None:
            return None
        return {"total_budget": budget.total_budget, "model_default": budget.model_default}

    async def _snapshot_client_settings(
        self, session: AsyncSession, client_id: UUID
    ) -> dict[str, Any]:
        client = (await session.execute(select(Client).where(Client.id == client_id))).scalar_one()
        return {"settings": client.settings, "theme_config": client.theme_config}

    async def instantiate(
        self,
        template: TenantTemplate,
        new_tenant_name: str,
        admin_email: str,
        overrides: dict[str, Any] | None = None,
    ) -> tuple[UUID, str]:
        """Crea un tenant nuevo a partir de un template.

        No copia documentos (ver `clone_documents()`): esta funcion solo deja
        el tenant, su admin y su configuracion listos.

        Args:
            template: Template del que se clona.
            new_tenant_name: Nombre visible del tenant nuevo.
            admin_email: Email del usuario admin que se crea para el tenant.
            overrides: `{"agent_configs": {<name>: {<campo>: <valor>, ...}}}`,
                opcional. Sobrescribe campos puntuales de un agent_config por
                nombre antes de copiarlo.

        Returns:
            `(new_client_id, admin_password_temporal)`. El password en claro
            no se guarda en ningun otro lado: es responsabilidad de quien
            llama persistirlo donde corresponda (ver docstring del modulo).
        """
        config = template.config
        new_client_id = uuid4()
        slug = f"{_slugify(new_tenant_name)}-{new_client_id.hex[:8]}"
        temp_password = secrets.token_urlsafe(18)

        async with tenant_session(new_client_id) as session:
            session.add(
                Client(
                    id=new_client_id,
                    name=new_tenant_name,
                    slug=slug,
                    settings=config.get("client_settings", {}).get("settings", {}),
                    theme_config=config.get("client_settings", {}).get("theme_config", {}),
                    is_active=True,
                )
            )
            session.add(
                User(
                    client_id=new_client_id,
                    email=admin_email,
                    password_hash=hash_password(temp_password),
                    first_name="Admin",
                    last_name=new_tenant_name,
                    role="admin",
                )
            )

            overrides_por_nombre = (overrides or {}).get("agent_configs", {})
            for ac_data in config.get("agent_configs", []):
                campos = {**ac_data, **overrides_por_nombre.get(ac_data["name"], {})}
                session.add(AgentConfig(client_id=new_client_id, **campos))

            for qr_data in config.get("quick_replies", []):
                session.add(QuickReply(client_id=new_client_id, **qr_data))

            for tag_data in config.get("tags", []):
                session.add(Tag(client_id=new_client_id, **tag_data))

            token_budget = config.get("token_budget")
            if token_budget:
                session.add(
                    TokenBudget(
                        client_id=new_client_id,
                        month=_mes_actual(),
                        total_budget=token_budget["total_budget"],
                        model_default=token_budget["model_default"],
                    )
                )

        return new_client_id, temp_password

    async def clone_documents(self, template: TenantTemplate, new_client_id: UUID) -> list[UUID]:
        """Copia los archivos de los documentos del template al tenant nuevo.

        Crea la fila `Document` (en `status="pending"`) pero NO encola la
        ingesta: eso es responsabilidad de quien llama (la task), para poder
        hacerlo fuera de esta transaccion, igual que el resto del codebase
        encola despues del commit (ver `documents.py`).

        Un archivo que ya no existe en Storage se salta (se registra, no
        bloquea el resto de la clonacion): el documento original pudo
        borrarse despues de tomar el snapshot.

        Args:
            template: Template origen.
            new_client_id: Tenant nuevo, ya creado por `instantiate()`.

        Returns:
            IDs de los documentos creados, listos para encolar su ingesta.
        """
        documentos_creados: list[UUID] = []

        async with tenant_session(new_client_id) as session:
            for doc_data in template.config.get("documents", []):
                filename = doc_data["file_url"].rsplit("/", 1)[-1]
                new_document_id = uuid4()
                new_path = build_object_path(new_client_id, new_document_id, filename)

                try:
                    contenido = await download_from_storage(doc_data["file_url"])
                    await upload_to_storage(
                        new_path, contenido, doc_data.get("file_type") or "application/octet-stream"
                    )
                except StorageError:
                    logger.warning(
                        "No se pudo copiar el archivo de %s al tenant %s; se omite ese documento",
                        doc_data["file_url"],
                        new_client_id,
                    )
                    continue

                session.add(
                    Document(
                        id=new_document_id,
                        client_id=new_client_id,
                        title=doc_data["title"],
                        file_url=new_path,
                        file_type=doc_data.get("file_type"),
                        file_size=doc_data.get("file_size"),
                        status="pending",
                    )
                )
                documentos_creados.append(new_document_id)

        return documentos_creados
