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

import asyncio
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

    No hace I/O de red, pero **sí de archivo**: en modo multiproceso
    `MultiProcessCollector.collect()` hace `glob` del directorio y abre, mapea
    y lee cada `.db` de cada proceso, todo síncrono. En tmpfs son microsegundos
    con pocos archivos, pero el coste crece con los procesos que hayan pasado
    por el contenedor, y corre en el hilo del event loop compitiendo con las
    requests reales (CLAUDE.md, regla 4). Por eso va a un hilo aparte.

    Returns:
        Las métricas del proceso en `text/plain; version=0.0.4`.
    """
    contenido = await asyncio.to_thread(lambda: generate_latest(build_registry()))
    return PlainTextResponse(content=contenido, media_type=CONTENT_TYPE_LATEST)
