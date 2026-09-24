"""Coherencia entre las colas de Celery y los servicios de `docker-compose.yml`.

El compose lista las variables de entorno de cada servicio una por una: una variable
que el codigo lee de `Settings` y que el servicio no declara llega vacia, y el fallo
solo aparece en produccion (un canal "sin credenciales", un webhook que siempre da 401).
Estos tests fijan lo que ya rompio o puede romper en silencio.
"""

import re
import typing
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic_core import PydanticUndefined

from app.core.config import Settings
from app.tasks import audio_transcription
from app.tasks.celery_config import celery_app

RAIZ = Path(__file__).resolve().parents[2]

#: Todo lo que necesita un worker para ENVIAR por los canales (`deliver_message`).
ENV_CANALES = frozenset(
    {
        "DEFAULT_CLIENT_ID",
        "YCLOUD_API_KEY",
        "YCLOUD_PHONE_NUMBER_ID",
        "YCLOUD_BASE_URL",
        "META_PAGE_ACCESS_TOKEN",
        "META_GRAPH_API_VERSION",
        "TELEGRAM_CHANNEL_BOT_TOKEN",
        "TELEGRAM_API_BASE_URL",
        "TELEGRAM_MAX_MESSAGES_PER_SECOND",
        "EMAIL_SMTP_HOST",
        "EMAIL_SMTP_PORT",
        "EMAIL_SMTP_USER",
        "EMAIL_SMTP_PASSWORD",
        "EMAIL_FROM_ADDRESS",
        "EMAIL_FROM_NAME",
    }
)

#: Lo que necesita `celery-webhooks` para `answerCallbackQuery` — llama a Telegram
#: (`_responder_callback_de_telegram`) antes de persistir el mensaje, asi que solo
#: hace falta ese canal, no el resto de `ENV_CANALES`.
ENV_TELEGRAM_RESPONDER = frozenset(
    {"TELEGRAM_CHANNEL_BOT_TOKEN", "TELEGRAM_API_BASE_URL", "TELEGRAM_MAX_MESSAGES_PER_SECOND"}
)

ENV_WHISPER = frozenset(
    {
        "OPENAI_API_KEY",
        "WHISPER_MODEL",
        "WHISPER_LANGUAGE",
        "WHISPER_MAX_AUDIO_BYTES",
        "WHISPER_COST_PER_MINUTE_USD",
        "WHISPER_TIMEOUT_SECONDS",
        "MEDIA_DOWNLOAD_TIMEOUT_SECONDS",
    }
)

ENV_API_CANALES = frozenset(
    {
        "DEFAULT_CLIENT_ID",
        "TELEGRAM_WEBHOOK_SECRET",
        "EMAIL_INBOUND_WEBHOOK_SECRET",
        "WEBCHAT_CHANNEL_TOKEN",
        "WEBCHAT_ALLOWED_ORIGINS",
    }
)


@pytest.fixture(scope="module")
def servicios() -> dict[str, dict[str, Any]]:
    """Servicios de `docker-compose.yml`."""
    compose = yaml.safe_load((RAIZ / "docker-compose.yml").read_text(encoding="utf-8"))
    return dict(compose["services"])


def _env(servicio: dict[str, Any]) -> dict[str, str]:
    """Variables `NOMBRE=valor` de un servicio."""
    resultado: dict[str, str] = {}
    for entrada in servicio.get("environment", []):
        nombre, _, valor = str(entrada).partition("=")
        resultado[nombre] = valor
    return resultado


def _cola_de(servicio: dict[str, Any]) -> str | None:
    """Cola que consume un worker de Celery (`-Q cola`), o `None` si no lo es."""
    comando = str(servicio.get("command", ""))
    encontrado = re.search(r"-Q\s+(\S+)", comando)
    return encontrado.group(1) if encontrado else None


def _worker_de(servicios: dict[str, dict[str, Any]], cola: str) -> dict[str, Any]:
    for servicio in servicios.values():
        if _cola_de(servicio) == cola:
            return servicio
    pytest.fail(f"Ningun servicio del compose consume la cola {cola!r}")


class TestColaMedia:
    def test_la_tarea_de_whisper_se_enruta_a_la_cola_media(self) -> None:
        tarea = audio_transcription.transcribe_audio_message

        assert tarea.name == "app.tasks.media_transcribe_audio"
        assert tarea.queue == "media"
        ruta = celery_app.amqp.router.route({}, tarea.name)
        assert ruta["queue"].name == "media"

    def test_la_tarea_de_ia_sigue_en_ai_inference(self) -> None:
        """Whisper sale de esa cola; el grafo se queda."""
        ruta = celery_app.amqp.router.route({}, "app.tasks.ai_process_response")

        assert ruta["queue"].name == "ai_inference"

    def test_la_cola_esta_declarada(self) -> None:
        assert "media" in {q.name for q in celery_app.conf.task_queues}

    def test_el_worker_existe_en_el_compose(self, servicios: dict[str, dict[str, Any]]) -> None:
        worker = _worker_de(servicios, "media")

        assert "media@" in " ".join(worker["healthcheck"]["test"])
        assert _env(worker)["OTEL_SERVICE_NAME"] == "omnichannel-worker-media"

    def test_la_concurrencia_es_configurable_con_default_2(
        self, servicios: dict[str, dict[str, Any]]
    ) -> None:
        assert "${CELERY_MEDIA_CONCURRENCY:-2}" in _worker_de(servicios, "media")["command"]

    def test_el_worker_de_media_tiene_lo_que_necesita_whisper(
        self, servicios: dict[str, dict[str, Any]]
    ) -> None:
        faltan = ENV_WHISPER - _env(_worker_de(servicios, "media")).keys()

        assert not faltan, f"celery-media sin {sorted(faltan)}"

    def test_ai_inference_ya_no_tiene_que_cargar_con_whisper(
        self, servicios: dict[str, dict[str, Any]]
    ) -> None:
        """Whisper corre en `media`: `ai_inference` no necesita sus limites de descarga."""
        env = _env(_worker_de(servicios, "ai_inference"))

        assert "WHISPER_MAX_AUDIO_BYTES" not in env


class TestInvarianteColasYWorkers:
    def test_toda_cola_declarada_tiene_un_worker(
        self, servicios: dict[str, dict[str, Any]]
    ) -> None:
        """Una cola sin worker acumula tareas para siempre, sin ningun error visible."""
        consumidas = {_cola_de(s) for s in servicios.values()}
        for cola in celery_app.conf.task_queues:
            assert cola.name in consumidas, f"la cola {cola.name!r} no tiene worker"

    def test_toda_cola_se_mide_en_redis(self, servicios: dict[str, dict[str, Any]]) -> None:
        exportador = servicios["redis-exporter"]["environment"]["REDIS_EXPORTER_CHECK_KEYS"]
        medidas = {p.split("=", 1)[1] for p in exportador.replace("\n", "").split(",") if p}

        for cola in celery_app.conf.task_queues:
            assert cola.name in medidas, f"la profundidad de {cola.name!r} no se exporta"

    def test_las_colas_de_la_config_existen_como_tarea_ruteada(self) -> None:
        rutas = celery_app.conf.task_routes
        colas = {q.name for q in celery_app.conf.task_queues}

        assert {r["queue"] for r in rutas.values()} <= colas


class TestCredencialesDeCanales:
    """Un worker que responde llama a `get_channel_config()`: sin esto, el canal
    "no esta configurado" y el contacto se queda sin respuesta."""

    # `bulk` entra en la lista desde el Sprint 12: ahi corren las campanas
    # masivas (`app.tasks.bulk_execute_campaign`), que mandan mensajes.
    @pytest.mark.parametrize("cola", ["ai_inference", "notifications", "media", "bulk"])
    def test_los_workers_que_envian_tienen_las_credenciales(
        self, servicios: dict[str, dict[str, Any]], cola: str
    ) -> None:
        faltan = ENV_CANALES - _env(_worker_de(servicios, cola)).keys()

        assert not faltan, f"el worker de {cola!r} no declara {sorted(faltan)}"

    def test_la_api_valida_los_webhooks_y_sirve_el_webchat(
        self, servicios: dict[str, dict[str, Any]]
    ) -> None:
        """Sin los secretos, Telegram y email responden siempre 401; sin el token, el
        Webchat esta desactivado."""
        faltan = ENV_API_CANALES - _env(servicios["api"]).keys()

        assert not faltan, f"la API no declara {sorted(faltan)}"

    def test_celery_webhooks_puede_responder_el_callback_de_telegram(
        self, servicios: dict[str, dict[str, Any]]
    ) -> None:
        """`_responder_callback_de_telegram` corre en `celery-webhooks`, no en `celery-ai`."""
        faltan = ENV_TELEGRAM_RESPONDER - _env(servicios["celery-webhooks"]).keys()

        assert not faltan, f"celery-webhooks no declara {sorted(faltan)}"


class TestDefaultsDeVariablesNoTexto:
    """Vale para TODAS las variables de `Settings` que el compose pasa, no solo las nuevas.

    `${X}` sin definir llega como cadena vacia: un `int`, un `float` o una lista de
    `Settings` con "" revientan al arrancar (o pierden su valor por defecto)."""

    @staticmethod
    def _tiene_default(nombre: str) -> bool:
        """Los campos obligatorios (`DATABASE_URL`, `JWT_SECRET`) no tienen default."""
        campo = Settings.model_fields.get(nombre)
        return campo is not None and campo.default is not PydanticUndefined

    @staticmethod
    def _no_texto(nombre: str) -> bool:
        campo = Settings.model_fields.get(nombre)
        if campo is None:
            return False
        tipo = campo.annotation
        return typing.get_origin(tipo) is list or tipo in (int, float)

    def test_toda_variable_numerica_o_lista_lleva_default(
        self, servicios: dict[str, dict[str, Any]]
    ) -> None:
        for nombre_servicio, servicio in servicios.items():
            for nombre, valor in _env(servicio).items():
                if not valor.startswith("${"):
                    continue  # valor fijo: no depende del entorno
                if self._no_texto(nombre) and self._tiene_default(nombre):
                    assert ":-" in valor, (
                        f"{nombre_servicio}: {nombre}={valor} llega vacia si no se define"
                    )

    def test_las_de_texto_con_default_no_vacio_tambien_lo_llevan(
        self, servicios: dict[str, dict[str, Any]]
    ) -> None:
        """`${WHISPER_MODEL}` sin definir pisaria el `whisper-1` de `Settings` con ''."""
        for nombre_servicio, servicio in servicios.items():
            for nombre, valor in _env(servicio).items():
                if not valor.startswith("${"):
                    continue  # valor fijo: no depende del entorno
                campo = Settings.model_fields.get(nombre)
                if campo is None or self._no_texto(nombre) or not self._tiene_default(nombre):
                    continue
                if isinstance(campo.default, str) and campo.default:
                    assert ":-" in valor, (
                        f"{nombre_servicio}: {nombre}={valor} pisa el default "
                        f"{campo.default!r} con una cadena vacia"
                    )


#: Variables que el compose pasa a un servicio y que `Settings` no lee, a proposito:
#: las leen otras piezas (Celery, OpenTelemetry, el modo multiproceso de Prometheus) o
#: los proveedores de enriquecimiento, que no pasan por `Settings`.
NO_SETTINGS_PERMITIDAS = frozenset(
    {
        "CELERY_BROKER_URL",
        "CELERY_RESULT_BACKEND",
        "OTEL_SERVICE_NAME",
        "PROMETHEUS_MULTIPROC_DIR",
        "BLAND_AI_API_KEY",
        "BLAND_AI_PHONE_NUMBER",
        "CLEARBIT_API_KEY",
        "HUNTER_API_KEY",
        "VAPI_API_KEY",
        "VAPI_PHONE_NUMBER",
    }
)

#: Lo que necesita cada servicio ademas de lo comun, segun el codigo que ejecuta.
REQUERIDAS_POR_SERVICIO: dict[str, frozenset[str]] = {
    # `auto_close` resuelve el tenant con DEFAULT_CLIENT_ID; `tenant_cloner`
    # (Sprint 10) copia archivos de Storage al clonar documentos; y el envio de
    # campanas (Sprint 12) aplica aca su limite de mensajes por segundo.
    "celery-bulk": frozenset(
        {
            "DEFAULT_CLIENT_ID",
            "SUPABASE_URL",
            "SUPABASE_SECRET_KEY",
            "SUPABASE_STORAGE_BUCKET",
            "CAMPAIGN_MAX_MESSAGES_PER_SECOND",
        }
    ),
    # `document_pipeline` descarga el archivo subido desde Supabase Storage.
    "celery-documents": frozenset(
        {"SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_STORAGE_BUCKET", "OPENAI_EMBEDDING_MODEL"}
    ),
    # El agente de agendamiento usa Google Calendar; el modelo de chat lo lee
    # `_tenant`; y el agente financiero (Sprint 12) habla con la DIAN desde el
    # grafo, que corre en este worker.
    "celery-ai": frozenset(
        {
            "GOOGLE_CALENDAR_CREDENTIALS_JSON",
            "GOOGLE_CALENDAR_ID",
            "OPENAI_CHAT_MODEL",
            "DIAN_API_URL",
            "DIAN_API_TOKEN",
            "DIAN_TIMEOUT_SECONDS",
        }
    ),
    # El engine de webhooks salientes corre entero en este worker (Sprint 11).
    "celery-notifications": frozenset(
        {"OUTGOING_WEBHOOK_TIMEOUT_SECONDS", "OUTGOING_WEBHOOK_ALLOW_PRIVATE_HOSTS"}
    ),
    "api": frozenset({"SUPABASE_STORAGE_BUCKET", "JWT_REFRESH_EXPIRATION_DAYS"}),
}


class TestOtrosWorkers:
    """Lo que el codigo de cada servicio lee de `Settings` y el compose debe pasarle."""

    @pytest.mark.parametrize("nombre", sorted(REQUERIDAS_POR_SERVICIO))
    def test_el_servicio_declara_lo_que_su_codigo_lee(
        self, servicios: dict[str, dict[str, Any]], nombre: str
    ) -> None:
        faltan = REQUERIDAS_POR_SERVICIO[nombre] - _env(servicios[nombre]).keys()

        assert not faltan, f"{nombre} no declara {sorted(faltan)}"

    def test_la_api_expone_todos_los_limites_del_webchat(
        self, servicios: dict[str, dict[str, Any]]
    ) -> None:
        """Si se agrega un limite `WEBCHAT_*` a `Settings`, el compose debe poder cambiarlo."""
        del_settings = {n for n in Settings.model_fields if n.startswith("WEBCHAT_")}

        assert del_settings <= _env(servicios["api"]).keys()

    def test_el_modelo_de_chat_usa_el_nombre_que_lee_settings(
        self, servicios: dict[str, dict[str, Any]]
    ) -> None:
        """El compose pasaba `OPENAI_DEFAULT_MODEL`, que `Settings` ignora: el modelo por
        defecto no se podia cambiar desde el entorno."""
        for nombre, servicio in servicios.items():
            assert "OPENAI_DEFAULT_MODEL" not in _env(servicio), nombre

    def test_ninguna_variable_del_compose_es_ignorada_por_settings(
        self, servicios: dict[str, dict[str, Any]]
    ) -> None:
        """Una variable mal escrita en el compose no falla: simplemente no hace nada."""
        for nombre, servicio in servicios.items():
            if nombre != "api" and not nombre.startswith("celery-"):
                continue
            for variable in _env(servicio):
                assert variable in Settings.model_fields or variable in NO_SETTINGS_PERMITIDAS, (
                    f"{nombre}: {variable} no es un campo de Settings"
                )
