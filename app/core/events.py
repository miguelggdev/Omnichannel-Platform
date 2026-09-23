"""Sistema de eventos internos que alimenta los webhooks salientes (Sprint 11).

Que hace y que no
------------------
`EventEmitter.emit()` **nunca** hace el envio: encola
`app.tasks.notification_dispatch_outgoing_webhooks` y devuelve. El flujo
principal (webhook entrante, endpoint del CRM, nodo del grafo) no espera a
ningun sistema externo ni se entera de si el envio salio bien — criterio 11
del spec.

Por el mismo motivo `emit()` **nunca propaga una excepcion**: si Redis esta
caido, un evento perdido no puede tumbar la respuesta a un contacto. Se
registra y se sigue.

Donde se emite
---------------
Siempre **despues** del commit de la transaccion que produjo el hecho, igual
que las metricas de `record_message()`: un evento emitido dentro de la
transaccion se dispararia tambien cuando esa transaccion termina en rollback,
y el receptor externo se quedaria con un hecho que nunca ocurrio.

`on()` esta para engancharse en el proceso (lo usa el CSAT de Dev B sobre
`conversation.resolved`); los handlers corren dentro del flujo que emite, asi
que tienen que ser baratos y no bloquear.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar
from uuid import UUID

logger = logging.getLogger(__name__)

Handler = Callable[[str, str, dict[str, Any]], Awaitable[None]]

# Cuanto se espera como maximo a que el broker acepte el encolado. Sin este
# corte, un Redis inalcanzable bloquea a quien emite durante todo el ciclo de
# reintentos de conexion de kombu: el flujo principal, que no depende del
# evento para nada, se quedaria esperando a la cola.
ENQUEUE_TIMEOUT_SECONDS = 5.0

# Eventos que un tenant puede suscribir. Vive aca, y no en el CRUD, porque lo
# necesitan los tres lados: quien emite, quien despacha y quien valida la
# suscripcion (`app/api/v1/webhooks_config.py`, Dev B).
EVENT_MESSAGE_RECEIVED = "message.received"
EVENT_MESSAGE_SENT = "message.sent"
EVENT_CONVERSATION_CREATED = "conversation.created"
EVENT_CONVERSATION_RESOLVED = "conversation.resolved"
EVENT_CONTACT_CREATED = "contact.created"
EVENT_CONTACT_UPDATED = "contact.updated"
EVENT_APPOINTMENT_CREATED = "appointment.created"

SUPPORTED_EVENTS: tuple[str, ...] = (
    EVENT_MESSAGE_RECEIVED,
    EVENT_MESSAGE_SENT,
    EVENT_CONVERSATION_CREATED,
    EVENT_CONVERSATION_RESOLVED,
    EVENT_CONTACT_CREATED,
    EVENT_CONTACT_UPDATED,
    EVENT_APPOINTMENT_CREATED,
)


class EventEmitter:
    """Punto unico de emision de eventos de dominio."""

    _handlers: ClassVar[dict[str, list[Handler]]] = {}

    @classmethod
    def on(cls, event: str, handler: Handler) -> None:
        """Registra un handler en proceso para un evento.

        Args:
            event: Nombre del evento (uno de `SUPPORTED_EVENTS`).
            handler: Corrutina `(event, client_id, data) -> None`.
        """
        cls._handlers.setdefault(event, []).append(handler)

    @classmethod
    def clear_handlers(cls) -> None:
        """Vacia el registro de handlers. Para los tests."""
        cls._handlers.clear()

    @classmethod
    async def emit(cls, event: str, client_id: UUID | str, data: dict[str, Any]) -> None:
        """Emite un evento: encola el despacho a los webhooks del tenant.

        No lanza nunca: cualquier fallo se registra y se descarta. Llamar
        siempre despues del commit del hecho que se anuncia.

        Args:
            event: Nombre del evento (uno de `SUPPORTED_EVENTS`).
            client_id: Tenant dueno del hecho.
            data: Cuerpo del evento, ya serializable a JSON.
        """
        if event not in SUPPORTED_EVENTS:
            logger.warning("Evento desconocido, no se emite: %s", event)
            return

        client_id_str = str(client_id)
        logger.debug("Evento %s emitido por el tenant %s", event, client_id_str)

        for handler in cls._handlers.get(event, []):
            try:
                await handler(event, client_id_str, data)
            except Exception:
                logger.exception("Handler de %s fallo; se ignora", event)

        try:
            # Import perezoso: `app.tasks.*` importa de `app.core.*`, y hacerlo
            # al tope del modulo cerraria el ciclo.
            from app.tasks.outgoing_webhooks import dispatch_outgoing_webhooks

            # `.delay()`/`.apply_async()` hablan con Redis de forma sincrona: en
            # un endpoint async bloquearian el event loop (regla 4 de
            # CLAUDE.md), igual que la ingesta de documentos en `documents.py`.
            # `retry=False`: reintentar la conexion es trabajo del flujo
            # principal, y este no es el lugar para pagarlo.
            # `shared_task` devuelve un proxy que se resuelve contra la app de
            # Celery *del hilo que lo usa*: resolverlo dentro del executor da la
            # app por defecto (broker amqp://localhost), no la configurada. Por
            # eso el metodo se liga aca, en el hilo que ya tiene la app buena, y
            # al hilo solo viaja la llamada.
            encolar = dispatch_outgoing_webhooks.apply_async

            def _encolar() -> None:
                encolar(args=(event, client_id_str, data), retry=False)

            # `run_in_executor` del propio loop, no el threadpool de starlette:
            # esto tambien corre dentro de los workers de Celery, donde no hay
            # ningun contexto de anyio que lo respalde.
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, _encolar),
                timeout=ENQUEUE_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception(
                "No se pudo encolar el despacho del evento %s del tenant %s", event, client_id_str
            )
