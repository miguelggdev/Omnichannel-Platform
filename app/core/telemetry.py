"""OpenTelemetry — trazas distribuidas de la plataforma (Sprint 8, Dev A).

Instala un `TracerProvider` global y las instrumentaciones automáticas de
FastAPI, Celery, SQLAlchemy, Redis y httpx, de modo que un mensaje entrante
produzca una sola traza que atraviesa los tres procesos del sistema:

    POST /api/v1/webhooks/...  (API)
        └── task webhook_processor  (worker de `webhooks`)
            └── task ai_processor   (worker de `ai_inference`)
                └── queries SQL, llamadas a OpenAI y al proveedor de mensajería

La propagación entre procesos la hace `CeleryInstrumentor`, que serializa el
`traceparent` de W3C en los headers del mensaje de Celery.

Decisiones que se apartan del spec (§1):

- **El provider se instala siempre; el exporter solo si hay endpoint.** Sin
  `OTEL_EXPORTER_OTLP_ENDPOINT` no se registra ningún `BatchSpanProcessor`,
  así que no hay tráfico de red ni reintentos en consola, pero los spans se
  siguen creando y el `trace_id` sigue llegando a los logs
  (`app/core/logging.py`) y a la cabecera `X-Trace-ID`. Un despliegue sin
  collector conserva la correlación de logs; el spec, que monta el exporter
  incondicionalmente, llena el log de `ConnectionRefused` en cualquier
  arranque local.
- **`SQLAlchemyInstrumentor` recibe `engine.sync_engine`.** El engine del
  proyecto es un `AsyncEngine`; la instrumentación trabaja sobre el `Engine`
  síncrono que envuelve. Pasarle el `AsyncEngine`, como dice el spec, no
  registra ningún listener y no aparece una sola query en las trazas.
- **Muestreo `ParentBased(TraceIdRatio)`** en vez del `AlwaysOn` implícito:
  con ratio < 1 un request muestreado en la API conserva sus spans en el
  worker en vez de cortarse a la mitad de la traza.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.core.config import get_settings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

# URLs que no generan traza: el healthcheck de Traefik (cada 10s) y el scrape
# de Prometheus (cada 15s) multiplicarian por diez el volumen de spans sin
# aportar nada. `excluded_urls` de OTel compara por substring.
_EXCLUDED_URLS = "internal/health,metrics"

_provider: TracerProvider | None = None
_instrumented: set[str] = set()


def _resource() -> Resource:
    """Construye el `Resource` que identifica este servicio en las trazas.

    Returns:
        Resource con nombre, versión y entorno de despliegue.
    """
    settings = get_settings()
    return Resource.create(
        {
            "service.name": settings.OTEL_SERVICE_NAME,
            "service.version": settings.APP_VERSION,
            "deployment.environment": settings.APP_ENV,
        }
    )


def _ensure_provider() -> TracerProvider:
    """Instala (una sola vez) el `TracerProvider` global del proceso.

    Idempotente: si ya se instaló en este proceso devuelve el mismo provider.
    El exporter OTLP solo se engancha si hay endpoint configurado.

    Returns:
        El `TracerProvider` global del proceso.
    """
    global _provider
    if _provider is not None:
        return _provider

    settings = get_settings()
    provider = TracerProvider(
        resource=_resource(),
        sampler=ParentBased(TraceIdRatioBased(settings.OTEL_TRACES_SAMPLER_RATIO)),
    )

    endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT.strip()
    if endpoint:
        # Import local: `opentelemetry-exporter-otlp` arrastra grpc, y no hay
        # razon para pagar ese import en un despliegue sin collector.
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=endpoint,
                    insecure=not endpoint.startswith("https"),
                )
            )
        )
        logger.info("Tracing OTLP activo — endpoint=%s", endpoint)
    else:
        logger.info("Tracing local: spans en memoria, sin exportar (OTLP sin configurar)")

    trace.set_tracer_provider(provider)
    set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    )
    _provider = provider
    return provider


def _instrumentadores_opcionales() -> list[tuple[str, Any]]:
    """Instrumentaciones que dependen de paquetes opcionales.

    Se importan de forma perezosa y tolerante: si el paquete no está instalado
    (por ejemplo, en un entorno de tests mínimo), el proceso arranca igual sin
    esa instrumentación en vez de fallar.

    Returns:
        Lista de pares (nombre, clase instrumentadora) disponibles.
    """
    disponibles: list[tuple[str, Any]] = []
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor

        disponibles.append(("redis", RedisInstrumentor))
    except ImportError:  # pragma: no cover — depende del entorno
        pass
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        disponibles.append(("httpx", HTTPXClientInstrumentor))
    except ImportError:  # pragma: no cover — depende del entorno
        pass
    return disponibles


def setup_telemetry(
    app: FastAPI | None = None,
    engine: AsyncEngine | None = None,
) -> TracerProvider:
    """Inicializa el tracing y las instrumentaciones automáticas del proceso.

    Pensada para llamarse una vez por proceso: desde el `lifespan` de FastAPI
    (con `app` y `engine`) y desde la señal `worker_process_init` de Celery
    (sin `app`). Las instrumentaciones ya aplicadas no se repiten.

    Args:
        app: Instancia de FastAPI a instrumentar. `None` en los workers.
        engine: Engine async de SQLAlchemy cuyas queries se quieren trazar.

    Returns:
        El `TracerProvider` global del proceso.
    """
    provider = _ensure_provider()

    if app is not None and "fastapi" not in _instrumented:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app, tracer_provider=provider, excluded_urls=_EXCLUDED_URLS
        )
        _instrumented.add("fastapi")

    if engine is not None and "sqlalchemy" not in _instrumented:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        # `.sync_engine` (no el AsyncEngine): ver la nota del docstring del modulo.
        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, tracer_provider=provider)
        _instrumented.add("sqlalchemy")

    for nombre, instrumentador in _instrumentadores_opcionales():
        if nombre in _instrumented:
            continue
        try:
            instrumentador().instrument(tracer_provider=provider)
        except Exception as exc:  # pragma: no cover — depende del entorno
            logger.warning("No se pudo instrumentar %s: %s", nombre, exc)
        else:
            _instrumented.add(nombre)

    return provider


def setup_celery_telemetry() -> TracerProvider:
    """Instrumenta Celery en el proceso worker.

    `CeleryInstrumentor` engancha las señales de Celery, así que tiene que
    correr dentro del proceso que ejecuta las tareas (`worker_process_init`),
    no al importar el módulo.

    Returns:
        El `TracerProvider` global del proceso.
    """
    provider = _ensure_provider()
    if "celery" not in _instrumented:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor

        # El constructor de CeleryInstrumentor no lleva anotaciones.
        CeleryInstrumentor().instrument(tracer_provider=provider)  # type: ignore[no-untyped-call]
        _instrumented.add("celery")
    return provider


def get_trace_id() -> str:
    """Devuelve el trace_id actual en hexadecimal de 32 caracteres.

    Returns:
        El trace_id del span activo, o cadena vacía si no hay traza en curso
        (código llamado fuera de un request o de una tarea).
    """
    contexto = trace.get_current_span().get_span_context()
    if not contexto.is_valid:
        return ""
    return format(contexto.trace_id, "032x")


def shutdown_telemetry() -> None:
    """Vacía los spans pendientes y desmonta el provider del proceso.

    La usan el `lifespan` al cerrar y los tests; deja el módulo en estado de
    "sin inicializar" para que un `setup_telemetry()` posterior vuelva a
    montarlo desde cero.
    """
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None
    _instrumented.clear()
