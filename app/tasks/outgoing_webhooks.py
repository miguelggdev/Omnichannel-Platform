"""Tasks de Celery del engine de webhooks salientes (Sprint 11).

Nombres de las tareas
----------------------
`app.tasks.notification_*`, no `dispatch_outgoing_webhooks`/`send_webhook_with_retry`
como en el spec: el routing automatico de `celery_config.py` manda a la cola
`notifications` lo que empieza por `app.tasks.notification_*`, y todo lo demas
cae en la cola por defecto (`webhooks`), que es justamente la de los mensajes
entrantes — un webhook saliente lento atascaria la recepcion. Mismo motivo por
el que la clonacion de tenants se llama `app.tasks.bulk_*` (ADR-064).

Por que son tareas sincronas
-----------------------------
El spec las declara `async def`: Celery no sabe esperar una corrutina, asi que
esas tareas devolverian el objeto coroutine sin ejecutar nada. Aca, como el
resto de `app/tasks/*`, la tarea es sincrona y la parte async corre dentro de
`run_isolated()` (BUG-006/BUG-021: engine y cliente Redis atados al event loop
que los creo).

Reintentos y desactivacion automatica
--------------------------------------
Cada evento se manda a cada webhook suscrito con hasta `MAX_ATTEMPTS` intentos
(1 inmediato + `RETRY_DELAYS`), con backoff 5s/30s/300s. Cada intento deja su
propia fila en `outgoing_webhook_logs` (comparten
`payload["webhook_delivery_id"]`, se distinguen por `attempt`).

`consecutive_failures` cuenta **entregas** fallidas, no intentos: se incrementa
cuando se agotan los reintentos de un evento, no en cada reintento. Asi el
umbral de `MAX_CONSECUTIVE_FAILURES` significa lo que dice el criterio 3 del
spec ("10 envios fallidos"), y no 10 intentos, que con el backoff serian dos
eventos y medio.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from celery import shared_task
from sqlalchemy import case, literal, select
from sqlalchemy import update as sa_update

from app.core.database import run_isolated, tenant_session
from app.models.outgoing_webhook_log import OutgoingWebhookLog
from app.models.tenant_webhook import (
    AUTO_DISABLED_REASON,
    MAX_CONSECUTIVE_FAILURES,
    TenantWebhook,
)
from app.services.webhook_dispatcher import WebhookDispatcher

logger = logging.getLogger(__name__)

# Espera antes de cada reintento, en segundos.
RETRY_DELAYS = (5, 30, 300)

# Intentos totales por entrega: el inmediato mas un reintento por retraso.
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1


async def _despachar(event: str, client_id: str, data: dict[str, Any]) -> int:
    """Encola un envio por cada webhook activo suscrito al evento.

    Args:
        event: Nombre del evento.
        client_id: Tenant dueno del hecho, serializado.
        data: Cuerpo del evento.

    Returns:
        Cuantos envios se encolaron.
    """
    tenant_id = UUID(client_id)

    async with tenant_session(tenant_id) as session:
        webhooks = list(
            (
                await session.execute(
                    select(TenantWebhook).where(
                        TenantWebhook.client_id == tenant_id,
                        TenantWebhook.is_active.is_(True),
                        # `literal(event)` y no `event` a secas: el SQL es el
                        # mismo (`:param = ANY (tenant_webhooks.events)`), pero
                        # asi el comparador recibe la expresion que su firma
                        # declara y mypy no tiene que mirar para otro lado.
                        TenantWebhook.events.any(literal(event)),
                    )
                )
            ).scalars()
        )
        # Solo el id: fuera de la sesion, tocar cualquier otro atributo
        # dispararia un refresh perezoso contra una sesion ya cerrada.
        webhook_ids = [str(webhook.id) for webhook in webhooks]

    for webhook_id in webhook_ids:
        payload = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "webhook_delivery_id": str(uuid4()),
            "data": data,
        }
        send_outgoing_webhook.apply_async(
            kwargs={
                "webhook_id": webhook_id,
                "client_id": client_id,
                "payload": payload,
                "attempt": 1,
            }
        )

    logger.info(
        "Evento %s del tenant %s: %d webhook(s) suscrito(s)", event, client_id, len(webhook_ids)
    )
    return len(webhook_ids)


@dataclass(frozen=True)
class _DatosDelWebhook:
    """Lo que hace falta para firmar y enviar, ya fuera de la sesion.

    `send_webhook()` solo lee `id`, `url`, `secret` y `headers`; copiarlos a un
    objeto suelto permite cerrar la transaccion antes del POST sin que ningun
    atributo dispare una carga perezosa contra una sesion ya cerrada.
    """

    id: UUID
    url: str
    secret: str
    headers: dict[str, Any]


async def _cargar_webhook(tenant_id: UUID, webhook_uuid: UUID) -> _DatosDelWebhook | None:
    """Lee el webhook activo del tenant en una transaccion corta.

    Args:
        tenant_id: Tenant dueno del webhook.
        webhook_uuid: Webhook a leer.

    Returns:
        Sus datos de envio, o None si ya no existe o esta apagado.
    """
    async with tenant_session(tenant_id) as session:
        webhook = (
            await session.execute(
                select(TenantWebhook).where(
                    TenantWebhook.id == webhook_uuid, TenantWebhook.client_id == tenant_id
                )
            )
        ).scalar_one_or_none()

        if webhook is None or not webhook.is_active:
            return None

        return _DatosDelWebhook(
            id=webhook.id,
            url=webhook.url,
            secret=webhook.secret,
            headers=dict(webhook.headers or {}),
        )


async def _registrar_intento(
    tenant_id: UUID,
    webhook_uuid: UUID,
    payload: dict[str, Any],
    attempt: int,
    resultado: dict[str, Any],
    ultimo_intento: bool,
) -> dict[str, Any]:
    """Deja el intento en la base y decide que sigue.

    El UPDATE del contador va como una sola sentencia atomica
    (`consecutive_failures + 1` calculado por PostgreSQL, con la
    desactivacion resuelta en el mismo `CASE`), no leyendo y reescribiendo
    desde Python: varias entregas del mismo webhook corren en paralelo en el
    worker, y un read-modify-write perderia incrementos — la misma correccion
    que BUG-022 en `TokenBudgetGuard.record_usage`.

    Args:
        tenant_id: Tenant dueno del webhook.
        webhook_uuid: Webhook al que se intento enviar.
        payload: Evento enviado.
        attempt: Numero de intento.
        resultado: Lo que devolvio `send_webhook()`.
        ultimo_intento: Si ya no quedan reintentos para esta entrega.

    Returns:
        `{"status": "success" | "failed" | "disabled", ...}`.
    """
    ahora = datetime.now(timezone.utc)
    exito = bool(resultado["success"])

    async with tenant_session(tenant_id) as session:
        session.add(
            OutgoingWebhookLog(
                client_id=tenant_id,
                webhook_id=webhook_uuid,
                event=str(payload.get("event", "unknown")),
                payload=payload,
                status="success" if exito else "failed",
                response_code=resultado.get("status_code"),
                response_body=resultado.get("response_body"),
                duration_ms=resultado.get("duration_ms"),
                attempt=attempt,
                error=resultado.get("error"),
            )
        )

        base = (
            sa_update(TenantWebhook)
            .where(TenantWebhook.id == webhook_uuid, TenantWebhook.client_id == tenant_id)
            .execution_options(synchronize_session=False)
        )

        if exito:
            await session.execute(
                base.values(last_triggered_at=ahora, last_success_at=ahora, consecutive_failures=0)
            )
            return {"status": "success", "status_code": resultado.get("status_code")}

        if not ultimo_intento:
            # Quedan reintentos: la entrega todavia no fracaso, solo el intento.
            await session.execute(base.values(last_triggered_at=ahora, last_failure_at=ahora))
            return {"status": "failed", "retry_in": RETRY_DELAYS[attempt - 1]}

        fallos = TenantWebhook.consecutive_failures + 1
        se_apaga = fallos >= MAX_CONSECUTIVE_FAILURES
        nuevos_fallos = (
            await session.execute(
                base.values(
                    last_triggered_at=ahora,
                    last_failure_at=ahora,
                    consecutive_failures=fallos,
                    is_active=case((se_apaga, False), else_=TenantWebhook.is_active),
                    disabled_reason=case(
                        (se_apaga, AUTO_DISABLED_REASON), else_=TenantWebhook.disabled_reason
                    ),
                ).returning(TenantWebhook.consecutive_failures, TenantWebhook.is_active)
            )
        ).first()

    if nuevos_fallos is not None and not nuevos_fallos.is_active:
        logger.warning(
            "Webhook %s desactivado tras %d entregas fallidas seguidas",
            webhook_uuid,
            nuevos_fallos.consecutive_failures,
        )
        return {"status": "disabled"}

    return {"status": "failed", "retry_in": None}


async def _enviar(
    webhook_id: str, client_id: str, payload: dict[str, Any], attempt: int
) -> dict[str, Any]:
    """Hace un intento de envio y deja el resultado en la base.

    El POST se hace **sin transaccion abierta**: con Supavisor en modo
    transaccion, mantenerla abierta durante la llamada retiene una conexion
    del pooler hasta 10 segundos (el timeout del envio) por cada webhook, y el
    worker de `notifications` manda muchos en paralelo. Son dos transacciones
    cortas, una antes y otra despues, como ya hace la subida de documentos.

    Args:
        webhook_id: Webhook destino, serializado.
        client_id: Tenant dueno del webhook, serializado.
        payload: Evento completo a enviar.
        attempt: Numero de intento, empezando en 1.

    Returns:
        `{"status": "success" | "failed" | "skipped" | "disabled", ...}`.
    """
    tenant_id = UUID(client_id)
    webhook_uuid = UUID(webhook_id)

    datos = await _cargar_webhook(tenant_id, webhook_uuid)
    if datos is None:
        # Borrado o apagado entre el encolado y el envio: no es un fallo.
        logger.info("Webhook %s ya no esta activo; no se envia", webhook_id)
        return {"status": "skipped"}

    resultado = await WebhookDispatcher().send_webhook(datos, payload)

    return await _registrar_intento(
        tenant_id,
        webhook_uuid,
        payload,
        attempt,
        resultado,
        ultimo_intento=attempt >= MAX_ATTEMPTS,
    )


@shared_task(
    name="app.tasks.notification_dispatch_outgoing_webhooks",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    acks_late=True,
    queue="notifications",
    time_limit=120,
    soft_time_limit=100,
)
def dispatch_outgoing_webhooks(
    self: Any, event: str, client_id: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Busca los webhooks suscritos a un evento y encola un envio por cada uno.

    Args:
        self: Instancia de la tarea (bind=True).
        event: Nombre del evento.
        client_id: Tenant dueno del hecho, serializado.
        data: Cuerpo del evento.

    Returns:
        `{"webhooks": <cuantos envios se encolaron>}`.

    Raises:
        Retry: Ante un fallo transitorio (la base sin responder, por ejemplo);
            hasta `max_retries`, que es lo unico que puede salvar al evento de
            perderse. Aca reintentar es seguro porque todavia no se envio nada
            a nadie — en `send_outgoing_webhook` no lo es, ver su docstring.
    """
    try:
        encolados = run_isolated(_despachar(event, client_id, data))
    except Exception as exc:
        logger.exception("No se pudo despachar el evento %s del tenant %s", event, client_id)
        raise self.retry(exc=exc) from exc
    return {"webhooks": encolados}


@shared_task(
    name="app.tasks.notification_send_outgoing_webhook",
    bind=True,
    acks_late=True,
    queue="notifications",
    time_limit=120,
    soft_time_limit=100,
)
def send_outgoing_webhook(
    self: Any,
    webhook_id: str,
    client_id: str,
    payload: dict[str, Any],
    attempt: int = 1,
) -> dict[str, Any]:
    """Envia un evento a un webhook y programa el reintento si hace falta.

    El reintento se programa como una tarea nueva con `countdown`, no con
    `self.retry()`: asi el intento que fallo queda cerrado (y su fila de log
    escrita) en vez de reencolarse entero, y el numero de intento viaja
    explicito en los argumentos.

    Un error inesperado (la base sin responder al registrar el intento) **no**
    se reintenta: el POST puede haber salido ya, y reintentar la tarea entera
    lo repetiria. Queda el `webhook_delivery_id` en el payload para que el
    receptor deduplique si el broker llegara a reentregar la tarea.

    Args:
        self: Instancia de la tarea (bind=True).
        webhook_id: Webhook destino, serializado.
        client_id: Tenant dueno del webhook, serializado.
        payload: Evento completo a enviar.
        attempt: Numero de intento, empezando en 1.

    Returns:
        Resultado del intento.
    """
    resultado = run_isolated(_enviar(webhook_id, client_id, payload, attempt))

    if resultado["status"] == "failed" and resultado.get("retry_in") is not None:
        logger.info(
            "Webhook %s fallo (intento %d/%d); reintento en %ds",
            webhook_id,
            attempt,
            MAX_ATTEMPTS,
            resultado["retry_in"],
        )
        send_outgoing_webhook.apply_async(
            kwargs={
                "webhook_id": webhook_id,
                "client_id": client_id,
                "payload": payload,
                "attempt": attempt + 1,
            },
            countdown=resultado["retry_in"],
        )

    return resultado
