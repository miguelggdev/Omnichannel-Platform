"""Endpoints de administracion: cumplimiento de RGPD sobre un contacto.

GET    /api/v1/admin/contacts/{id}/export       todos sus datos personales en JSON
DELETE /api/v1/admin/contacts/{id}/gdpr-delete  anonimiza esos datos

Por que anonimizar y no borrar (spec §10.2)
-------------------------------------------
Borrar la fila del contacto arrastraria sus conversaciones y mensajes por las
FK, y con ellos el historico agregado del tenant (cuantas conversaciones hubo,
cuantas se resolvieron). El derecho de supresion se cumple igual quitando los
datos personales y dejando el esqueleto sin identificar.

Las dos operaciones quedan registradas en `audit_logs` sin que este modulo haga
nada: los UPDATE sobre `contacts`, `messages` y `conversations` los captura el
trigger de la migracion 006, con el usuario que los pidio, que
`AuditContextMiddleware` publica en `app.current_user_id`.

El propio UPDATE de anonimizacion tambien queda auditado
-----------------------------------------------------------
Eso es un problema, no una curiosidad: el trigger guarda `to_jsonb(OLD)` antes
de anonimizar, asi que el nombre y el contenido real de los mensajes quedarian
recuperables para siempre en `audit_logs` — el endpoint diria "anonimizado" sin
que fuera cierto. `gdpr_delete_contact()` por eso redacta, al final de la misma
transaccion, las filas de `audit_logs` de `contacts`/`messages` que le
pertenecen a este contacto (ver `_redactar_rastro_de_auditoria()`). El resto
de la fila de auditoria (quien, cuando, que tabla) se conserva: lo que RGPD
exige borrar es el dato personal, no la prueba de que hubo una operacion.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import bindparam, case, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import (
    CONFLICT,
    DUPLICATE,
    FORBIDDEN,
    NOT_FOUND,
    VALIDATION_ERROR,
    AppException,
)
from app.core.security import hash_password
from app.models.contact import Contact
from app.models.contact_identifier import ContactIdentifier
from app.models.contact_tag import ContactTag
from app.models.conversation import Conversation
from app.models.internal_note import InternalNote
from app.models.lead import Lead
from app.models.message import Message
from app.models.satisfaction_survey import SatisfactionSurvey
from app.models.tag import Tag
from app.models.user import User
from app.schemas.csat import CsatSummaryResponse
from app.schemas.user import UserCreate, UserListResponse, UserResponse, UserUpdate
from app.services.deals import deals_para_export
from app.services.lead_privacy import lead_a_dict, lead_esta_anonimizado
from app.services.lead_score_service import scores_para_export
from app.services.lead_sequences import inscripciones_para_export
from app.services.lead_suppression import suprimir_leads
from app.services.scheduled_calls import llamadas_para_export

logger = logging.getLogger(__name__)

router = APIRouter()

_GDPR_ROLES = ("super_admin", "admin")
_CSAT_ROLES = ("super_admin", "admin", "supervisor")
_USER_ADMIN_ROLES = ("super_admin", "admin")

# Texto con el que se reemplazan los datos personales.
ANONIMIZADO = "[ELIMINADO]"
CONTENIDO_ANONIMIZADO = "[CONTENIDO ELIMINADO POR SOLICITUD RGPD]"


async def _get_contact_or_404(session: AsyncSession, contact_id: UUID, client_id: UUID) -> Contact:
    """Carga un contacto del tenant activo o levanta 404.

    Args:
        session: Sesion con contexto de tenant.
        contact_id: Contacto buscado.
        client_id: Tenant propietario.

    Returns:
        El contacto.

    Raises:
        AppException: 404 si no existe para este tenant.
    """
    contact = (
        await session.execute(
            select(Contact).where(Contact.id == contact_id, Contact.client_id == client_id)
        )
    ).scalar_one_or_none()
    if contact is None:
        raise AppException(
            status_code=404,
            error_code=NOT_FOUND,
            message="Contacto no encontrado",
        )
    return contact


@router.get("/contacts/{contact_id}/export")
async def export_contact_data(
    contact_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_GDPR_ROLES)),
) -> dict[str, Any]:
    """Exporta en JSON todo lo que la plataforma guarda sobre un contacto.

    Cubre el derecho de acceso y portabilidad: contacto, identificadores por
    canal, etiquetas, notas internas y todas sus conversaciones con sus mensajes.

    Se devuelve entero, sin paginar, porque un export parcial no cumple el
    proposito. Las conversaciones de un contacto son decenas, no millones.

    Args:
        contact_id: Contacto a exportar.
        user: Usuario autenticado; solo admin y super_admin.

    Returns:
        Estructura JSON con todos los datos asociados.

    Raises:
        AppException: 404 si el contacto no existe para este tenant.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        contact = await _get_contact_or_404(session, contact_id, client_id)

        identificadores = (
            (
                await session.execute(
                    select(ContactIdentifier).where(
                        ContactIdentifier.client_id == client_id,
                        ContactIdentifier.contact_id == contact_id,
                    )
                )
            )
            .scalars()
            .all()
        )

        etiquetas = (
            await session.execute(
                select(Tag.name)
                .join(ContactTag, ContactTag.tag_id == Tag.id)
                .where(
                    ContactTag.client_id == client_id,
                    ContactTag.contact_id == contact_id,
                )
                .order_by(Tag.name.asc())
            )
        ).all()

        notas = (
            (
                await session.execute(
                    select(InternalNote)
                    .where(
                        InternalNote.client_id == client_id,
                        InternalNote.contact_id == contact_id,
                    )
                    .order_by(InternalNote.created_at.asc())
                )
            )
            .scalars()
            .all()
        )

        conversaciones = (
            (
                await session.execute(
                    select(Conversation)
                    .where(
                        Conversation.client_id == client_id,
                        Conversation.contact_id == contact_id,
                    )
                    .order_by(Conversation.created_at.asc())
                )
            )
            .scalars()
            .all()
        )

        # Todos los mensajes de una vez y agrupados en memoria: una consulta por
        # conversacion seria N+1 sobre la tabla mas grande del sistema.
        ids_conversaciones = [c.id for c in conversaciones]
        mensajes_por_conversacion: dict[UUID, list[Message]] = {
            cid: [] for cid in ids_conversaciones
        }
        if ids_conversaciones:
            mensajes = (
                (
                    await session.execute(
                        select(Message)
                        .where(
                            Message.client_id == client_id,
                            Message.conversation_id.in_(ids_conversaciones),
                        )
                        .order_by(Message.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
            for mensaje in mensajes:
                mensajes_por_conversacion[mensaje.conversation_id].append(mensaje)

        leads = (
            (
                await session.execute(
                    select(Lead)
                    .where(Lead.client_id == client_id, Lead.contact_id == contact_id)
                    .order_by(Lead.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        scores = await scores_para_export(session, client_id, [lead.id for lead in leads])
        ids_leads = [lead.id for lead in leads]
        secuencias = await inscripciones_para_export(session, client_id, ids_leads)
        deals = await deals_para_export(session, client_id, ids_leads)
        llamadas = await llamadas_para_export(session, client_id, ids_leads)

        return {
            "export_date": datetime.now(timezone.utc).isoformat(),
            "contact": {
                "id": str(contact.id),
                "first_name": contact.first_name,
                "last_name": contact.last_name,
                "display_name": contact.display_name,
                "metadata": contact.metadata_ or {},
                "is_gdpr_deleted": contact.is_gdpr_deleted,
                "gdpr_deleted_at": (
                    contact.gdpr_deleted_at.isoformat() if contact.gdpr_deleted_at else None
                ),
                "created_at": contact.created_at.isoformat(),
            },
            "identifiers": [
                {
                    "id": str(i.id),
                    "channel": i.channel,
                    "identifier_value": i.identifier_value,
                    "created_at": i.created_at.isoformat(),
                }
                for i in identificadores
            ],
            "tags": [fila.name for fila in etiquetas],
            # Incluye los leads con soft delete: borrar un lead no lo quita de la base. Y su
            # historial de scores (Sprint 17): es perfilado de la persona. Y las secuencias por
            # las que paso (Sprint 18): es tratamiento de sus datos para contactarla.
            "leads": [
                {
                    **lead_a_dict(lead),
                    "score_history": scores[lead.id],
                    "sequence_enrollments": secuencias[lead.id],
                    # Sprint 19: deals y llamadas agendadas (notas y motivos son texto libre).
                    "deals": deals[lead.id],
                    "scheduled_calls": llamadas[lead.id],
                }
                for lead in leads
            ],
            "notes": [
                {
                    "id": str(n.id),
                    "content": n.content,
                    "author_id": str(n.author_id),
                    "created_at": n.created_at.isoformat(),
                }
                for n in notas
            ],
            "conversations": [
                {
                    "id": str(c.id),
                    "channel": c.channel,
                    "status": c.status,
                    "subject": c.subject,
                    "created_at": c.created_at.isoformat(),
                    "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
                    "messages": [
                        {
                            "id": str(m.id),
                            "direction": m.direction,
                            "message_type": m.message_type,
                            "content": m.content,
                            "media_url": m.media_url,
                            "sender_type": m.sender_type,
                            "created_at": m.created_at.isoformat(),
                        }
                        for m in mensajes_por_conversacion[c.id]
                    ],
                }
                for c in conversaciones
            ],
        }


async def _redactar_rastro_de_auditoria(
    session: AsyncSession,
    client_id: UUID,
    contact_id: UUID,
    mensaje_ids: list[UUID],
    conversacion_ids: list[UUID],
) -> None:
    """Redacta el dato personal dentro de las filas de `audit_logs` ya escritas.

    El trigger de la migracion 006 ya insertó, antes de que este endpoint
    corriera, filas de auditoria con el nombre/contenido real (el INSERT
    original del contacto o del mensaje). Y el propio UPDATE de anonimizacion
    de esta misma peticion va a generar una fila mas, con `old_values` = los
    datos que se acaban de reemplazar. Las dos quedan cubiertas porque este
    UPDATE corre sobre **todas** las filas de auditoria de este contacto/sus
    mensajes, sin importar cuando se escribieron.

    Se sobreescriben las claves del JSONB con `||` en vez de borrarlas: la fila
    de auditoria conserva su forma (que columnas tenia la tabla en ese momento)
    para quien la lea despues, solo que con el valor sensible reemplazado — el
    mismo criterio que ya usa `ANONIMIZADO`/`CONTENIDO_ANONIMIZADO` en las
    columnas reales. `audit_logs` no esta en `AUDITED_TABLES` (migracion 006),
    asi que este UPDATE no dispara el trigger sobre si mismo.

    Args:
        session: Sesion con contexto de tenant ya aplicado.
        client_id: Tenant propietario (filtro explicito ademas de RLS).
        contact_id: Contacto cuyas filas de `contacts` hay que redactar.
        mensaje_ids: Mensajes entrantes anonimizados cuyas filas de `messages`
            hay que redactar. Puede ser una lista vacia.
        conversacion_ids: Conversaciones del contacto, cuyas filas de
            `conversations` llevan el `subject` (texto libre que escribe un
            agente y que el export RGPD entrega como dato del contacto). Solo
            se redacta donde el asunto no era nulo.
    """
    redaccion_contacto = {
        "first_name": ANONIMIZADO,
        "last_name": ANONIMIZADO,
        "display_name": ANONIMIZADO,
        "metadata": {},
    }
    await session.execute(
        text("""
            UPDATE audit_logs
            SET old_values = CASE WHEN old_values IS NOT NULL
                    THEN old_values || CAST(:redaccion AS jsonb) ELSE NULL END,
                new_values = CASE WHEN new_values IS NOT NULL
                    THEN new_values || CAST(:redaccion AS jsonb) ELSE NULL END
            WHERE client_id = :client_id
              AND table_name = 'contacts'
              AND record_id = :contact_id
        """),
        {
            "redaccion": json.dumps(redaccion_contacto),
            "client_id": str(client_id),
            "contact_id": str(contact_id),
        },
    )

    if conversacion_ids:
        redaccion_asunto = {"subject": ANONIMIZADO}
        stmt_asunto = text("""
            UPDATE audit_logs
            SET old_values = CASE WHEN old_values->>'subject' IS NOT NULL
                    THEN old_values || CAST(:redaccion AS jsonb) ELSE old_values END,
                new_values = CASE WHEN new_values->>'subject' IS NOT NULL
                    THEN new_values || CAST(:redaccion AS jsonb) ELSE new_values END
            WHERE client_id = :client_id
              AND table_name = 'conversations'
              AND record_id IN :conversacion_ids
        """).bindparams(bindparam("conversacion_ids", expanding=True))
        await session.execute(
            stmt_asunto,
            {
                "redaccion": json.dumps(redaccion_asunto),
                "client_id": str(client_id),
                "conversacion_ids": [str(c) for c in conversacion_ids],
            },
        )

    if not mensaje_ids:
        return

    redaccion_mensaje: dict[str, Any] = {
        "content": CONTENIDO_ANONIMIZADO,
        "media_url": None,
        "metadata": {},
    }
    stmt = text("""
        UPDATE audit_logs
        SET old_values = CASE WHEN old_values IS NOT NULL
                THEN old_values || CAST(:redaccion AS jsonb) ELSE NULL END,
            new_values = CASE WHEN new_values IS NOT NULL
                THEN new_values || CAST(:redaccion AS jsonb) ELSE NULL END
        WHERE client_id = :client_id
          AND table_name = 'messages'
          AND record_id IN :mensaje_ids
    """).bindparams(bindparam("mensaje_ids", expanding=True))
    await session.execute(
        stmt,
        {
            "redaccion": json.dumps(redaccion_mensaje),
            "client_id": str(client_id),
            "mensaje_ids": [str(m) for m in mensaje_ids],
        },
    )


async def _redactar_payloads_de_webhooks_salientes(
    session: AsyncSession, client_id: UUID, contact_id: UUID
) -> int:
    """Redacta el texto de mensaje dentro de `outgoing_webhook_logs.payload`.

    Pendiente anotado en ADR-065: `payload` guarda el evento completo, que en
    `message.received` incluye el texto que escribio el contacto — dato
    personal que el borrado RGPD tiene que alcanzar, igual que ya hace con
    `messages.content` (`CONTENIDO_ANONIMIZADO`). Se sobreescribe solo la clave
    `data.content` con `jsonb_set`, no el payload entero: el resto (evento,
    ids, timestamp) es el rastro de que el envio ocurrio, no un dato personal,
    y algunos eventos (`conversation.resolved`, `appointment.created`) ni
    siquiera tienen `content`.

    Solo `message.received`, nunca `message.sent`: igual que el bloque de
    mensajes de `gdpr_delete_contact()` mas arriba filtra
    `Message.direction == "inbound"` y deja intactos los salientes ("lo que
    respondio la empresa"), esta redaccion tiene que respetar la misma
    frontera — `message.sent` tambien lleva `data.content` (lo que el bot le
    escribio al contacto) y `data.contact_id` del mismo contacto, y redactarlo
    borraria el rastro de lo que la empresa dijo, no un dato del contacto.

    `jsonb_typeof(...) <> 'null'` ademas de `? 'content'`: el operador `?` de
    JSONB solo verifica que la clave exista, no que su valor no sea el `null`
    de JSON — un mensaje de solo medios sin caption serializa `data.content`
    como `null`, y sin este chequeo la redaccion lo sobreescribiria con el
    aviso de "eliminado" aunque nunca hubo texto. Ojo: `... -> 'content' IS
    NOT NULL` **no sirve** para esto — el operador `->` devuelve un valor
    `jsonb` que representa el `null` de JSON, y eso no es un `NULL` de SQL, asi
    que `IS NOT NULL` da verdadero igual (se comprobo con un test de
    integracion que lo hizo fallar antes de este cambio).

    No pasa por `audit_logs`: `outgoing_webhook_logs` no esta en las tablas que
    audita el trigger de la migracion 006 (es un log de entregas, no una
    entidad de negocio), asi que no hace falta una redaccion equivalente ahi.

    Args:
        session: Sesion con contexto de tenant ya aplicado.
        client_id: Tenant propietario (filtro explicito ademas de RLS).
        contact_id: Contacto cuyos payloads hay que redactar.

    Returns:
        Cuantas filas se redactaron.
    """
    resultado: Any = await session.execute(
        text("""
            UPDATE outgoing_webhook_logs
            SET payload = jsonb_set(payload, '{data,content}', to_jsonb(CAST(:contenido AS text)))
            WHERE client_id = :client_id
              AND event = 'message.received'
              AND payload -> 'data' ->> 'contact_id' = :contact_id
              AND payload -> 'data' ? 'content'
              AND jsonb_typeof(payload -> 'data' -> 'content') <> 'null'
        """),
        {
            "contenido": CONTENIDO_ANONIMIZADO,
            "client_id": str(client_id),
            "contact_id": str(contact_id),
        },
    )
    return int(resultado.rowcount or 0)


@router.delete("/contacts/{contact_id}/gdpr-delete")
async def gdpr_delete_contact(
    contact_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_GDPR_ROLES)),
) -> dict[str, Any]:
    """Anonimiza los datos personales de un contacto, sin borrar las filas.

    Qué se anonimiza:
      - nombre, apellido, display_name y `metadata` del contacto;
      - el valor de cada identificador de canal (telefono, PSID, usuario);
      - el contenido y el media de los mensajes **entrantes**, que son las
        palabras del propio contacto;
      - las notas internas que el equipo escribio sobre el;
      - el asunto (`subject`) de sus conversaciones, texto libre de un agente.

    Qué NO se anonimiza, a proposito: los mensajes salientes. Son el registro de
    lo que la empresa respondio, no datos aportados por el contacto, y borrarlos
    destruiria la trazabilidad de la atencion. Si en un caso concreto una
    respuesta contiene datos personales, se edita ese mensaje aparte.

    La operacion es idempotente: repetirla sobre un contacto ya anonimizado
    responde 400, para que un doble clic no reescriba `gdpr_deleted_at` y se
    pierda la fecha real de la supresion.

    Args:
        contact_id: Contacto a anonimizar.
        user: Usuario autenticado; solo admin y super_admin.

    Returns:
        Resumen de cuantas filas se tocaron.

    Raises:
        AppException: 404 si no existe, 400 si ya estaba anonimizado.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        contact = await _get_contact_or_404(session, contact_id, client_id)

        if contact.is_gdpr_deleted:
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message=(
                    "El contacto ya fue anonimizado el "
                    f"{contact.gdpr_deleted_at.isoformat() if contact.gdpr_deleted_at else '?'}"
                ),
            )

        ahora = datetime.now(timezone.utc)

        contact.first_name = ANONIMIZADO
        contact.last_name = ANONIMIZADO
        contact.display_name = ANONIMIZADO
        contact.metadata_ = {}
        contact.is_gdpr_deleted = True
        contact.gdpr_deleted_at = ahora

        identificadores = (
            (
                await session.execute(
                    select(ContactIdentifier).where(
                        ContactIdentifier.client_id == client_id,
                        ContactIdentifier.contact_id == contact_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for identificador in identificadores:
            # El sufijo con el id mantiene unico el valor: la tabla tiene
            # unique(client_id, channel, identifier_hash) y un contacto puede
            # tener dos identificadores del mismo canal. El hash lo recalcula
            # solo el listener `before_update` del modelo (Sprint 8): esta
            # asignacion pasa por el ORM, no por un UPDATE masivo.
            identificador.identifier_value = f"[ELIMINADO-{identificador.id.hex[:8]}]"

        conversaciones = (
            (
                await session.execute(
                    select(Conversation.id).where(
                        Conversation.client_id == client_id,
                        Conversation.contact_id == contact_id,
                    )
                )
            )
            .scalars()
            .all()
        )

        mensajes_anonimizados = 0
        mensaje_ids: list[UUID] = []
        if conversaciones:
            mensajes = (
                (
                    await session.execute(
                        select(Message).where(
                            Message.client_id == client_id,
                            Message.conversation_id.in_(conversaciones),
                            Message.direction == "inbound",
                        )
                    )
                )
                .scalars()
                .all()
            )
            for mensaje in mensajes:
                mensaje.content = CONTENIDO_ANONIMIZADO
                mensaje.media_url = None
                mensaje.metadata_ = {}
                mensajes_anonimizados += 1
                mensaje_ids.append(mensaje.id)

        notas = (
            (
                await session.execute(
                    select(InternalNote).where(
                        InternalNote.client_id == client_id,
                        InternalNote.contact_id == contact_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for nota in notas:
            nota.content = CONTENIDO_ANONIMIZADO

        # Los leads vinculados guardan nombre, email y telefono del mismo titular (Sprint 16).
        leads = (
            (
                await session.execute(
                    select(Lead).where(Lead.client_id == client_id, Lead.contact_id == contact_id)
                )
            )
            .scalars()
            .all()
        )
        await suprimir_leads(
            session,
            client_id,
            leads,
            user_id=UUID(str(user["user_id"])),
            via="contact",
            ahora=datetime.now(timezone.utc),
        )

        if conversaciones:
            # El asunto es texto libre de un agente y puede llevar datos del
            # contacto; el export RGPD ya lo entrega, asi que la supresion
            # tiene que alcanzarlo. Donde es NULL se deja NULL.
            await session.execute(
                update(Conversation)
                .where(
                    Conversation.client_id == client_id,
                    Conversation.id.in_(conversaciones),
                    Conversation.subject.is_not(None),
                )
                .values(subject=ANONIMIZADO)
            )

        await session.flush()

        # Despues del flush: el UPDATE de anonimizacion de arriba ya disparo el
        # trigger y ya escribio su propia fila en audit_logs con el dato real
        # en old_values. Redactar antes dejaria esa fila afuera.
        await _redactar_rastro_de_auditoria(
            session, client_id, contact_id, mensaje_ids, list(conversaciones)
        )
        await _redactar_payloads_de_webhooks_salientes(session, client_id, contact_id)

    logger.info(
        "RGPD: contacto %s anonimizado por %s (tenant %s)",
        contact_id,
        user["user_id"],
        client_id,
    )
    return {
        "status": "success",
        "message": "Datos del contacto anonimizados",
        "contact_id": str(contact_id),
        "gdpr_deleted_at": ahora.isoformat(),
        "identifiers_anonymized": len(identificadores),
        "messages_anonymized": mensajes_anonimizados,
        "notes_anonymized": len(notas),
        "leads_anonymized": len(leads),
    }


# ─── RGPD de leads (Sprint 16) ────────────────────────────────────────────────


async def _lead_o_404(session: AsyncSession, lead_id: UUID, client_id: UUID) -> Lead:
    """Busca un lead del tenant, **borrado o no** (la supresion debe alcanzar a ambos)."""
    lead: Lead | None = (
        await session.execute(select(Lead).where(Lead.id == lead_id, Lead.client_id == client_id))
    ).scalar_one_or_none()
    if lead is None:
        raise AppException(status_code=404, error_code=NOT_FOUND, message="Lead no encontrado")
    return lead


@router.get("/leads/{lead_id}/export")
async def export_lead_data(
    lead_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_GDPR_ROLES)),
) -> dict[str, Any]:
    """Exporta en JSON todo lo que la plataforma guarda sobre un lead (derecho de acceso).

    Args:
        lead_id: Lead a exportar (tambien si tiene soft delete).
        user: Usuario autenticado; solo admin y super_admin.

    Returns:
        Fecha del export y los datos del lead.

    Raises:
        AppException: 404 si no existe para este tenant.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        lead = await _lead_o_404(session, lead_id, client_id)
        scores = await scores_para_export(session, client_id, [lead.id])
        secuencias = await inscripciones_para_export(session, client_id, [lead.id])
        deals = await deals_para_export(session, client_id, [lead.id])
        llamadas = await llamadas_para_export(session, client_id, [lead.id])
        return {
            "export_date": datetime.now(timezone.utc).isoformat(),
            "lead": {
                **lead_a_dict(lead),
                "score_history": scores[lead.id],
                "sequence_enrollments": secuencias[lead.id],
                "deals": deals[lead.id],
                "scheduled_calls": llamadas[lead.id],
            },
        }


@router.delete("/leads/{lead_id}/gdpr-delete")
async def gdpr_delete_lead(
    lead_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_GDPR_ROLES)),
) -> dict[str, Any]:
    """Anonimiza los datos personales de un lead (derecho de supresion).

    El `DELETE /leads/{id}` normal es un soft delete y deja los datos en la base; esto es la
    supresion de verdad. Ver `app/services/lead_privacy.py` para que se quita y que se
    conserva. Repetirla responde 400.

    Args:
        lead_id: Lead a anonimizar (tambien si tiene soft delete).
        user: Usuario autenticado; solo admin y super_admin.

    Returns:
        Confirmacion con el id del lead.

    Raises:
        AppException: 404 si no existe; 400 si ya estaba anonimizado.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        lead = await _lead_o_404(session, lead_id, client_id)
        if lead_esta_anonimizado(lead):
            raise AppException(
                status_code=400, error_code=VALIDATION_ERROR, message="El lead ya fue anonimizado"
            )
        await suprimir_leads(
            session,
            client_id,
            [lead],
            user_id=UUID(str(user["user_id"])),
            via="lead",
            ahora=datetime.now(timezone.utc),
        )
    logger.info("RGPD: lead %s anonimizado por %s (tenant %s)", lead_id, user["user_id"], client_id)
    return {"status": "success", "message": "Datos del lead anonimizados", "lead_id": str(lead_id)}


# ─── CSAT (Sprint 11, Dev B) ──────────────────────────────────────────────────


@router.get("/csat/summary", response_model=CsatSummaryResponse)
async def csat_summary(
    days: int = Query(default=30, ge=1, le=365),
    user: dict[str, Any] = Depends(require_role(*_CSAT_ROLES)),
) -> CsatSummaryResponse:
    """Resumen de CSAT del tenant en los ultimos `days` dias.

    `promoters` (rating >= 4) y `detractors` (rating <= 2) usan el corte que
    NPS/CSAT convencional aplica a una escala 1-5, no el 0-10 de NPS puro.

    Args:
        days: Ventana de dias hacia atras; por defecto 30.
        user: Usuario autenticado.

    Returns:
        Total enviado y respondido, tasa de respuesta, promedio, promotores y
        detractores, distribucion por rating y tendencia semanal de respuestas.
    """
    client_id: UUID = user["client_id"]
    desde = datetime.now(timezone.utc) - timedelta(days=days)

    async with tenant_session(client_id) as session:
        agregado = (
            await session.execute(
                select(
                    func.count(SatisfactionSurvey.id).label("total"),
                    func.count(SatisfactionSurvey.rating).label("respuestas"),
                    func.avg(SatisfactionSurvey.rating).label("promedio"),
                    func.count(case((SatisfactionSurvey.rating >= 4, 1))).label("promotores"),
                    func.count(case((SatisfactionSurvey.rating <= 2, 1))).label("detractores"),
                ).where(
                    SatisfactionSurvey.client_id == client_id,
                    SatisfactionSurvey.sent_at >= desde,
                )
            )
        ).one()

        distribucion = (
            await session.execute(
                select(SatisfactionSurvey.rating, func.count(SatisfactionSurvey.id))
                .where(
                    SatisfactionSurvey.client_id == client_id,
                    SatisfactionSurvey.sent_at >= desde,
                    SatisfactionSurvey.rating.is_not(None),
                )
                .group_by(SatisfactionSurvey.rating)
                .order_by(SatisfactionSurvey.rating)
            )
        ).all()

        tendencia = (
            await session.execute(
                select(
                    func.date_trunc("week", SatisfactionSurvey.responded_at).label("semana"),
                    func.avg(SatisfactionSurvey.rating).label("promedio"),
                    func.count(SatisfactionSurvey.id).label("cantidad"),
                )
                .where(
                    SatisfactionSurvey.client_id == client_id,
                    SatisfactionSurvey.responded_at >= desde,
                    SatisfactionSurvey.rating.is_not(None),
                )
                .group_by(text("semana"))
                .order_by(text("semana"))
            )
        ).all()

    total = agregado.total or 0
    respuestas = agregado.respuestas or 0
    tasa_respuesta = (respuestas / total * 100) if total else 0.0

    return CsatSummaryResponse(
        period_days=days,
        total_surveys_sent=total,
        total_responses=respuestas,
        response_rate=round(tasa_respuesta, 1),
        average_rating=round(float(agregado.promedio or 0), 2),
        promoters=agregado.promotores or 0,
        detractors=agregado.detractores or 0,
        distribution={fila[0]: fila[1] for fila in distribucion},
        weekly_trend=[
            {"week": semana.isoformat(), "avg_rating": round(float(promedio), 2), "count": cantidad}
            for semana, promedio, cantidad in tendencia
        ],
    )


@router.post("/users", response_model=UserResponse, status_code=201)
async def create_user(
    data: UserCreate,
    user: dict[str, Any] = Depends(require_role(*_USER_ADMIN_ROLES)),
) -> UserResponse:
    """Da de alta un usuario en el tenant del administrador que llama.

    Es el unico camino para crear un usuario `medical` (ADR-073) sin tocar la
    base. El tenant sale del token, nunca del cuerpo, y `UserCreate` no admite
    `super_admin`, asi que un admin no puede crear usuarios de otro tenant ni
    escalar privilegios.

    Args:
        data: Email, password inicial, nombre y rol.
        user: Usuario autenticado; solo admin o super_admin.

    Returns:
        El usuario creado, sin `password_hash`.

    Raises:
        AppException: 409 si el email ya esta registrado. `users.email` es unico
            en toda la plataforma y RLS oculta los de otros tenants, asi que la
            unica comprobacion fiable es la restriccion de la base.
    """
    client_id: UUID = user["client_id"]
    # bcrypt es CPU: fuera del event loop (CLAUDE.md, regla 4).
    password_hash = await asyncio.to_thread(hash_password, data.password)

    try:
        async with tenant_session(client_id) as session:
            nuevo = User(
                client_id=client_id,
                email=str(data.email),
                password_hash=password_hash,
                first_name=data.first_name,
                last_name=data.last_name,
                role=data.role,
            )
            session.add(nuevo)
            await session.flush()
            await session.refresh(nuevo)
            respuesta = UserResponse.model_validate(nuevo)
    except IntegrityError as exc:
        raise AppException(
            status_code=409,
            error_code=DUPLICATE,
            message="Ya existe un usuario con ese email",
        ) from exc

    logger.info(
        "Usuario %s creado con rol %s por %s (client_id=%s)",
        respuesta.id,
        respuesta.role,
        user.get("user_id"),
        client_id,
    )
    return respuesta


@router.get("/users", response_model=UserListResponse)
async def list_users(
    search: str | None = Query(default=None, description="Busca en nombre, apellido y email"),
    role: str | None = Query(default=None, description="Filtra por rol"),
    is_active: bool | None = Query(default=None, description="Solo activos o solo inactivos"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_role(*_USER_ADMIN_ROLES)),
) -> UserListResponse:
    """Lista los usuarios del tenant, de los mas recientes a los mas antiguos.

    Args:
        search: Texto a buscar (sin distinguir mayusculas) en nombre, apellido o email.
        role: Rol exacto.
        is_active: `true` solo activos, `false` solo desactivados.
        page: Pagina, empezando en 1.
        page_size: Tamano de pagina, maximo 100.
        user: Usuario autenticado; solo admin o super_admin.

    Returns:
        Pagina de usuarios, sin `password_hash`, y el total de coincidencias.
    """
    client_id: UUID = user["client_id"]
    filtros = [User.client_id == client_id]
    if search:
        patron = f"%{search.strip()}%"
        filtros.append(
            User.first_name.ilike(patron) | User.last_name.ilike(patron) | User.email.ilike(patron)
        )
    if role:
        filtros.append(User.role == role)
    if is_active is not None:
        filtros.append(User.is_active == is_active)

    async with tenant_session(client_id) as session:
        total = (
            await session.execute(select(func.count()).select_from(User).where(*filtros))
        ).scalar_one()
        filas = (
            (
                await session.execute(
                    select(User)
                    .where(*filtros)
                    .order_by(User.created_at.desc(), User.id)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        items = [UserResponse.model_validate(f) for f in filas]

    return UserListResponse(items=items, total=int(total), page=page, page_size=page_size)


# Roles que pueden administrar el tenant: lo que no puede quedarse a cero.
_ROLES_DE_ADMINISTRACION = ("admin", "super_admin")


@router.put("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: UUID,
    data: UserUpdate,
    user: dict[str, Any] = Depends(require_role(*_USER_ADMIN_ROLES)),
) -> UserResponse:
    """Edita, cambia de rol, desactiva o restablece la contrasena de un usuario del tenant.

    Reglas, para que nadie se quede sin acceso ni escale privilegios:

    - Nadie cambia su **propio** rol ni se desactiva (otro administrador puede hacerlo).
    - Un `super_admin` es de la plataforma: solo otro `super_admin` puede tocarlo.
    - El tenant no puede quedarse sin ningun administrador activo.
    - Desactivar a alguien corta su acceso: no puede iniciar sesion ni renovarla
      (`/auth/refresh` revalida `is_active`). El access token que ya tenga sigue valiendo
      hasta que caduque (`JWT_EXPIRATION_MINUTES`, 30 por defecto).

    Args:
        user_id: Usuario a modificar.
        data: Campos a cambiar; los omitidos no se tocan.
        user: Usuario autenticado; solo admin o super_admin.

    Returns:
        El usuario ya modificado, sin `password_hash`.

    Raises:
        AppException: 404 si no existe en este tenant; 403 si es un `super_admin` y quien
            llama no lo es; 400 si intenta cambiarse su propio rol o desactivarse; 409 si
            dejaria al tenant sin administrador activo.
    """
    client_id: UUID = user["client_id"]
    es_uno_mismo = user_id == user["user_id"]
    password_hash = await asyncio.to_thread(hash_password, data.password) if data.password else None

    async with tenant_session(client_id) as session:
        objetivo = (
            await session.execute(
                select(User).where(User.id == user_id, User.client_id == client_id)
            )
        ).scalar_one_or_none()
        if objetivo is None:
            raise AppException(
                status_code=404, error_code=NOT_FOUND, message="Usuario no encontrado"
            )
        if objetivo.role == "super_admin" and user["role"] != "super_admin":
            raise AppException(
                status_code=403,
                error_code=FORBIDDEN,
                message="Un super_admin solo lo puede modificar otro super_admin",
            )

        cambia_rol = data.role is not None and data.role != objetivo.role
        se_desactiva = data.is_active is False and objetivo.is_active
        if es_uno_mismo and (cambia_rol or se_desactiva):
            raise AppException(
                status_code=400,
                error_code=VALIDATION_ERROR,
                message="No puedes cambiar tu propio rol ni desactivarte",
            )

        deja_de_administrar = objetivo.is_active and objetivo.role in _ROLES_DE_ADMINISTRACION
        if deja_de_administrar and (
            (cambia_rol and data.role not in _ROLES_DE_ADMINISTRACION) or se_desactiva
        ):
            quedan = (
                await session.execute(
                    select(func.count())
                    .select_from(User)
                    .where(
                        User.client_id == client_id,
                        User.id != user_id,
                        User.is_active.is_(True),
                        User.role.in_(_ROLES_DE_ADMINISTRACION),
                    )
                )
            ).scalar_one()
            if quedan == 0:
                raise AppException(
                    status_code=409,
                    error_code=CONFLICT,
                    message="El tenant no puede quedarse sin un administrador activo",
                )

        if data.first_name is not None:
            objetivo.first_name = data.first_name
        if data.last_name is not None:
            objetivo.last_name = data.last_name
        if data.role is not None:
            objetivo.role = data.role
        if data.is_active is not None:
            objetivo.is_active = data.is_active
        if password_hash is not None:
            objetivo.password_hash = password_hash
        await session.flush()
        await session.refresh(objetivo)
        respuesta = UserResponse.model_validate(objetivo)

    logger.info(
        "Usuario %s modificado por %s (client_id=%s): campos=%s",
        user_id,
        user.get("user_id"),
        client_id,
        sorted(data.model_fields_set),
    )
    return respuesta
