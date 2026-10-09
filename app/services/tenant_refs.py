"""Comprobar que un id referenciado es de este tenant (Sprint 19).

Las claves foraneas de PostgreSQL **se comprueban sin RLS**: un `assigned_user_id` o un
`call_record_id` de otro tenant pasaria el `FOREIGN KEY` y dejaria una fila apuntando a datos
ajenos. Antes de guardar una referencia que llega del cliente, se comprueba aqui, dentro de la
sesion del tenant (la RLS ya oculta lo ajeno) y con `client_id` explicito.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

#: Roles que no atienden leads ni llamadas comerciales (el profesional clinico, ADR-072).
_ROLES_SIN_LEADS = frozenset({"medical"})


async def usuario_asignable(session: AsyncSession, client_id: UUID, user_id: UUID) -> bool:
    """Si el usuario existe en el tenant, esta activo y puede atender leads.

    Misma regla que la API de leads (`app/api/v1/leads.py::_validar_referencias`).

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        user_id: Usuario.

    Returns:
        `True` si se le puede asignar un deal o una llamada.
    """
    fila = (
        await session.execute(
            select(User.role, User.is_active).where(User.id == user_id, User.client_id == client_id)
        )
    ).first()
    return fila is not None and bool(fila.is_active) and fila.role not in _ROLES_SIN_LEADS


async def existe_en_tenant(session: AsyncSession, modelo: Any, client_id: UUID, id_: UUID) -> bool:
    """Si una fila de `modelo` con ese id existe en el tenant.

    Args:
        session: Sesion con el contexto del tenant fijado.
        modelo: Modelo con `id` y `client_id`.
        client_id: Tenant.
        id_: Id buscado.

    Returns:
        `True` si existe.
    """
    return (
        await session.execute(
            select(modelo.id).where(modelo.id == id_, modelo.client_id == client_id)
        )
    ).first() is not None
