"""Tests unitarios del worker de webhooks, sin base de datos ni Redis.

La sesion de SQLAlchemy y Redis se sustituyen por dobles, de modo que estos tests
cubren la logica de resolucion, reintentos y DLQ sin `--run-db`. El recorrido
completo contra PostgreSQL vive en `tests/integration/test_webhook_flow.py`.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from app.services import dedup as dedup_module
from app.tasks import webhook_processor as wp

# ─── Dobles ──────────────────────────────────────────────────────────────────


class FakeResult:
    """Resultado de `session.execute()` con un valor prefijado."""

    def __init__(self, value: Any = None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        """Devuelve el valor configurado."""
        return self._value


class FakeSession:
    """AsyncSession minima: registra los objetos agregados y sirve resultados."""

    def __init__(self, results: list[Any] | None = None, gets: dict[Any, Any] | None = None):
        self.added: list[Any] = []
        self.flushes = 0
        self._results = list(results or [])
        self._gets = gets or {}

    async def execute(self, *args: Any, **kwargs: Any) -> FakeResult:
        """Consume el siguiente resultado programado."""
        return FakeResult(self._results.pop(0) if self._results else None)

    def add(self, obj: Any) -> None:
        """Registra el objeto para poder afirmar sobre el."""
        self.added.append(obj)
        # Emula el server_default de id: el codigo necesita contact.id tras flush.
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()

    async def flush(self) -> None:
        """Cuenta los flush."""
        self.flushes += 1

    async def get(self, model: Any, pk: Any) -> Any:
        """Devuelve el objeto preprogramado para esa PK."""
        return self._gets.get(pk)


class FakeRedisList:
    """Redis en memoria que solo entiende LPUSH."""

    def __init__(self, raises: bool = False) -> None:
        self.lists: dict[str, list[str]] = {}
        self.raises = raises

    async def lpush(self, key: str, value: str) -> int:
        """Agrega al inicio de la lista, o falla si asi se configuro."""
        if self.raises:
            raise ConnectionError("redis caido")
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])


class FakeTaskSelf:
    """Sustituto del `self` de una tarea Celery con bind=True."""

    def __init__(self, retries: int = 0, max_retries: int = 3) -> None:
        self.request = type("Request", (), {"retries": retries})()
        self.max_retries = max_retries
        self.retry_calls = 0
        self.last_countdown: float | None = None

    def retry(self, exc: Exception | None = None, countdown: float | None = None) -> Exception:
        """Devuelve la excepcion que el codigo relanza como reintento."""
        self.retry_calls += 1
        self.last_countdown = countdown
        return RuntimeError("retry solicitado")


# ─── Timestamps ──────────────────────────────────────────────────────────────


class TestParseTimestamp:
    """`_parse_timestamp` normaliza lo que llegue a un datetime con timezone."""

    def test_iso_con_zona(self) -> None:
        """Un ISO-8601 con offset se respeta tal cual."""
        assert wp._parse_timestamp("2026-09-09T20:00:00+00:00") == datetime(
            2026, 9, 9, 20, 0, tzinfo=timezone.utc
        )

    def test_iso_con_sufijo_z(self) -> None:
        """El sufijo Z de los proveedores se interpreta como UTC."""
        assert wp._parse_timestamp("2026-09-09T20:00:00Z") == datetime(
            2026, 9, 9, 20, 0, tzinfo=timezone.utc
        )

    def test_naive_se_asume_utc(self) -> None:
        """Un datetime sin tz no puede guardarse tal cual: se asume UTC."""
        resultado = wp._parse_timestamp(datetime(2026, 9, 9, 20, 0))

        assert resultado.tzinfo is timezone.utc

    def test_valor_invalido_no_rompe(self) -> None:
        """Un timestamp corrupto no debe tumbar el procesamiento del mensaje."""
        resultado = wp._parse_timestamp("no-es-una-fecha")

        assert resultado.tzinfo is not None

    def test_ausente_usa_ahora(self) -> None:
        """Sin timestamp se usa el momento actual."""
        assert wp._parse_timestamp(None).tzinfo is not None


# ─── Resolucion de contacto ──────────────────────────────────────────────────


class TestResolverContacto:
    """`_resolve_contact` reutiliza o crea, y sigue la cadena de merges."""

    async def test_crea_contacto_e_identifier_si_no_existe(self) -> None:
        """Un remitente nuevo genera Contact + ContactIdentifier."""
        session = FakeSession(results=[None])
        client_id = uuid.uuid4()

        contacto = await wp._resolve_contact(session, client_id, "whatsapp", "573001112233")

        assert contacto.display_name == "573001112233"
        assert len(session.added) == 2, "debe crear contacto e identifier"
        assert session.added[1].identifier_value == "573001112233"

    async def test_reutiliza_contacto_existente(self) -> None:
        """Si ya hay identifier no se crea nada nuevo."""
        client_id = uuid.uuid4()
        contacto_id = uuid.uuid4()
        existente = type("Ident", (), {"contact_id": contacto_id})()
        contacto = type("Contact", (), {"id": contacto_id, "merged_into_id": None})()
        session = FakeSession(results=[existente], gets={contacto_id: contacto})

        resultado = await wp._resolve_contact(session, client_id, "whatsapp", "573001112233")

        assert resultado is contacto
        assert session.added == []

    async def test_sigue_la_cadena_de_merge(self) -> None:
        """Un contacto fusionado devuelve el superviviente."""
        client_id = uuid.uuid4()
        viejo_id, nuevo_id = uuid.uuid4(), uuid.uuid4()
        viejo = type("Contact", (), {"id": viejo_id, "merged_into_id": nuevo_id})()
        nuevo = type("Contact", (), {"id": nuevo_id, "merged_into_id": None})()
        existente = type("Ident", (), {"contact_id": viejo_id})()
        session = FakeSession(results=[existente], gets={viejo_id: viejo, nuevo_id: nuevo})

        resultado = await wp._resolve_contact(session, client_id, "whatsapp", "x")

        assert resultado is nuevo

    async def test_ciclo_de_merges_no_cuelga(self) -> None:
        """Una cadena circular corrupta corta en lugar de girar para siempre."""
        client_id = uuid.uuid4()
        a_id, b_id = uuid.uuid4(), uuid.uuid4()
        a = type("Contact", (), {"id": a_id, "merged_into_id": b_id})()
        b = type("Contact", (), {"id": b_id, "merged_into_id": a_id})()
        existente = type("Ident", (), {"contact_id": a_id})()
        session = FakeSession(results=[existente], gets={a_id: a, b_id: b})

        resultado = await wp._resolve_contact(session, client_id, "whatsapp", "x")

        assert resultado in (a, b)


# ─── Resolucion de conversacion ──────────────────────────────────────────────


class TestResolverConversacion:
    """`_resolve_conversation` reabre la activa o crea una nueva."""

    async def test_reutiliza_conversacion_activa(self) -> None:
        """Si hay hilo abierto en el canal, se reutiliza."""
        activa = object()
        session = FakeSession(results=[activa])

        resultado = await wp._resolve_conversation(session, uuid.uuid4(), uuid.uuid4(), "whatsapp")

        assert resultado is activa
        assert session.added == []

    async def test_crea_conversacion_en_bot_active(self) -> None:
        """Sin hilo abierto se crea uno nuevo listo para el grafo de agentes."""
        session = FakeSession(results=[None])

        resultado = await wp._resolve_conversation(session, uuid.uuid4(), uuid.uuid4(), "instagram")

        assert resultado.status == "bot_active"
        assert resultado.channel == "instagram"
        assert len(session.added) == 1


# ─── Dead Letter Queue ───────────────────────────────────────────────────────


class TestDeadLetterQueue:
    """Lo que agota reintentos tiene que quedar registrado."""

    async def test_escribe_entrada_completa(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """La entrada lleva provider, canal, mensaje y marca de tiempo."""
        fake = FakeRedisList()
        monkeypatch.setattr(wp, "get_redis", lambda: fake)

        await wp._send_to_dlq("meta", "instagram", {"external_message_id": "ig.1"})

        entrada = json.loads(fake.lists[wp.DLQ_KEY][0])
        assert entrada["provider"] == "meta"
        assert entrada["channel"] == "instagram"
        assert entrada["message"]["external_message_id"] == "ig.1"
        assert entrada["retries_exhausted"] is True
        assert entrada["failed_at"]

    async def test_redis_caido_no_propaga(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Si tampoco hay Redis, el fallo se registra pero no explota."""
        monkeypatch.setattr(wp, "get_redis", lambda: FakeRedisList(raises=True))

        await wp._send_to_dlq("ycloud", "whatsapp", {"external_message_id": "w.1"})


# ─── Encolado de IA ──────────────────────────────────────────────────────────


class TestEncoladoDeIA:
    """El paso de IA es opcional hasta Sprint 6."""

    def test_sin_ai_processor_no_falla(self) -> None:
        """El mensaje ya esta guardado: que no exista ai_processor no es un error."""
        wp._enqueue_ai_processing(
            client_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            contact_id=uuid.uuid4(),
            channel="whatsapp",
            message_data={},
        )


# ─── Procesamiento de mensaje: canal correcto hacia la IA ────────────────────


class TestProcessMessage:
    """`_process_message` debe encolar la IA con el canal resuelto, no el de la URL."""

    async def test_encola_ia_con_el_canal_resuelto_no_el_de_la_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Meta no separa webhooks por canal: el `channel` de la URL puede no
        coincidir con el canal real del mensaje (`message_data["channel"]`).
        La IA debe encolarse con el segundo, igual que el contacto y la
        conversacion, no con el primero.
        """
        client_id = uuid.uuid4()
        monkeypatch.setattr(wp, "_resolve_client_id", lambda provider, channel: client_id)

        session = FakeSession(results=[None, None])

        class FakeTenantSession:
            async def __aenter__(self) -> FakeSession:
                return session

            async def __aexit__(self, *exc: Any) -> None:
                return None

        monkeypatch.setattr(wp, "tenant_session", lambda _client_id: FakeTenantSession())

        async def sin_duplicado(*args: Any, **kwargs: Any) -> bool:
            return False

        async def persistido_ok(*args: Any, **kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(wp, "is_duplicate_persisted", sin_duplicado)
        monkeypatch.setattr(wp, "persist_dedup", persistido_ok)

        capturado: dict[str, Any] = {}
        monkeypatch.setattr(wp, "_enqueue_ai_processing", lambda **kw: capturado.update(kw))

        message_data = {
            "external_message_id": "fb.1",
            "sender_identifier": "psid123",
            "channel": "facebook",
        }

        await wp._process_message("meta", "instagram", message_data)

        assert capturado["channel"] == "facebook"


# ─── Tarea Celery: reintentos y DLQ ──────────────────────────────────────────


class TestTareaCelery:
    """Contrato de reintentos de `process_incoming_message`."""

    # Celery expone la funcion original en __wrapped__ ya ligada a la instancia de
    # la tarea; __func__ la devuelve sin ligar para poder inyectar un `self` falso.
    cuerpo = staticmethod(wp.process_incoming_message.__wrapped__.__func__)

    def test_exito_devuelve_processed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un procesamiento sin errores responde processed."""

        async def ok(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(wp, "_process_message", ok)

        resultado = wp.process_incoming_message.run(
            "ycloud", "whatsapp", {"external_message_id": "w.ok"}
        )

        assert resultado == {"status": "processed"}

    def test_fallo_con_reintentos_disponibles_reintenta(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con reintentos pendientes se relanza, no se manda a la DLQ."""

        async def boom(*args: Any, **kwargs: Any) -> None:
            raise ValueError("fallo temporal")

        monkeypatch.setattr(wp, "_process_message", boom)
        task_self = FakeTaskSelf(retries=0)

        with pytest.raises(RuntimeError):
            self.cuerpo(task_self, "ycloud", "whatsapp", {"external_message_id": "w.retry"})

        assert task_self.retry_calls == 1
        assert task_self.last_countdown == 5

    @pytest.mark.parametrize(("retries", "countdown_esperado"), [(0, 5), (1, 25), (2, 125)])
    def test_backoff_exponencial_5_25_125(
        self, monkeypatch: pytest.MonkeyPatch, retries: int, countdown_esperado: int
    ) -> None:
        """El countdown del reintento sigue el backoff documentado 5s -> 25s -> 125s."""

        async def boom(*args: Any, **kwargs: Any) -> None:
            raise ValueError("fallo temporal")

        monkeypatch.setattr(wp, "_process_message", boom)
        task_self = FakeTaskSelf(retries=retries)

        with pytest.raises(RuntimeError):
            self.cuerpo(task_self, "ycloud", "whatsapp", {"external_message_id": "w.retry"})

        assert task_self.last_countdown == countdown_esperado

    def test_reintentos_agotados_van_a_dlq(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Al tercer fallo el mensaje se archiva en la DLQ en vez de perderse."""

        async def boom(*args: Any, **kwargs: Any) -> None:
            raise ValueError("fallo permanente")

        fake = FakeRedisList()
        monkeypatch.setattr(wp, "_process_message", boom)
        monkeypatch.setattr(wp, "get_redis", lambda: fake)
        task_self = FakeTaskSelf(retries=3)

        resultado = self.cuerpo(task_self, "ycloud", "whatsapp", {"external_message_id": "w.dlq"})

        assert resultado == {"status": "dlq"}
        assert task_self.retry_calls == 0
        assert len(fake.lists[wp.DLQ_KEY]) == 1


# ─── Dedup persistente con sesion inyectada ──────────────────────────────────


class TestDedupPersistente:
    """`persist_dedup` / `is_duplicate_persisted` con la sesion del worker."""

    async def test_persist_usa_la_sesion_recibida(self) -> None:
        """Con session dada no se abre transaccion propia; entra en la del worker."""
        session = FakeSession()
        client_id = uuid.uuid4()

        creado = await dedup_module.persist_dedup(client_id, "whatsapp", "wamid.1", session=session)

        assert creado is True
        assert len(session.added) == 1
        assert session.added[0].external_message_id == "wamid.1"
        assert session.flushes == 1

    async def test_is_duplicate_detecta_existente(self) -> None:
        """Si la consulta devuelve una fila, el mensaje ya se proceso."""
        session = FakeSession(results=[uuid.uuid4()])

        assert (
            await dedup_module.is_duplicate_persisted(
                uuid.uuid4(), "whatsapp", "wamid.1", session=session
            )
            is True
        )

    async def test_is_duplicate_devuelve_false_si_no_existe(self) -> None:
        """Sin fila previa, el mensaje es nuevo."""
        session = FakeSession(results=[None])

        assert (
            await dedup_module.is_duplicate_persisted(
                uuid.uuid4(), "whatsapp", "wamid.2", session=session
            )
            is False
        )
