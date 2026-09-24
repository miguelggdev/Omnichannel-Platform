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

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import bindparam, case, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import NOT_FOUND, VALIDATION_ERROR, AppException
from app.models.contact import Contact
from app.models.contact_identifier import ContactIdentifier
from app.models.contact_tag import ContactTag
from app.models.conversation import Conversation
from app.models.internal_note import InternalNote
from app.models.message import Message
from app.models.satisfaction_survey import SatisfactionSurvey
from app.models.tag import Tag
from app.schemas.csat import CsatSummaryResponse

logger = logging.getLogger(__name__)

router = APIRouter()

_GDPR_ROLES = ("super_admin", "admin")
_CSAT_ROLES = ("super_admin", "admin", "supervisor")

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

    `payload -> 'data' -> 'content' IS NOT NULL` ademas de `? 'content'`: el
    operador `?` de JSONB solo verifica que la clave exista, no que su valor
    no sea `null` — un mensaje de solo medios sin caption serializa
    `data.content` como `null`, y sin este chequeo la redaccion lo
    sobreescribiria con el aviso de "eliminado" aunque nunca hubo texto.

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
              AND payload -> 'data' -> 'content' IS NOT NULL
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
    }


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
