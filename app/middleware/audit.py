"""Middleware que pone al usuario autenticado en el contexto de auditoria.

El rastro de auditoria lo escribe un trigger de PostgreSQL (migracion 004), no
Python. El trigger sabe a que tenant pertenece cada cambio porque lo lee de la
fila, pero no tiene forma de saber *quien* lo hizo: eso solo lo sabe la
aplicacion, y se lo pasa por `app.current_user_id`.

Este middleware es el unico punto donde ese dato entra al sistema. Corre despues
de `TenantContextMiddleware` (que valida el JWT y deja `request.state.user_id`)
y copia el usuario a un `ContextVar` que `tenant_session()` publica a PostgreSQL
en cada transaccion.

Por que un ContextVar y no un parametro
---------------------------------------
`tenant_session(client_id)` se llama desde ~30 sitios. Pasar el usuario a mano
por todos significaria que cualquiera que olvide hacerlo deja un hueco silencioso
en el rastro — un cambio atribuido al sistema cuando lo hizo una persona. Con el
ContextVar, el valor viaja solo por la tarea asincrona de la peticion, y lo que
corre fuera de una peticion (un worker de Celery) queda en None, que es
exactamente lo que el rastro debe registrar.

Un ContextVar es seguro aqui justamente porque es *por tarea*, no global: dos
peticiones concurrentes no se pisan el valor.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.database import current_user_id

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)


class AuditContextMiddleware(BaseHTTPMiddleware):
    """Copia el usuario del request al contexto que lee el trigger de auditoria."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Fija el usuario para la peticion y lo suelta al terminar.

        El `reset()` del final no es opcional: sin el, el valor se quedaria
        pegado al contexto que reutilice el servidor y un cambio hecho sin
        autenticacion podria acabar atribuido al ultimo usuario que paso por
        ahi — es decir, un rastro de auditoria que miente.

        Args:
            request: Peticion entrante.
            call_next: Siguiente eslabon de la cadena.

        Returns:
            La respuesta del resto de la aplicacion.
        """
        user_id: UUID | None = getattr(request.state, "user_id", None)
        token = current_user_id.set(user_id)
        try:
            return await call_next(request)
        finally:
            current_user_id.reset(token)
