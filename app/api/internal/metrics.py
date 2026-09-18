"""Endpoint de métricas para Prometheus — Sin autenticación (Sprint 8).

Cuelga de `/internal/metrics`, no de `/metrics` como dice el spec (§3.3). El
prefijo `/internal` es el que ya usa el healthcheck y deja el corte de acceso
en un solo sitio: Prometheus scrapea `api:8000` por la red `backend` de
docker-compose, y lo que quede bajo `/internal` no necesita salir por Traefik.
Publicar `/metrics` en la raíz lo dejaba colgando del mismo router público que
la API del cliente, exponiendo nombres de tenant (las métricas llevan
`client_id`) a cualquiera que llegase al host.

Tampoco se monta como sub-app ASGI (`app.mount("/metrics", make_asgi_app())`):
una sub-app montada se salta los exception handlers y el middleware de la app
principal, y además `make_asgi_app()` fija el registro por defecto, que en modo
multiproceso es justo el que no hay que servir (ver `app/core/metrics.py`).
"""

import logging

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.metrics import build_registry

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics() -> PlainTextResponse:
    """Expone las métricas en el formato de texto de Prometheus.

    `generate_latest()` solo serializa muestras que ya están en memoria (o en
    los ficheros mmap del modo multiproceso): no hace I/O de red, así que no
    rompe la regla de "nada síncrono en un endpoint async".

    Returns:
        Las métricas del proceso en `text/plain; version=0.0.4`.
    """
    return PlainTextResponse(
        content=generate_latest(build_registry()),
        media_type=CONTENT_TYPE_LATEST,
    )
