"""Métricas Prometheus de la plataforma (Sprint 8, Dev A).

Define el catálogo de métricas y las funciones que el resto del código llama
para registrarlas. Se expone en dos sitios:

- **API:** `GET /internal/metrics` (`app/api/internal/metrics.py`).
- **Workers de Celery:** un servidor HTTP propio en `METRICS_WORKER_PORT`
  (`start_worker_metrics_server()`), porque el worker no sirve la API.

Multiproceso
------------
Ni la API ni los workers corren en un solo proceso: uvicorn arranca con
`--workers 2` (Dockerfile); cada worker de Celery usa el pool prefork con 1-4
hijos. Un contador vive en la memoria del proceso que lo incrementa, así que un
scrape a `api:8000` devolvería el valor de *uno* de los procesos, eligiendo uno
distinto cada vez — la serie resultante sube y baja sin relación con el tráfico
real.

`deploy.replicas: 2` del servicio `api` solo lo respeta Swarm: con
`docker compose up` corre **un** contenedor con 2 procesos uvicorn. Da igual
para el modo multiproceso (cada contenedor agrega su propio directorio), pero
conviene no asumir 4 procesos al leer las series.

La solución es el modo multiproceso de `prometheus_client`: con
`PROMETHEUS_MULTIPROC_DIR` definido, cada proceso escribe sus muestras en
ficheros mmap de ese directorio y el endpoint las agrega con
`MultiProcessCollector`. `docker-compose.yml` define esa variable y un `tmpfs`
por contenedor. Si la variable no está (tests, arranque local de un solo
proceso), se usa el registro por defecto y todo funciona igual.

Dos consecuencias del modo multiproceso que conviene tener presentes:

- Los `Gauge` necesitan `multiprocess_mode`; aquí no se usa ninguno. La
  profundidad de las colas de Celery **no** se mide desde la aplicación: la
  publica `redis-exporter` (`redis_key_size`), que lee la lista de Redis
  directamente. Medirla desde la app obligaría a un `LLEN` síncrono dentro del
  endpoint async de métricas.
- El directorio tiene que estar vacío al arrancar el contenedor; si no,
  arrastra las muestras de procesos muertos de la ejecución anterior. El
  `tmpfs` de compose lo garantiza.

Ninguna de las funciones `record_*` propaga excepciones: una métrica rota no
puede tumbar el procesamiento de un mensaje.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Counter, Histogram, multiprocess

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

_MULTIPROC_ENV = "PROMETHEUS_MULTIPROC_DIR"

# Buckets de latencia HTTP: el objetivo de CLAUDE.md para webhooks es <100 ms,
# así que la resolución tiene que estar abajo; los cortes altos existen para
# que el p95/p99 no se sature cuando una llamada al LLM se alarga.
_HTTP_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
# El retrieval RAG es una query a pgvector: milisegundos, no segundos.
_RAG_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
# Las tareas de Celery van de un envío de mensaje (decimas) a un backup (horas).
_TASK_BUCKETS = (0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 3600.0)
_CONFIDENCE_BUCKETS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0)


# ─── Catálogo de métricas ───────────────────────────────────────────────────

http_requests_total = Counter(
    "http_requests_total",
    "Requests HTTP atendidos por la API",
    ["method", "endpoint", "status"],
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "Latencia de los requests HTTP",
    ["method", "endpoint"],
    buckets=_HTTP_BUCKETS,
)

messages_processed_total = Counter(
    "messages_processed_total",
    "Mensajes procesados por canal y sentido",
    ["client_id", "channel", "direction"],
)

llm_tokens_consumed_total = Counter(
    "llm_tokens_consumed_total",
    "Tokens de LLM consumidos por tenant",
    ["client_id", "model", "operation", "token_type"],
)

llm_cost_usd_total = Counter(
    "llm_cost_usd_total",
    "Costo estimado en USD del consumo de LLM",
    ["client_id", "model"],
)

handoff_total = Counter(
    "handoff_total",
    "Escalaciones a un agente humano",
    ["client_id", "reason"],
)

conversations_resolved_total = Counter(
    "conversations_resolved_total",
    "Conversaciones cerradas, por quien las cerro",
    ["client_id", "resolved_by"],
)

rag_retrieval_latency_seconds = Histogram(
    "rag_retrieval_latency_seconds",
    "Latencia de la busqueda vectorial de RAG",
    ["client_id"],
    buckets=_RAG_BUCKETS,
)

rag_chunks_retrieved = Histogram(
    "rag_chunks_retrieved",
    "Cantidad de chunks por encima del umbral en cada retrieval",
    ["client_id"],
    buckets=(0, 1, 2, 3, 5, 8, 13, 21),
)

intent_routing_confidence = Histogram(
    "intent_routing_confidence",
    "Confianza del clasificador de intents",
    ["intent"],
    buckets=_CONFIDENCE_BUCKETS,
)

celery_tasks_total = Counter(
    "celery_tasks_total",
    "Tareas de Celery terminadas, por estado final",
    ["task", "queue", "state"],
)

celery_task_duration_seconds = Histogram(
    "celery_task_duration_seconds",
    "Duracion de las tareas de Celery",
    ["task", "queue"],
    buckets=_TASK_BUCKETS,
)


# ─── Registro y exposición ──────────────────────────────────────────────────


def build_registry() -> CollectorRegistry:
    """Devuelve el registro a usar para servir `/metrics`.

    En modo multiproceso construye un registro nuevo con `MultiProcessCollector`,
    que agrega las muestras de todos los procesos del contenedor. Fuera de ese
    modo devuelve el registro por defecto del proceso.

    Returns:
        Registro listo para pasar a `generate_latest()`.
    """
    directorio = os.environ.get(_MULTIPROC_ENV)
    if not directorio:
        from prometheus_client import REGISTRY

        return REGISTRY

    registro = CollectorRegistry()
    # `MultiProcessCollector` no lleva anotaciones en prometheus_client.
    multiprocess.MultiProcessCollector(registro, path=directorio)  # type: ignore[no-untyped-call]
    return registro


def start_worker_metrics_server(port: int | None = None) -> bool:
    """Levanta el servidor HTTP de métricas dentro de un worker de Celery.

    Se llama desde la señal `worker_init` (proceso maestro del worker). En modo
    multiproceso el maestro agrega lo que escriben sus hijos prefork; sin ese
    modo solo publicaría las métricas del propio maestro, que no ejecuta
    tareas — por eso se avisa en el log.

    Args:
        port: Puerto donde escuchar. Por defecto, `METRICS_WORKER_PORT`.

    Returns:
        True si el servidor quedó escuchando.
    """
    from prometheus_client import start_http_server

    from app.core.config import get_settings

    puerto = port if port is not None else get_settings().METRICS_WORKER_PORT
    if not os.environ.get(_MULTIPROC_ENV):
        logger.warning(
            "%s sin definir: el exporter del worker solo vera las metricas del "
            "proceso maestro, no las de los hijos que ejecutan tareas",
            _MULTIPROC_ENV,
        )
    try:
        start_http_server(puerto, registry=build_registry())
    except OSError as exc:
        # Puerto ocupado: pasa si dos workers comparten `network_mode`. No es
        # motivo para que el worker no arranque.
        logger.warning("No se pudo exponer metricas en el puerto %s: %s", puerto, exc)
        return False
    logger.info("Metricas del worker expuestas en :%s/metrics", puerto)
    return True


# ─── Helpers de registro ────────────────────────────────────────────────────


@contextmanager
def _sin_fallar() -> Iterator[None]:
    """Aísla el registro de una métrica de lo que lo rodea.

    Context manager y no un `_safe(fn, *args)`: con la forma de función, la
    parte que más falla —`metrica.labels(...)`, que revienta si el número de
    etiquetas no cuadra— se evalúa **antes** de entrar en la protección, y la
    excepción escapa igual. Envolviendo el bloque queda cubierta la expresión
    entera.

    Yields:
        Control al bloque que registra la métrica.
    """
    try:
        yield
    except Exception:
        logger.debug("Fallo al registrar una metrica", exc_info=True)


def record_http_request(method: str, endpoint: str, status: int, duracion: float) -> None:
    """Registra un request HTTP atendido.

    Args:
        method: Verbo HTTP.
        endpoint: Plantilla de la ruta (`/api/v1/contacts/{contact_id}`), nunca
            la URL concreta: con el id dentro, la métrica crecería sin límite.
        status: Código de estado devuelto.
        duracion: Segundos que tardó el request.
    """
    with _sin_fallar():
        http_requests_total.labels(method, endpoint, str(status)).inc()
        http_request_duration_seconds.labels(method, endpoint).observe(duracion)


def record_message(client_id: str, channel: str, direction: str) -> None:
    """Contabiliza un mensaje procesado.

    Args:
        client_id: Tenant dueño del mensaje.
        channel: Canal (`whatsapp`, `instagram`, ...).
        direction: `inbound` u `outbound` (el enum real del modelo).
    """
    with _sin_fallar():
        messages_processed_total.labels(str(client_id), channel, direction).inc()


def record_tokens(
    client_id: str,
    model: str,
    operation: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float = 0.0,
) -> None:
    """Contabiliza el consumo de una llamada al LLM.

    Args:
        client_id: Tenant que consumió.
        model: Modelo invocado.
        operation: Operación (`intent_routing`, `rag_query`, ...).
        prompt_tokens: Tokens de entrada.
        completion_tokens: Tokens generados.
        cost_usd: Costo estimado de la llamada.
    """
    tenant = str(client_id)
    with _sin_fallar():
        if prompt_tokens:
            llm_tokens_consumed_total.labels(tenant, model, operation, "prompt").inc(prompt_tokens)
        if completion_tokens:
            llm_tokens_consumed_total.labels(tenant, model, operation, "completion").inc(
                completion_tokens
            )
        if cost_usd:
            llm_cost_usd_total.labels(tenant, model).inc(cost_usd)


def record_handoff(client_id: str, reason: str) -> None:
    """Contabiliza una escalación a un humano.

    Args:
        client_id: Tenant de la conversación.
        reason: Motivo del handoff.
    """
    with _sin_fallar():
        handoff_total.labels(str(client_id), reason).inc()


def record_conversation_resolved(client_id: str, resolved_by: str, cantidad: int = 1) -> None:
    """Contabiliza una o varias conversaciones cerradas.

    Args:
        client_id: Tenant de la conversación.
        resolved_by: `agent` (la IA), `human` o `auto_close`.
        cantidad: Cuántas cerrar de golpe. El auto-cierre resuelve por UPDATE
            masivo y suma el total en una sola llamada.
    """
    if cantidad <= 0:
        return
    with _sin_fallar():
        conversations_resolved_total.labels(str(client_id), resolved_by).inc(cantidad)


def record_rag_retrieval(client_id: str, duracion: float, chunks: int) -> None:
    """Registra una búsqueda vectorial.

    Args:
        client_id: Tenant de la búsqueda.
        duracion: Segundos que tardó la query.
        chunks: Chunks devueltos por encima del umbral.
    """
    tenant = str(client_id)
    with _sin_fallar():
        rag_retrieval_latency_seconds.labels(tenant).observe(duracion)
        rag_chunks_retrieved.labels(tenant).observe(chunks)


def record_intent(intent: str, confidence: float) -> None:
    """Registra la confianza con que se clasificó un intent.

    Args:
        intent: Intent elegido.
        confidence: Confianza entre 0 y 1.
    """
    with _sin_fallar():
        intent_routing_confidence.labels(intent).observe(confidence)


class Cronometro:
    """Mide el tiempo de un bloque con el reloj monótono.

    `time.monotonic()` y no `time.time()`: un ajuste de hora del sistema (NTP)
    puede hacer negativa una duración medida con el reloj de pared, y una
    observación negativa rompe el histograma.

    Example:
        >>> with Cronometro() as c:
        ...     pass
        >>> c.elapsed >= 0
        True
    """

    def __init__(self) -> None:
        self._inicio = 0.0
        self.elapsed = 0.0

    def __enter__(self) -> Cronometro:
        """Arranca el cronómetro.

        Returns:
            El propio cronómetro.
        """
        self._inicio = time.monotonic()
        return self

    def __exit__(self, *_: object) -> None:
        """Detiene el cronómetro y guarda el tiempo transcurrido."""
        self.elapsed = time.monotonic() - self._inicio


def iter_metric_names() -> Iterator[str]:
    """Nombres con los que se exponen las métricas de este módulo.

    Son los nombres que ve Prometheus, que para un `Counter` llevan el sufijo
    `_total` (`prometheus_client` lo guarda aparte en `Metric._name`, sin él).
    Las consultas de los dashboards y las alertas se escriben contra estos.

    Yields:
        El nombre expuesto de cada métrica del catálogo.
    """
    yield from (
        "http_requests_total",
        "http_request_duration_seconds",
        "messages_processed_total",
        "llm_tokens_consumed_total",
        "llm_cost_usd_total",
        "handoff_total",
        "conversations_resolved_total",
        "rag_retrieval_latency_seconds",
        "rag_chunks_retrieved",
        "intent_routing_confidence",
        "celery_tasks_total",
        "celery_task_duration_seconds",
    )
