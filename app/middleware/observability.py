"""Middleware de observabilidad: traza, contexto de log y métricas HTTP.

Es el punto donde se juntan las tres piezas del Sprint 8 para un request:

1. Lee el `trace_id` del span que creó la instrumentación de OpenTelemetry y lo
   devuelve en la cabecera `X-Trace-ID`, para que un incidente reportado por un
   cliente se pueda buscar en Jaeger por ese identificador.
2. Publica `trace_id`, `client_id` y `user_id` en el contexto de Loguru, de
   modo que toda línea emitida durante el request los lleve sin que ningún
   módulo tenga que acordarse de pasarlos.
3. Registra `http_requests_total` y `http_request_duration_seconds`.

Orden en la pila
----------------
Se registra **después** de `TenantContextMiddleware` en `create_app()`, lo que
en Starlette significa que se ejecuta **antes** que él. Eso tiene una
consecuencia deliberada: el `client_id` todavía no está en `request.state`
cuando empieza el request, así que se lee al terminar, ya resuelto por el
middleware de tenant. Ponerlo por dentro haría que los rechazos por JWT
inválido (401) no se contabilizaran en las métricas ni dejaran log con
`trace_id` — justo los casos que interesa ver.

La etiqueta `endpoint` es la **plantilla** de la ruta
(`/api/v1/contacts/{contact_id}`), no la URL concreta. Con la URL concreta,
cada id de contacto crearía una serie temporal nueva y Prometheus acabaría
guardando millones de series muertas por un endpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.metrics import Cronometro, record_http_request
from app.core.telemetry import get_trace_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response

# Rutas que no se contabilizan: el scrape de Prometheus y el healthcheck de
# Traefik dominarian por completo `http_requests_total` (cada 15s y cada 10s,
# por replica) y taparian el trafico real.
_RUTAS_IGNORADAS = frozenset({"/internal/metrics", "/internal/health"})


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Correlaciona logs con trazas y mide cada request."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Envuelve el request con contexto de log, métricas y `X-Trace-ID`.

        Args:
            request: Petición entrante.
            call_next: Siguiente eslabón de la cadena.

        Returns:
            La respuesta, con la cabecera `X-Trace-ID` añadida.
        """
        trace_id = get_trace_id()
        ignorada = request.url.path in _RUTAS_IGNORADAS

        with logger.contextualize(trace_id=trace_id, client_id="", user_id=""):
            with Cronometro() as cronometro:
                respuesta = await call_next(request)

            if trace_id:
                respuesta.headers["X-Trace-ID"] = trace_id

            if not ignorada:
                record_http_request(
                    method=request.method,
                    endpoint=_plantilla_de_ruta(request),
                    status=respuesta.status_code,
                    duracion=cronometro.elapsed,
                )

        return respuesta


def _plantilla_de_ruta(request: Request) -> str:
    """Devuelve la plantilla de la ruta que atendió el request.

    Starlette 1.0 deja en el scope `endpoint` y `path_params`, pero **no** la
    ruta en sí (`Route.matches()` no incluye `"route"` en el `child_scope`), así
    que la plantilla se resuelve por el endpoint contra el índice de rutas de la
    app. Se sigue mirando `scope["route"]` primero por si una versión futura lo
    vuelve a poblar.

    Args:
        request: Petición ya enrutada.

    Returns:
        La plantilla del endpoint (`/api/v1/contacts/{contact_id}`), o
        `"<sin_ruta>"` si ninguna coincidió — un 404, o un request cortado por
        un middleware anterior al router, como el 401 de JWT inválido.
        Devolver ahí la URL real convertiría cualquier escaneo de rutas en una
        explosión de series temporales.
    """
    ruta = request.scope.get("route")
    plantilla = getattr(ruta, "path", None)
    if plantilla:
        return str(plantilla)

    endpoint = request.scope.get("endpoint")
    if endpoint is None:
        return "<sin_ruta>"
    return _indice_de_rutas(request.app).get(endpoint, "<sin_ruta>")


def _indice_de_rutas(app: Any) -> dict[Any, str]:
    """Mapa {función del endpoint: plantilla de ruta} de una app.

    Se construye una sola vez por app y se cachea en `app.state`: recorrer las
    ~60 rutas en cada request para encontrar la plantilla sería trabajo inútil
    repetido.

    Args:
        app: Instancia de FastAPI que atendió el request.

    Returns:
        El índice de endpoints a plantillas.
    """
    indice: dict[Any, str] | None = getattr(app.state, "_indice_de_rutas", None)
    if indice is None:
        indice = {}
        for ruta in app.routes:
            endpoint = getattr(ruta, "endpoint", None)
            camino = getattr(ruta, "path", None)
            # La primera gana: si dos rutas comparten la misma funcion, la
            # metrica las agrupa en vez de inventar una plantilla.
            if endpoint is not None and camino and endpoint not in indice:
                indice[endpoint] = str(camino)
        app.state._indice_de_rutas = indice
    return indice
