"""Tests de la observabilidad: trazas, logging estructurado y métricas.

Cubre las tres piezas de `app/core/{telemetry,logging,metrics}.py` y el
middleware que las une, sin necesidad de base de datos ni de un collector OTLP.
"""

from __future__ import annotations

import json
import logging
import weakref
from typing import Any, ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger
from opentelemetry import trace
from prometheus_client import CollectorRegistry, Counter, generate_latest

from app.core import metrics as metrics_module
from app.core import telemetry as telemetry_module
from app.core.logging import InterceptHandler, bind_task_context, setup_logging
from app.core.metrics import (
    Cronometro,
    build_registry,
    iter_metric_names,
    record_conversation_resolved,
    record_http_request,
    record_intent,
    record_rag_retrieval,
    record_tokens,
)
from app.core.telemetry import get_trace_id, setup_telemetry, shutdown_telemetry
from app.middleware.observability import ObservabilityMiddleware, _plantilla_de_ruta


@pytest.fixture(autouse=True)
def _telemetria_limpia() -> Any:
    """Deja el módulo de telemetría sin provider entre tests.

    Sin esto, el primer test que instale el provider global condiciona a todos
    los siguientes: `setup_telemetry()` es idempotente a propósito.
    """
    shutdown_telemetry()
    yield
    shutdown_telemetry()


class TestTelemetria:
    """`setup_telemetry()` y `get_trace_id()`."""

    def test_sin_endpoint_no_monta_exporter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin OTLP configurado no hay procesador de spans que exporte.

        Es la diferencia deliberada con el spec: montar el exporter siempre
        llena el log de `ConnectionRefused` en cualquier arranque sin collector.
        """
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "OTEL_EXPORTER_OTLP_ENDPOINT", "")
        provider = setup_telemetry()
        # `_active_span_processor` agrupa los procesadores registrados.
        assert not provider._active_span_processor._span_processors

    def test_es_idempotente(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Llamarla dos veces no instala dos providers ni duplica spans."""
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "OTEL_EXPORTER_OTLP_ENDPOINT", "")
        primero = setup_telemetry()
        segundo = setup_telemetry()
        assert primero is segundo

    def test_trace_id_vacio_fuera_de_una_traza(self) -> None:
        """Sin span activo no se inventa un identificador."""
        assert get_trace_id() == ""

    def test_trace_id_dentro_de_un_span(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dentro de un span devuelve 32 caracteres hex, el formato de W3C."""
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "OTEL_EXPORTER_OTLP_ENDPOINT", "")
        setup_telemetry()
        with trace.get_tracer(__name__).start_as_current_span("prueba"):
            trace_id = get_trace_id()
        assert len(trace_id) == 32
        assert int(trace_id, 16) > 0

    def test_el_muestreo_respeta_al_padre(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Con ratio 0 se sigue respetando un padre ya muestreado.

        Si el sampler fuera un `TraceIdRatioBased` pelado, el worker descartaría
        los spans de un request que la API sí decidió muestrear y la traza
        quedaría cortada por la mitad.
        """
        from opentelemetry.sdk.trace.sampling import ParentBased

        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "OTEL_EXPORTER_OTLP_ENDPOINT", "")
        monkeypatch.setattr(get_settings(), "OTEL_TRACES_SAMPLER_RATIO", 0.0)
        provider = setup_telemetry()
        assert isinstance(provider.sampler, ParentBased)


class TestRedaccionDeUrls:
    """Las credenciales que viajan en la URL no llegan a los spans (ni a Jaeger)."""

    @pytest.mark.parametrize(
        ("url", "esperada"),
        [
            (
                "https://api.telegram.org/bot123456789:AAH-tok_en/sendMessage",
                "https://api.telegram.org/bot***/sendMessage",
            ),
            (
                "https://graph.facebook.com/v19.0/me/messages?access_token=EAAB123&x=1",
                "https://graph.facebook.com/v19.0/me/messages?access_token=***&x=1",
            ),
            (
                "https://api.example.com/v1?a=1&api_key=SECRETO&b=2",
                "https://api.example.com/v1?a=1&api_key=***&b=2",
            ),
            (
                "https://api.ycloud.com/v2/whatsapp/messages",
                "https://api.ycloud.com/v2/whatsapp/messages",
            ),
            ("https://x.test/a?page=3&size=10", "https://x.test/a?page=3&size=10"),
        ],
    )
    def test_redactar_url(self, url: str, esperada: str) -> None:
        """Quita el token del path de Telegram y los query params sensibles."""
        from app.core.telemetry import redactar_url

        assert redactar_url(url) == esperada

    @staticmethod
    def _exportador() -> tuple[Any, Any]:
        """Provider de prueba con un exportador en memoria.

        Returns:
            Tupla `(provider, exportador)`.
        """
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        exportador = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exportador))
        return provider, exportador

    def test_los_spans_de_un_cliente_async_salen_sin_el_token(self) -> None:
        """Con el instrumentor real, `http.url` y `url.full` no llevan el token.

        La instrumentacion guarda la URL completa en cada span; `meta.py` manda
        el `access_token` como query param y Telegram lleva el token del bot en
        el path. Un `AsyncClient` necesita el hook async (el codigo de
        produccion pasa los dos a `instrument()`).
        """
        import asyncio

        import httpx
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        from app.core.telemetry import _redactar_span_httpx_async

        provider, exportador = self._exportador()

        async def _llamar() -> None:
            transporte = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
            async with httpx.AsyncClient(transport=transporte) as cliente:
                HTTPXClientInstrumentor.instrument_client(
                    cliente, tracer_provider=provider, request_hook=_redactar_span_httpx_async
                )
                await cliente.post("https://api.telegram.org/bot123456:SECRETO-token/sendMessage")
                await cliente.get("https://graph.facebook.com/v19.0/me?access_token=EAABSECRETO")

        asyncio.run(_llamar())

        spans = exportador.get_finished_spans()
        assert len(spans) == 2
        for span in spans:
            assert "SECRETO" not in str(dict(span.attributes))
        assert spans[0].attributes["url.full"] == "https://api.telegram.org/bot***/sendMessage"
        assert spans[1].attributes["http.url"].endswith("?access_token=***")

    def test_los_spans_de_un_cliente_sincrono_salen_sin_el_token(self) -> None:
        """Lo mismo con `httpx.Client` y el hook sincrono."""
        import httpx
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        from app.core.telemetry import _redactar_span_httpx

        provider, exportador = self._exportador()
        transporte = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
        with httpx.Client(transport=transporte) as cliente:
            HTTPXClientInstrumentor.instrument_client(
                cliente, tracer_provider=provider, request_hook=_redactar_span_httpx
            )
            cliente.post("https://api.telegram.org/bot123456:SECRETO-token/sendMessage")

        (span,) = exportador.get_finished_spans()
        assert "SECRETO" not in str(dict(span.attributes))

    def test_una_url_sin_credenciales_no_toca_el_span(self) -> None:
        """Solo se sobreescribe el atributo cuando hay algo que redactar."""
        import asyncio

        import httpx
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        from app.core.telemetry import _redactar_span_httpx_async

        provider, exportador = self._exportador()

        async def _llamar() -> None:
            transporte = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
            async with httpx.AsyncClient(transport=transporte) as cliente:
                HTTPXClientInstrumentor.instrument_client(
                    cliente, tracer_provider=provider, request_hook=_redactar_span_httpx_async
                )
                await cliente.get("https://api.ycloud.com/v2/whatsapp/messages?page=2")

        asyncio.run(_llamar())

        (span,) = exportador.get_finished_spans()
        assert span.attributes["http.url"] == "https://api.ycloud.com/v2/whatsapp/messages?page=2"
        # `url.full` no lo pone el instrumentador por defecto: si el hook no
        # tiene nada que redactar, tampoco lo inventa.
        assert "url.full" not in span.attributes


class TestLoggingEstructurado:
    """El intercept de la stdlib y el formato JSON."""

    def test_la_raiz_de_logging_queda_interceptada(self) -> None:
        """Los ~40 módulos que usan `logging` salen por Loguru sin tocarlos."""
        setup_logging(force=True)
        assert any(isinstance(h, InterceptHandler) for h in logging.root.handlers)

    def test_el_sink_escribe_desde_un_hilo_de_fondo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`enqueue=True`: un stdout lento no debe bloquear el event loop de la API.

        Sin la cola, cada `logger.*()` escribe a stdout en el hilo llamante, que
        en la API es el unico del event loop del proceso (CLAUDE.md, regla 4).
        """
        opciones: list[dict[str, Any]] = []
        original = logger.add

        def _espia(*args: Any, **kwargs: Any) -> int:
            opciones.append(kwargs)
            return original(*args, **kwargs)

        monkeypatch.setattr(logger, "add", _espia)

        setup_logging(force=True)

        assert opciones, "setup_logging() no registro ningun sink"
        assert all(o.get("enqueue") is True for o in opciones)

    def test_el_hijo_prefork_de_celery_reconfigura_el_logging(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El fork copia `_configurado=True`, pero no el hilo del sink `enqueue`.

        Si `_instrumentar_proceso` llamara `setup_logging()` sin `force`, seria un
        no-op y los logs de cada worker quedarian encolados sin quien los escriba.
        """
        from app.tasks import observability as obs_module

        llamadas: list[bool] = []
        monkeypatch.setattr(obs_module, "setup_logging", lambda force=False: llamadas.append(force))
        monkeypatch.setattr(telemetry_module, "setup_celery_telemetry", lambda: None)
        monkeypatch.setattr(telemetry_module, "setup_telemetry", lambda **_: None)

        obs_module._instrumentar_proceso()

        assert llamadas == [True]

    def test_el_hijo_instrumenta_tambien_sql_redis_y_httpx(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Solo `CeleryInstrumentor` no basta: casi todo el trabajo esta en el worker.

        Las queries, Redis y las llamadas a YCloud/OpenAI ocurren en las tareas.
        Si el hijo no instrumenta el engine, la traza de un mensaje se corta en
        cuanto la tarea empieza a trabajar.
        """
        from app.core.database import engine
        from app.tasks import observability as obs_module

        recibidos: list[dict[str, Any]] = []
        monkeypatch.setattr(obs_module, "setup_logging", lambda force=False: None)
        monkeypatch.setattr(telemetry_module, "setup_celery_telemetry", lambda: None)
        monkeypatch.setattr(telemetry_module, "setup_telemetry", lambda **kw: recibidos.append(kw))

        obs_module._instrumentar_proceso()

        assert recibidos == [{"engine": engine}]

    def test_el_json_lleva_los_tres_campos_de_contexto(self) -> None:
        """`trace_id`, `client_id` y `user_id` en toda línea, aunque estén vacíos."""
        capturado: list[str] = []
        logger.remove()
        from app.core.logging import _serializar

        identificador = logger.add(lambda m: capturado.append(_serializar(m.record)), level="INFO")
        try:
            with logger.contextualize(trace_id="abc", client_id="cli", user_id="usr"):
                logger.info("mensaje de prueba")
        finally:
            logger.remove(identificador)

        registro = json.loads(capturado[0])
        assert registro["trace_id"] == "abc"
        assert registro["client_id"] == "cli"
        assert registro["user_id"] == "usr"
        assert registro["message"] == "mensaje de prueba"
        assert registro["level"] == "INFO"

    def test_el_intercept_conserva_el_modulo_real(self) -> None:
        """La línea del log apunta a quien llamó, no a `logging/__init__.py`.

        Con el `logging.currentframe()` del recetario viejo, todos los logs
        quedaban atribuidos a `callHandlers`, que no dice nada.
        """
        # `setup_logging()` hace `logger.remove()`: el sink de captura tiene que
        # engancharse despues, o se lo lleva por delante.
        setup_logging(force=True)
        capturado: list[Any] = []
        identificador = logger.add(lambda m: capturado.append(m.record), level="INFO")
        try:
            logging.getLogger("app.prueba").info("desde la stdlib")
        finally:
            logger.remove(identificador)

        assert capturado, "el record de la stdlib no llego a Loguru"
        assert capturado[0]["module"] != "__init__"

    def test_contexto_de_tarea_sin_traza(self) -> None:
        """`bind_task_context()` no falla fuera de un span."""
        capturado: list[Any] = []
        logger.remove()
        identificador = logger.add(lambda m: capturado.append(m.record), level="INFO")
        try:
            with bind_task_context(client_id="cli-1"):
                logger.info("en la tarea")
        finally:
            logger.remove(identificador)

        assert capturado[0]["extra"]["client_id"] == "cli-1"
        assert capturado[0]["extra"]["trace_id"] == ""


class TestMetricas:
    """Catálogo, helpers y registro."""

    def test_el_catalogo_tiene_las_metricas_del_spec(self) -> None:
        """Las que piden §3.1 y los dashboards existen con ese nombre exacto."""
        nombres = set(iter_metric_names())
        assert {
            "http_requests_total",
            "http_request_duration_seconds",
            "messages_processed_total",
            "llm_tokens_consumed_total",
            "handoff_total",
            "rag_retrieval_latency_seconds",
            "intent_routing_confidence",
        } <= nombres

    def test_los_tokens_se_separan_por_tipo(self) -> None:
        """Prompt y completion cuentan aparte: no cuestan lo mismo."""
        record_tokens("cli-1", "gpt-4o", "rag_query", 100, 20, cost_usd=0.5)
        texto = generate_latest(build_registry()).decode()
        assert 'token_type="prompt"' in texto
        assert 'token_type="completion"' in texto
        assert "llm_cost_usd_total" in texto

    def test_un_consumo_en_cero_no_ensucia_la_metrica(self) -> None:
        """Una llamada sin datos de uso no crea series con valor 0."""
        record_tokens("cli-sin-uso", "gpt-4o", "intent_routing", 0, 0)
        texto = generate_latest(build_registry()).decode()
        assert "cli-sin-uso" not in texto

    def test_el_cierre_masivo_suma_de_una_vez(self) -> None:
        """El auto-cierre resuelve por UPDATE masivo y suma el total de golpe."""
        record_conversation_resolved("cli-bulk", "auto_close", 7)
        texto = generate_latest(build_registry()).decode()
        assert (
            'conversations_resolved_total{client_id="cli-bulk",resolved_by="auto_close"} 7.0'
            in texto
        )

    def test_cantidad_no_positiva_no_registra(self) -> None:
        """Un UPDATE que no toco ninguna fila no incrementa nada."""
        record_conversation_resolved("cli-cero", "auto_close", 0)
        assert "cli-cero" not in generate_latest(build_registry()).decode()

    def test_un_fallo_de_metrica_no_escala(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Una métrica rota no puede tumbar el procesamiento de un mensaje."""

        def explota(*_: Any, **__: Any) -> None:
            raise RuntimeError("registro roto")

        monkeypatch.setattr(metrics_module.intent_routing_confidence, "labels", explota)
        record_intent("faq", 0.9)  # no debe propagar

    def test_rag_registra_latencia_y_chunks(self) -> None:
        """Las dos mitades del retrieval quedan medidas."""
        record_rag_retrieval("cli-rag", 0.02, 3)
        texto = generate_latest(build_registry()).decode()
        assert 'rag_retrieval_latency_seconds_count{client_id="cli-rag"} 1.0' in texto
        assert 'rag_chunks_retrieved_sum{client_id="cli-rag"} 3.0' in texto

    def test_el_cronometro_usa_reloj_monotono(self) -> None:
        """Nunca negativo: una observación negativa rompe el histograma."""
        with Cronometro() as cronometro:
            pass
        assert cronometro.elapsed >= 0

    def test_registro_multiproceso(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        """Con `PROMETHEUS_MULTIPROC_DIR` se construye un registro agregador.

        Es el modo obligatorio en producción: uvicorn corre con `--workers 2`.
        """
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        registro = build_registry()
        assert isinstance(registro, CollectorRegistry)
        from prometheus_client import REGISTRY

        assert registro is not REGISTRY


class TestMiddlewareDeObservabilidad:
    """El middleware sobre una app mínima, sin la del proyecto."""

    @staticmethod
    def _app() -> FastAPI:
        """Construye una app de prueba con el middleware montado.

        Returns:
            App con dos rutas: una con parámetro y otra que revienta.
        """
        app = FastAPI()
        app.add_middleware(ObservabilityMiddleware)

        @app.get("/eco/{identificador}")
        async def eco(identificador: str) -> dict[str, str]:
            """Devuelve el parámetro recibido."""
            return {"identificador": identificador}

        return app

    def test_la_etiqueta_endpoint_es_la_plantilla(self) -> None:
        """Dos ids distintos caen en la misma serie temporal.

        Con la URL concreta como etiqueta, cada contacto crearía una serie
        nueva y Prometheus terminaría guardando millones de series muertas.
        """
        registro_previo = generate_latest(build_registry()).decode()
        previo = registro_previo.count('endpoint="/eco/{identificador}"')

        cliente = TestClient(self._app())
        cliente.get("/eco/uno")
        cliente.get("/eco/dos")

        texto = generate_latest(build_registry()).decode()
        assert texto.count('endpoint="/eco/{identificador}"') > previo
        assert "/eco/uno" not in texto

    def test_devuelve_la_cabecera_de_traza(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`X-Trace-ID` permite buscar en Jaeger un incidente reportado."""
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "OTEL_EXPORTER_OTLP_ENDPOINT", "")
        app = self._app()
        setup_telemetry(app=app)
        try:
            respuesta = TestClient(app).get("/eco/uno")
            assert len(respuesta.headers["X-Trace-ID"]) == 32
        finally:
            telemetry_module.shutdown_telemetry()

    def test_sin_traza_no_inventa_cabecera(self) -> None:
        """Sin instrumentación no se devuelve un `X-Trace-ID` falso."""
        respuesta = TestClient(self._app()).get("/eco/uno")
        assert "X-Trace-ID" not in respuesta.headers

    def test_una_ruta_inexistente_no_abre_una_serie(self) -> None:
        """Un escaneo de rutas no puede multiplicar las series temporales."""
        TestClient(self._app()).get("/no/existe/12345")
        texto = generate_latest(build_registry()).decode()
        assert "/no/existe/12345" not in texto
        assert 'endpoint="<sin_ruta>"' in texto

    def test_plantilla_sin_ruta_resuelta(self) -> None:
        """Un request cortado antes del router no tiene plantilla."""

        class _RequestFalso:
            scope: ClassVar[dict[str, Any]] = {}

        assert _plantilla_de_ruta(_RequestFalso()) == "<sin_ruta>"  # type: ignore[arg-type]


class TestEndpointDeMetricas:
    """`/internal/metrics` sobre la app real."""

    def test_expone_el_formato_de_prometheus(self) -> None:
        """Content-type y cuerpo son los que espera el scrape."""
        from app.main import create_app

        respuesta = TestClient(create_app()).get("/internal/metrics")
        assert respuesta.status_code == 200
        assert "text/plain" in respuesta.headers["content-type"]
        assert "http_requests_total" in respuesta.text

    def test_la_lectura_del_registro_corre_fuera_del_event_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """En modo multiproceso `collect()` abre y lee archivos: I/O sincrono.

        Tiene que ejecutarse en un hilo aparte, no en el del event loop
        (CLAUDE.md, regla 4). `TestClient` ya corre la app en un hilo propio, asi
        que comparar hilos no distinguiria nada: se verifica que el endpoint
        delega en `asyncio.to_thread`.
        """
        import asyncio

        from app.api.internal import metrics as endpoint_module
        from app.main import create_app

        delegadas: list[Any] = []
        original = asyncio.to_thread

        async def _espia(funcion: Any, *args: Any, **kwargs: Any) -> Any:
            delegadas.append(funcion)
            return await original(funcion, *args, **kwargs)

        monkeypatch.setattr(endpoint_module.asyncio, "to_thread", _espia)

        respuesta = TestClient(create_app()).get("/internal/metrics")

        assert respuesta.status_code == 200
        assert len(delegadas) == 1, "generate_latest() no se delego a un hilo"

    def test_no_requiere_autenticacion(self) -> None:
        """Prometheus scrapea por la red interna, sin JWT."""
        from app.middleware.tenant_context import PUBLIC_PATHS

        assert "/internal/metrics" in PUBLIC_PATHS

    def test_el_propio_scrape_no_se_contabiliza(self) -> None:
        """Si se midiera, el scrape cada 15s taparia el trafico real."""
        from app.main import create_app

        cliente = TestClient(create_app())
        cliente.get("/internal/metrics")
        texto = cliente.get("/internal/metrics").text
        assert 'endpoint="/internal/metrics"' not in texto


class TestObservabilidadDeCelery:
    """Las señales que instrumentan el worker."""

    def test_las_señales_quedan_registradas(self) -> None:
        """Importar `celery_app` tiene que dejar el worker instrumentado.

        Es la unica garantia de que el worker arranque con observabilidad:
        nadie llama a `app/tasks/observability.py` explicitamente.
        """
        from celery.signals import task_postrun, task_prerun, worker_init

        import app.tasks.celery_app  # noqa: F401  — import por su efecto
        from app.tasks import observability

        assert observability._configurar_worker in _receptores(worker_init)
        assert observability._al_empezar in _receptores(task_prerun)
        assert observability._al_terminar in _receptores(task_postrun)

    def test_la_tarea_mide_duracion_y_estado(self) -> None:
        """`task_prerun`/`task_postrun` dejan las dos métricas de la tarea."""
        from app.tasks import observability

        class _TareaFalsa:
            name = "app.tasks.bulk_prueba"
            request = type("R", (), {"delivery_info": {"routing_key": "bulk"}})()

        tarea = _TareaFalsa()
        observability._al_empezar(task_id="t-1", task=tarea, kwargs={"client_id": "cli-9"})
        observability._al_terminar(task_id="t-1", task=tarea, state="SUCCESS")

        texto = generate_latest(build_registry()).decode()
        assert (
            'celery_tasks_total{queue="bulk",state="SUCCESS",task="app.tasks.bulk_prueba"} 1.0'
            in texto
        )
        assert (
            'celery_task_duration_seconds_count{queue="bulk",task="app.tasks.bulk_prueba"} 1.0'
            in texto
        )

    def test_un_mensaje_enviado_a_la_dlq_no_cuenta_como_exito(self) -> None:
        """`process_incoming_message` devuelve `{"status": "dlq"}` en vez de relanzar.

        Para Celery esa tarea termino en SUCCESS: sin distinguirlo, un mensaje
        abandonado tras agotar reintentos —que alguien tiene que revisar—
        quedaba en el dashboard y en las alertas como un exito mas.
        """
        from app.tasks import observability

        class _TareaFalsa:
            name = "app.tasks.webhook_dlq_prueba"
            request = type("R", (), {"delivery_info": {"routing_key": "webhooks"}})()

        tarea = _TareaFalsa()
        observability._al_empezar(task_id="t-dlq", task=tarea, kwargs={})
        observability._al_terminar(
            task_id="t-dlq", task=tarea, state="SUCCESS", retval={"status": "dlq"}
        )

        texto = generate_latest(build_registry()).decode()
        assert (
            'celery_tasks_total{queue="webhooks",state="DLQ",task="app.tasks.webhook_dlq_prueba"} 1.0'
            in texto
        )
        assert 'state="SUCCESS",task="app.tasks.webhook_dlq_prueba"' not in texto

    @pytest.mark.parametrize(
        ("state", "retval", "esperado"),
        [
            ("SUCCESS", {"status": "processed"}, "SUCCESS"),
            ("SUCCESS", None, "SUCCESS"),
            ("FAILURE", None, "FAILURE"),
            (None, None, "UNKNOWN"),
        ],
    )
    def test_estado_efectivo_deja_lo_demas_como_estaba(
        self, state: str | None, retval: Any, esperado: str
    ) -> None:
        """Solo `{"status": "dlq"}` cambia el estado que reporta Celery."""
        from app.tasks.observability import _estado_efectivo

        assert _estado_efectivo(state, retval) == esperado

    def test_postrun_sin_prerun_no_revienta(self) -> None:
        """Un postrun huérfano (worker reiniciado) no puede tumbar la tarea."""
        from app.tasks import observability

        class _TareaFalsa:
            name = "app.tasks.bulk_huerfana"
            request = None

        observability._al_terminar(task_id="t-inexistente", task=_TareaFalsa(), state="FAILURE")
        texto = generate_latest(build_registry()).decode()
        assert 'task="app.tasks.bulk_huerfana"' in texto


class TestRegistroPropio:
    """Detalle de `prometheus_client` que conviene dejar fijado."""

    def test_un_contador_se_puede_servir_desde_un_registro_aparte(self) -> None:
        """`build_registry()` no es el único registro posible: los tests aíslan."""
        registro = CollectorRegistry()
        contador = Counter("prueba_total", "Contador de prueba", registry=registro)
        contador.inc()
        assert "prueba_total 1.0" in generate_latest(registro).decode()


def test_http_request_se_contabiliza_con_su_estado() -> None:
    """`record_http_request()` etiqueta método, endpoint y código."""
    record_http_request("GET", "/api/v1/prueba", 503, 0.12)
    texto = generate_latest(build_registry()).decode()
    assert 'http_requests_total{endpoint="/api/v1/prueba",method="GET",status="503"} 1.0' in texto


def _receptores(señal: Any) -> list[Any]:
    """Funciones conectadas a una señal de Celery.

    `Signal.receivers` guarda pares (clave, receptor), y el receptor puede venir
    envuelto en una referencia débil según cómo se conectó.

    Args:
        señal: Señal de Celery.

    Returns:
        Las funciones conectadas, ya resueltas.
    """
    resueltos = []
    for _, receptor in señal.receivers:
        resueltos.append(receptor() if isinstance(receptor, weakref.ReferenceType) else receptor)
    return resueltos
