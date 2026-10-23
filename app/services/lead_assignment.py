"""Asignacion de leads a agentes: manual y round-robin (Sprint 17, Dev B).

Estrategia por defecto (PROGRESS.md, "Asignacion de leads"): **round-robin** entre los usuarios
activos con rol `agent` o `supervisor` del tenant (o entre los `user_ids` que el admin fije).
Sin territorio ni carga.

El turno se guarda en `clients.lead_automation_config["assignment_cursor"]` (ultimo usuario
asignado) y se avanza bajo un candado de transaccion por tenant: dos leads que llegan a la vez
no reciben el mismo agente. Se elige el siguiente `id` mayor al del cursor, dando la vuelta; si
el usuario del cursor deja de ser elegible, sigue el siguiente por orden de `id` (sin saltos
raros).

Ningun paso hace `commit`.
"""

import logging
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.models.lead import Lead
from app.models.lead_activity import ACTIVITY_ASSIGNED, ACTIVITY_UNASSIGNED
from app.models.user import User
from app.schemas.lead_automation import AssignmentConfig
from app.services.lead_activity import registrar_actividad
from app.services.lead_qualification import cargar_automatizacion

logger = logging.getLogger(__name__)

#: Roles entre los que rota el round-robin. `admin` y `super_admin` administran, no atienden;
#: `medical` es el profesional clinico y no atiende leads (ADR-072).
ROLES_ROUND_ROBIN = ("agent", "supervisor")
CLAVE_CURSOR = "assignment_cursor"


async def candidatos(
    session: AsyncSession, client_id: UUID, config: AssignmentConfig
) -> list[UUID]:
    """Los usuarios entre los que rotar, ordenados por id.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        config: Configuracion de asignacion.

    Returns:
        Ids de usuarios activos con rol asignable; si `config.user_ids` fija una lista, solo los
        de esa lista que sigan siendo elegibles.
    """
    consulta = select(User.id).where(
        User.client_id == client_id,
        User.is_active.is_(True),
        User.role.in_(ROLES_ROUND_ROBIN),
    )
    if config.user_ids is not None:
        consulta = consulta.where(User.id.in_(config.user_ids))
    return [fila[0] for fila in (await session.execute(consulta.order_by(User.id))).all()]


async def siguiente_round_robin(session: AsyncSession, client_id: UUID) -> UUID | None:
    """Elige el siguiente usuario del turno y avanza el cursor.

    Args:
        session: Sesion con el contexto del tenant fijado (el candado dura hasta su commit).
        client_id: Tenant.

    Returns:
        El usuario, o `None` si no hay nadie elegible.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:clave, 0))"),
        {"clave": f"lead_assign:{client_id}"},
    )
    bruto = (
        await session.execute(select(Client.lead_automation_config).where(Client.id == client_id))
    ).scalar_one_or_none()
    config = cargar_automatizacion(bruto, client_id=client_id).assignment
    ids = await candidatos(session, client_id, config)
    if not ids:
        return None
    cursor: UUID | None = None
    texto = (bruto or {}).get(CLAVE_CURSOR)
    if isinstance(texto, str):
        try:
            cursor = UUID(texto)
        except ValueError:
            cursor = None
    elegido = next((i for i in ids if cursor is not None and i > cursor), ids[0])
    await session.execute(
        text(
            "UPDATE clients SET lead_automation_config = "
            "jsonb_set(lead_automation_config, '{assignment_cursor}', "
            "to_jsonb(CAST(:usuario AS text)), true) WHERE id = :cid"
        ),
        {"usuario": str(elegido), "cid": client_id},
    )
    return elegido


def asignar(
    session: AsyncSession, lead: Lead, user_id: UUID | None, *, actor_id: UUID | None
) -> bool:
    """Pone (o quita) el responsable del lead y lo deja en el historial.

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead (se modifica en sitio).
        user_id: Nuevo responsable; `None` para quitarlo.
        actor_id: Quien lo hace; `None` si es el sistema (round-robin automatico).

    Returns:
        `True` si el responsable cambio.
    """
    anterior = lead.assigned_user_id
    if anterior == user_id:
        return False
    lead.assigned_user_id = user_id
    registrar_actividad(
        session,
        client_id=lead.client_id,
        lead_id=lead.id,
        tipo=ACTIVITY_ASSIGNED if user_id else ACTIVITY_UNASSIGNED,
        user_id=actor_id,
        from_user=anterior,
        to_user=user_id,
        automatic=actor_id is None,
    )
    return True


async def asignar_automaticamente(session: AsyncSession, lead: Lead) -> UUID | None:
    """Asigna un lead nuevo sin responsable si el tenant tiene la asignacion automatica activa.

    Es un extra del alta: va en un punto de guardado (`SAVEPOINT`) y **un fallo aqui nunca
    cuesta el lead** (la captura publica y el alta manual siguen adelante sin responsable). La
    importacion masiva no la usa: ahi el usuario elige el responsable del lote.

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead recien creado (se modifica en sitio).

    Returns:
        El usuario asignado, o `None` si no hizo nada (apagada, ya tenia responsable, no hay
        nadie elegible o fallo).
    """
    if lead.assigned_user_id is not None:
        return None
    try:
        async with session.begin_nested():
            bruto = (
                await session.execute(
                    select(Client.lead_automation_config).where(Client.id == lead.client_id)
                )
            ).scalar_one_or_none()
            if not cargar_automatizacion(bruto, client_id=lead.client_id).assignment.auto_assign:
                return None
            elegido = await siguiente_round_robin(session, lead.client_id)
            if elegido is None:
                logger.warning(
                    "Asignacion automatica sin usuarios elegibles (client_id=%s)", lead.client_id
                )
                return None
            asignar(session, lead, elegido, actor_id=None)
            await session.flush()
            return elegido
    except Exception:
        logger.exception("Fallo la asignacion automatica (client_id=%s)", lead.client_id)
        return None
