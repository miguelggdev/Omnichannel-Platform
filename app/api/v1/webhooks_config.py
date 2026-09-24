"""CRUD de configuracion de webhooks salientes (Sprint 11, Dev B).

GET    /api/v1/outgoing-webhooks              lista los del tenant
POST   /api/v1/outgoing-webhooks              crea uno (el secreto se devuelve una sola vez)
PUT    /api/v1/outgoing-webhooks/{id}         edicion parcial; reactivar resetea los fallos
DELETE /api/v1/outgoing-webhooks/{id}         borra (y sus logs, por ON DELETE CASCADE)
GET    /api/v1/outgoing-webhooks/{id}/logs    historial de entregas
POST   /api/v1/outgoing-webhooks/{id}/test    manda un evento de prueba

No "/api/v1/webhooks/outgoing" (el path del spec): `TenantContextMiddleware`
salta la autenticacion JWT en cualquier ruta bajo "/api/v1/webhooks/" porque ahi
vive el receptor de webhooks entrantes, que se autentica por firma HMAC, no por
JWT (`WEBHOOK_PATHS_PREFIX`). Un CRUD en esa ruta habria quedado sin auth.

El engine que firma y envia (`WebhookDispatcher`, `app/tasks/outgoing_webhooks.py`)
es de Dev A (ADR-065); este modulo solo administra la configuracion y consulta el
historial que ese engine deja en `outgoing_webhook_logs`.
"""

import logging
import secrets
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import NOT_FOUND, VALIDATION_ERROR, AppException
from app.models.outgoing_webhook_log import OutgoingWebhookLog
from app.models.tenant_webhook import TenantWebhook
from app.schemas.webhook_config import (
    WebhookCreate,
    WebhookCreateResponse,
    WebhookLogResponse,
    WebhookResponse,
    WebhookTestResponse,
    WebhookUpdate,
    eventos_no_soportados,
)
from app.services.webhook_dispatcher import WebhookDispatcher

# Privada y reusada tal cual: ya existe con exactamente estos cuatro campos
# para exactamente este proposito (copiar el webhook fuera de la sesion antes
# de un POST que puede tardar) — duplicarla con un SimpleNamespace aparte solo
# daria dos implementaciones del mismo Protocol sin chequeo estatico.
from app.tasks.outgoing_webhooks import _DatosDelWebhook

logger = logging.getLogger(__name__)

router = APIRouter()

_ROLES = ("super_admin", "admin")

# Longitud del secreto autogenerado cuando el tenant no manda uno propio.
# `token_hex(32)` da 64 caracteres hexadecimales: mismo orden de magnitud que
# el secreto de webhooks de proveedores como Stripe o GitHub.
AUTO_SECRET_BYTES = 32


async def _get_or_404(session: AsyncSession, webhook_id: UUID, client_id: UUID) -> TenantWebhook:
    """Carga un webhook del tenant activo o levanta 404.

    Args:
        session: Sesion con contexto de tenant.
        webhook_id: Webhook buscado.
        client_id: Tenant propietario.

    Returns:
        El webhook.

    Raises:
        AppException: 404 si no existe para este tenant.
    """
    stmt = select(TenantWebhook).where(
        TenantWebhook.id == webhook_id,
        TenantWebhook.client_id == client_id,
    )
    webhook = (await session.execute(stmt)).scalar_one_or_none()
    if webhook is None:
        raise AppException(status_code=404, error_code=NOT_FOUND, message="Webhook no encontrado")
    return webhook


def _validar_eventos(events: list[str]) -> None:
    """Levanta 400 si `events` incluye algo fuera de `SUPPORTED_EVENTS`.

    Args:
        events: Eventos que el tenant quiere suscribir.

    Raises:
        AppException: 400 con el detalle de los eventos no reconocidos.
    """
    invalidos = eventos_no_soportados(events)
    if invalidos:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message=f"Eventos no soportados: {', '.join(sorted(invalidos))}",
        )


@router.get("", response_model=list[WebhookResponse])
async def list_outgoing_webhooks(
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> list[WebhookResponse]:
    """Lista los webhooks salientes configurados por el tenant.

    Args:
        user: Usuario autenticado.

    Returns:
        Los webhooks del tenant, del mas nuevo al mas viejo.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        stmt = (
            select(TenantWebhook)
            .where(TenantWebhook.client_id == client_id)
            .order_by(TenantWebhook.created_at.desc())
        )
        webhooks = (await session.execute(stmt)).scalars().all()
        return [WebhookResponse.model_validate(w) for w in webhooks]


@router.post("", status_code=201, response_model=WebhookCreateResponse)
async def create_outgoing_webhook(
    data: WebhookCreate,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> WebhookCreateResponse:
    """Configura un webhook saliente nuevo.

    Args:
        data: URL, eventos suscritos y, opcionalmente, secreto y cabeceras.
        user: Usuario autenticado.

    Returns:
        El webhook creado, con el secreto en claro (unica vez que se devuelve).

    Raises:
        AppException: 400 si algun evento no esta soportado.
    """
    client_id: UUID = user["client_id"]
    _validar_eventos(data.events)
    secret = data.secret or secrets.token_hex(AUTO_SECRET_BYTES)

    async with tenant_session(client_id) as session:
        # `is_active`/`consecutive_failures` van explicitos y no solo confiados
        # al `server_default`: la respuesta se arma con el objeto tal como queda
        # tras el `refresh()`, y un `refresh()` que no vuelva a leer esas dos
        # columnas (algo que no depende de este endpoint) las dejaria en blanco
        # en vez del valor real con el que se creo la fila.
        webhook = TenantWebhook(
            client_id=client_id,
            url=data.url,
            secret=secret,
            events=data.events,
            description=data.description,
            headers=data.headers or {},
            is_active=True,
            consecutive_failures=0,
        )
        session.add(webhook)
        await session.flush()
        await session.refresh(webhook)

    logger.info("Webhook saliente %s creado (tenant %s)", webhook.id, client_id)
    # `webhook.secret` ya paso por `EncryptedString` de vuelta (el refresh lo
    # descifra); se sustituye por el valor en claro que se acaba de generar
    # para no depender de que el roundtrip de cifrado devuelva exactamente lo
    # mismo que se escribio.
    return WebhookCreateResponse(
        **WebhookResponse.model_validate(webhook).model_dump(),
        secret=secret,
    )


@router.put("/{webhook_id}", response_model=WebhookResponse)
async def update_outgoing_webhook(
    webhook_id: UUID,
    data: WebhookUpdate,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> WebhookResponse:
    """Actualiza los campos enviados de un webhook saliente.

    Reactivarlo (`is_active=true`) resetea `consecutive_failures` y
    `disabled_reason`: es el unico modo de recuperar un webhook que el propio
    engine apago tras `MAX_CONSECUTIVE_FAILURES` fallos seguidos.

    Args:
        webhook_id: Webhook a actualizar.
        data: Campos a modificar.
        user: Usuario autenticado.

    Returns:
        El webhook ya actualizado.

    Raises:
        AppException: 404 si no existe, 400 si algun evento nuevo no esta soportado.
    """
    client_id: UUID = user["client_id"]
    if data.events is not None:
        _validar_eventos(data.events)

    cambios = data.model_dump(exclude_unset=True)
    # `url`/`events`/`headers` son NOT NULL en la base: un cliente que mande
    # `null` explicito para alguno pasa la validacion de Pydantic (el schema
    # los tipa Optional para que "no enviado" siga significando "no tocar"),
    # y sin este chequeo el `setattr` de abajo dejaria el atributo en `None` y
    # el `flush()` reventaria con un `IntegrityError` sin atrapar — un 500
    # generico en vez del 400 que un valor invalido deberia dar.
    nulos = [
        campo
        for campo in ("url", "events", "headers")
        if campo in cambios and cambios[campo] is None
    ]
    if nulos:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message=f"No se puede poner en null: {', '.join(nulos)}",
        )

    async with tenant_session(client_id) as session:
        webhook = await _get_or_404(session, webhook_id, client_id)

        for campo, valor in cambios.items():
            setattr(webhook, campo, valor)

        if data.is_active:
            webhook.consecutive_failures = 0
            webhook.disabled_reason = None

        await session.flush()
        await session.refresh(webhook)
        return WebhookResponse.model_validate(webhook)


@router.delete("/{webhook_id}", status_code=200)
async def delete_outgoing_webhook(
    webhook_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> dict[str, str]:
    """Borra un webhook saliente y su historial de entregas.

    Args:
        webhook_id: Webhook a borrar.
        user: Usuario autenticado.

    Returns:
        Confirmacion del borrado.

    Raises:
        AppException: 404 si no existe para este tenant.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        webhook = await _get_or_404(session, webhook_id, client_id)
        await session.delete(webhook)
        await session.flush()

    logger.info("Webhook saliente %s borrado (tenant %s)", webhook_id, client_id)
    return {"message": "Webhook eliminado"}


@router.get("/{webhook_id}/logs", response_model=list[WebhookLogResponse])
async def get_webhook_logs(
    webhook_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None, description="Filtra por 'success' o 'failed'"),
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> list[WebhookLogResponse]:
    """Historial de entregas de un webhook, la mas reciente primero.

    Args:
        webhook_id: Webhook a consultar.
        limit: Maximo de filas a devolver.
        status: Limita a `success` o `failed`.
        user: Usuario autenticado.

    Returns:
        Los intentos de entrega registrados.

    Raises:
        AppException: 404 si el webhook no existe para este tenant.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        await _get_or_404(session, webhook_id, client_id)

        stmt = select(OutgoingWebhookLog).where(
            OutgoingWebhookLog.client_id == client_id,
            OutgoingWebhookLog.webhook_id == webhook_id,
        )
        if status:
            stmt = stmt.where(OutgoingWebhookLog.status == status)
        stmt = stmt.order_by(OutgoingWebhookLog.created_at.desc()).limit(limit)

        logs = (await session.execute(stmt)).scalars().all()
        return [WebhookLogResponse.model_validate(log) for log in logs]


@router.post("/{webhook_id}/test", response_model=WebhookTestResponse)
async def send_test_webhook(
    webhook_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> WebhookTestResponse:
    """Manda un evento de prueba al webhook, sin pasar por Celery ni dejar log.

    Sincrono a proposito: quien configura el webhook quiere saber en el acto si
    la URL responde, no esperar a que la cola `notifications` lo procese. Por lo
    mismo no se escribe en `outgoing_webhook_logs` ni cuenta para
    `consecutive_failures` — es una prueba de configuracion, no una entrega real.

    Args:
        webhook_id: Webhook a probar.
        user: Usuario autenticado.

    Returns:
        Si el envio de prueba tuvo exito, con el detalle del intento.

    Raises:
        AppException: 404 si el webhook no existe para este tenant.
    """
    client_id: UUID = user["client_id"]

    async with tenant_session(client_id) as session:
        webhook = await _get_or_404(session, webhook_id, client_id)
        datos = _DatosDelWebhook(
            id=webhook.id, url=webhook.url, secret=webhook.secret, headers=webhook.headers
        )

    resultado = await WebhookDispatcher().send_webhook(
        datos,
        {
            "event": "webhook.test",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "webhook_delivery_id": str(uuid4()),
            "data": {"message": "Este es un evento de prueba."},
        },
    )
    return WebhookTestResponse(
        success=bool(resultado["success"]),
        status_code=resultado.get("status_code"),
        duration_ms=resultado.get("duration_ms"),
        error=resultado.get("error"),
    )
