"""Tests del engine de webhooks salientes: emisor y tasks (Sprint 11).

`tenant_session` se sustituye por una sesion falsa con cola de resultados, y
`WebhookDispatcher.send_webhook` por un doble que devuelve lo que cada test
necesita: lo que se prueba aca es la orquestacion (a quien se le encola, que
queda en la base, cuando se reintenta y cuando se desactiva), no el POST.
"""

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-testing-only-minimum-32-chars")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-minimum-32-characters-long")

from app.core.events import SUPPORTED_EVENTS, EventEmitter
from app.models.tenant_webhook import AUTO_DISABLED_REASON, MAX_CONSECUTIVE_FAILURES
from app.tasks import outgoing_webhooks as ow
from app.tasks.celery_app import TASK_MODULES
from app.tasks.celery_config import celery_app


class _Resultado:
    def __init__(self, filas: list) -> None:
        self._filas = filas

    def scalars(self):
        return list(self._filas)

    def scalar_one_or_none(self):
        return self._filas[0] if self._filas else None


class _SesionFalsa:
    def __init__(self, resultados: list[_Resultado] | None = None) -> None:
        self._resultados = list(resultados or [])
        self.added: list[object] = []

    async def execute(self, stmt: object = None, params: object = None) -> _Resultado:
        if self._resultados:
            return self._resultados.pop(0)
        return _Resultado([])

    def add(self, obj: object) -> None:
        self.added.append(obj)


def _tenant_session_falsa(sesion: _SesionFalsa):
    @asynccontextmanager
    async def _cm(client_id, user_id=None):
        yield sesion

    return _cm


def _webhook(**over):
    base = {
        "id": uuid4(),
        "url": "https://ejemplo.com/hook",
        "secret": "secreto",
        "headers": {},
        "is_active": True,
        "consecutive_failures": 0,
        "last_triggered_at": None,
        "last_success_at": None,
        "last_failure_at": None,
        "disabled_reason": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _payload(**over):
    base = {
        "event": "message.received",
        "timestamp": "2026-09-23T10:00:00+00:00",
        "webhook_delivery_id": str(uuid4()),
        "data": {},
    }
    base.update(over)
    return base


def _run_isolated_falso(resultado: dict):
    """Sustituto de `run_isolated` que cierra la corrutina en vez de ejecutarla.

    Sin el `close()`, Python avisa de una corrutina creada y nunca esperada.
    """

    def _run(coro):
        coro.close()
        return resultado

    return _run


class TestEventEmitter:
    @pytest.mark.asyncio
    async def test_encola_el_despacho_con_el_evento_y_el_tenant(self, eventos_emitidos) -> None:
        client_id = uuid4()

        await EventEmitter.emit("message.received", client_id, {"message_id": "1"})

        assert eventos_emitidos == [("message.received", str(client_id), {"message_id": "1"})]

    @pytest.mark.asyncio
    async def test_un_evento_desconocido_no_se_encola(self, eventos_emitidos) -> None:
        """Un typo en el nombre no debe llegar a la cola como evento valido."""
        await EventEmitter.emit("message.recieved", uuid4(), {})

        assert eventos_emitidos == []

    @pytest.mark.asyncio
    async def test_si_el_broker_falla_el_flujo_principal_sigue(self, monkeypatch) -> None:
        """Criterio 11: emitir no puede tumbar la respuesta a un contacto."""

        def _broker_caido(*args: object, **kwargs: object) -> None:
            raise ConnectionError("Redis no responde")

        monkeypatch.setattr(ow.dispatch_outgoing_webhooks, "apply_async", _broker_caido)

        await EventEmitter.emit("message.received", uuid4(), {})

    @pytest.mark.asyncio
    async def test_un_handler_que_falla_no_impide_el_despacho(self, eventos_emitidos) -> None:
        async def _handler_roto(event: str, client_id: str, data: dict) -> None:
            raise RuntimeError("handler mal escrito")

        EventEmitter.on("contact.created", _handler_roto)
        try:
            await EventEmitter.emit("contact.created", uuid4(), {})
        finally:
            EventEmitter.clear_handlers()

        assert len(eventos_emitidos) == 1

    @pytest.mark.asyncio
    async def test_los_handlers_registrados_reciben_el_evento(self, eventos_emitidos) -> None:
        """El CSAT de Dev B se engancha por aca a `conversation.resolved`."""
        recibidos: list[tuple] = []

        async def _handler(event: str, client_id: str, data: dict) -> None:
            recibidos.append((event, client_id, data))

        EventEmitter.on("conversation.resolved", _handler)
        client_id = uuid4()
        try:
            await EventEmitter.emit("conversation.resolved", client_id, {"conversation_id": "7"})
        finally:
            EventEmitter.clear_handlers()

        assert recibidos == [("conversation.resolved", str(client_id), {"conversation_id": "7"})]


class TestDespachar:
    @pytest.mark.asyncio
    async def test_encola_un_envio_por_webhook_suscrito(self) -> None:
        sesion = _SesionFalsa([_Resultado([_webhook(), _webhook()])])
        client_id = str(uuid4())

        with (
            patch("app.tasks.outgoing_webhooks.tenant_session", _tenant_session_falsa(sesion)),
            patch.object(ow.send_outgoing_webhook, "apply_async") as mock_envio,
        ):
            encolados = await ow._despachar("message.received", client_id, {"message_id": "1"})

        assert encolados == 2
        assert mock_envio.call_count == 2
        kwargs = mock_envio.call_args.kwargs["kwargs"]
        assert kwargs["attempt"] == 1
        assert kwargs["client_id"] == client_id
        assert kwargs["payload"]["event"] == "message.received"
        assert kwargs["payload"]["data"] == {"message_id": "1"}

    @pytest.mark.asyncio
    async def test_cada_webhook_recibe_su_propio_delivery_id(self) -> None:
        """El `webhook_delivery_id` identifica una entrega, no un evento global."""
        sesion = _SesionFalsa([_Resultado([_webhook(), _webhook()])])

        with (
            patch("app.tasks.outgoing_webhooks.tenant_session", _tenant_session_falsa(sesion)),
            patch.object(ow.send_outgoing_webhook, "apply_async") as mock_envio,
        ):
            await ow._despachar("message.received", str(uuid4()), {})

        ids = {
            llamada.kwargs["kwargs"]["payload"]["webhook_delivery_id"]
            for llamada in mock_envio.call_args_list
        }
        assert len(ids) == 2

    @pytest.mark.asyncio
    async def test_sin_webhooks_suscritos_no_encola_nada(self) -> None:
        sesion = _SesionFalsa([_Resultado([])])

        with (
            patch("app.tasks.outgoing_webhooks.tenant_session", _tenant_session_falsa(sesion)),
            patch.object(ow.send_outgoing_webhook, "apply_async") as mock_envio,
        ):
            encolados = await ow._despachar("contact.created", str(uuid4()), {})

        assert encolados == 0
        mock_envio.assert_not_called()

    @pytest.mark.asyncio
    async def test_la_consulta_filtra_activos_y_suscritos_al_evento(self) -> None:
        """El filtro va en el WHERE, no en Python: es lo que hace util al indice parcial."""
        sesion = _SesionFalsa([_Resultado([])])
        consultas: list[str] = []

        async def _execute(stmt, params=None):
            consultas.append(str(stmt.compile(compile_kwargs={"literal_binds": False})))
            return _Resultado([])

        sesion.execute = _execute  # type: ignore[method-assign]

        with patch("app.tasks.outgoing_webhooks.tenant_session", _tenant_session_falsa(sesion)):
            await ow._despachar("appointment.created", str(uuid4()), {})

        sql = consultas[0]
        assert "is_active IS true" in sql
        assert "ANY (tenant_webhooks.events)" in sql


class TestEnviar:
    @pytest.mark.asyncio
    async def test_exito_resetea_el_contador_y_deja_log(self) -> None:
        webhook = _webhook(consecutive_failures=4)
        sesion = _SesionFalsa([_Resultado([webhook])])

        with (
            patch("app.tasks.outgoing_webhooks.tenant_session", _tenant_session_falsa(sesion)),
            patch(
                "app.tasks.outgoing_webhooks.WebhookDispatcher.send_webhook",
                AsyncMock(
                    return_value={
                        "success": True,
                        "status_code": 200,
                        "duration_ms": 12,
                        "response_body": "OK",
                    }
                ),
            ),
        ):
            resultado = await ow._enviar(str(webhook.id), str(uuid4()), _payload(), 1)

        assert resultado["status"] == "success"
        assert webhook.consecutive_failures == 0
        assert webhook.last_success_at is not None
        log = sesion.added[0]
        assert log.status == "success"
        assert log.response_code == 200
        assert log.attempt == 1

    @pytest.mark.asyncio
    async def test_fallo_con_reintentos_pendientes_no_toca_el_contador(self) -> None:
        """`consecutive_failures` cuenta entregas, no intentos."""
        webhook = _webhook(consecutive_failures=2)
        sesion = _SesionFalsa([_Resultado([webhook])])

        with (
            patch("app.tasks.outgoing_webhooks.tenant_session", _tenant_session_falsa(sesion)),
            patch(
                "app.tasks.outgoing_webhooks.WebhookDispatcher.send_webhook",
                AsyncMock(
                    return_value={"success": False, "duration_ms": 9, "error": "HTTP 500: boom"}
                ),
            ),
        ):
            resultado = await ow._enviar(str(webhook.id), str(uuid4()), _payload(), 1)

        assert resultado == {"status": "failed", "retry_in": ow.RETRY_DELAYS[0]}
        assert webhook.consecutive_failures == 2
        assert webhook.last_failure_at is not None
        assert sesion.added[0].status == "failed"

    @pytest.mark.asyncio
    async def test_el_ultimo_intento_cuenta_la_entrega_como_fallida(self) -> None:
        webhook = _webhook(consecutive_failures=2)
        sesion = _SesionFalsa([_Resultado([webhook])])

        with (
            patch("app.tasks.outgoing_webhooks.tenant_session", _tenant_session_falsa(sesion)),
            patch(
                "app.tasks.outgoing_webhooks.WebhookDispatcher.send_webhook",
                AsyncMock(return_value={"success": False, "duration_ms": 9, "error": "timeout"}),
            ),
        ):
            resultado = await ow._enviar(str(webhook.id), str(uuid4()), _payload(), ow.MAX_ATTEMPTS)

        assert resultado == {"status": "failed", "retry_in": None}
        assert webhook.consecutive_failures == 3
        assert webhook.is_active is True

    @pytest.mark.asyncio
    async def test_se_desactiva_solo_al_llegar_al_umbral(self) -> None:
        """Criterio 3: una URL muerta deja de generar trafico inutil."""
        webhook = _webhook(consecutive_failures=MAX_CONSECUTIVE_FAILURES - 1)
        sesion = _SesionFalsa([_Resultado([webhook])])

        with (
            patch("app.tasks.outgoing_webhooks.tenant_session", _tenant_session_falsa(sesion)),
            patch(
                "app.tasks.outgoing_webhooks.WebhookDispatcher.send_webhook",
                AsyncMock(return_value={"success": False, "duration_ms": 9, "error": "timeout"}),
            ),
        ):
            resultado = await ow._enviar(str(webhook.id), str(uuid4()), _payload(), ow.MAX_ATTEMPTS)

        assert resultado["status"] == "disabled"
        assert webhook.is_active is False
        assert webhook.disabled_reason == AUTO_DISABLED_REASON

    @pytest.mark.asyncio
    async def test_un_webhook_borrado_no_es_un_fallo(self) -> None:
        sesion = _SesionFalsa([_Resultado([])])
        enviados = AsyncMock()

        with (
            patch("app.tasks.outgoing_webhooks.tenant_session", _tenant_session_falsa(sesion)),
            patch("app.tasks.outgoing_webhooks.WebhookDispatcher.send_webhook", enviados),
        ):
            resultado = await ow._enviar(str(uuid4()), str(uuid4()), _payload(), 1)

        assert resultado == {"status": "skipped"}
        enviados.assert_not_awaited()
        assert sesion.added == []

    @pytest.mark.asyncio
    async def test_un_webhook_desactivado_no_recibe_el_reintento(self) -> None:
        """Entre el encolado y el envio, el admin pudo apagarlo."""
        webhook = _webhook(is_active=False)
        sesion = _SesionFalsa([_Resultado([webhook])])
        enviados = AsyncMock()

        with (
            patch("app.tasks.outgoing_webhooks.tenant_session", _tenant_session_falsa(sesion)),
            patch("app.tasks.outgoing_webhooks.WebhookDispatcher.send_webhook", enviados),
        ):
            resultado = await ow._enviar(str(webhook.id), str(uuid4()), _payload(), 2)

        assert resultado == {"status": "skipped"}
        enviados.assert_not_awaited()


class TestReintento:
    def test_un_fallo_reintenta_con_el_backoff_del_spec(self) -> None:
        with (
            patch(
                "app.tasks.outgoing_webhooks.run_isolated",
                _run_isolated_falso({"status": "failed", "retry_in": 30}),
            ),
            patch.object(ow.send_outgoing_webhook, "apply_async") as mock_envio,
        ):
            ow.send_outgoing_webhook(
                webhook_id="w-1", client_id="c-1", payload=_payload(), attempt=2
            )

        assert mock_envio.call_args.kwargs["countdown"] == 30
        assert mock_envio.call_args.kwargs["kwargs"]["attempt"] == 3

    def test_agotados_los_intentos_no_se_reencola(self) -> None:
        with (
            patch(
                "app.tasks.outgoing_webhooks.run_isolated",
                _run_isolated_falso({"status": "failed", "retry_in": None}),
            ),
            patch.object(ow.send_outgoing_webhook, "apply_async") as mock_envio,
        ):
            ow.send_outgoing_webhook(
                webhook_id="w-1", client_id="c-1", payload=_payload(), attempt=ow.MAX_ATTEMPTS
            )

        mock_envio.assert_not_called()

    def test_un_exito_no_se_reencola(self) -> None:
        with (
            patch(
                "app.tasks.outgoing_webhooks.run_isolated",
                _run_isolated_falso({"status": "success", "status_code": 200}),
            ),
            patch.object(ow.send_outgoing_webhook, "apply_async") as mock_envio,
        ):
            ow.send_outgoing_webhook(webhook_id="w-1", client_id="c-1", payload=_payload())

        mock_envio.assert_not_called()

    def test_el_backoff_es_5_30_300(self) -> None:
        assert ow.RETRY_DELAYS == (5, 30, 300)
        assert ow.MAX_ATTEMPTS == 4


class TestRegistroDeLasTareas:
    def test_van_a_la_cola_notifications(self) -> None:
        """Con el nombre del spec el routing las habria mandado a `webhooks`."""
        for nombre in (
            "app.tasks.notification_dispatch_outgoing_webhooks",
            "app.tasks.notification_send_outgoing_webhook",
        ):
            assert nombre in celery_app.tasks
            assert celery_app.tasks[nombre].queue == "notifications"

    def test_el_modulo_esta_en_task_modules(self) -> None:
        """Sin esto el worker de notifications arranca sin conocer las tareas (BUG-014)."""
        assert "app.tasks.outgoing_webhooks" in TASK_MODULES

    def test_los_eventos_del_spec_estan_soportados(self) -> None:
        assert set(SUPPORTED_EVENTS) == {
            "message.received",
            "message.sent",
            "conversation.created",
            "conversation.resolved",
            "contact.created",
            "contact.updated",
            "appointment.created",
        }
