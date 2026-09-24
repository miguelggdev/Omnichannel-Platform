"""Tests de las tasks de CSAT (Sprint 11, Dev B).

Mismo patron que `test_outgoing_webhooks.py`: `tenant_session` se sustituye por
una sesion falsa con cola de resultados, y lo que se prueba es la orquestacion
(cuando se envia, cuando se salta, que queda en la base), no el envio real —
`deliver_message()` se sustituye por un doble.
"""

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-testing-only-minimum-32-chars")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-minimum-32-characters-long")

from app.tasks import csat_tasks as ct
from app.tasks.celery_app import TASK_MODULES


class _Resultado:
    def __init__(self, filas: list) -> None:
        self._filas = filas

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


def _conversation(**over):
    base = {"id": uuid4(), "client_id": uuid4(), "contact_id": uuid4(), "channel": "whatsapp"}
    base.update(over)
    base.setdefault("status", "resolved")
    return SimpleNamespace(**base)


def _contact(**over):
    base = {"id": uuid4(), "client_id": uuid4(), "first_name": "Ada"}
    base.update(over)
    return SimpleNamespace(**base)


class TestTaskModules:
    def test_csat_tasks_esta_registrado(self) -> None:
        """Sin esto el worker de `notifications`/`bulk` no conoce las tareas."""
        assert "app.tasks.csat_tasks" in TASK_MODULES


class TestEnviarEncuesta:
    @pytest.mark.asyncio
    async def test_conversacion_no_resuelta_se_salta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conv = _conversation(status="waiting_client")
        sesion = _SesionFalsa(resultados=[_Resultado([conv])])
        monkeypatch.setattr(ct, "tenant_session", _tenant_session_falsa(sesion))

        resultado = await ct._enviar_encuesta(str(conv.client_id), str(conv.id))

        assert resultado == {"status": "skipped_not_resolved"}

    @pytest.mark.asyncio
    async def test_conversacion_inexistente_se_salta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sesion = _SesionFalsa(resultados=[_Resultado([])])
        monkeypatch.setattr(ct, "tenant_session", _tenant_session_falsa(sesion))

        resultado = await ct._enviar_encuesta(str(uuid4()), str(uuid4()))

        assert resultado == {"status": "skipped_not_resolved"}

    @pytest.mark.asyncio
    async def test_encuesta_duplicada_se_salta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conv = _conversation()
        sesion = _SesionFalsa(
            resultados=[_Resultado([conv]), _Resultado([uuid4()])]  # survey_exists -> True
        )
        monkeypatch.setattr(ct, "tenant_session", _tenant_session_falsa(sesion))

        resultado = await ct._enviar_encuesta(str(conv.client_id), str(conv.id))

        assert resultado == {"status": "skipped_duplicate"}

    @pytest.mark.asyncio
    async def test_fallo_de_envio_no_registra_nada(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conv = _conversation()
        contacto = _contact(id=conv.contact_id)
        sesion = _SesionFalsa(
            resultados=[_Resultado([conv]), _Resultado([]), _Resultado([contacto])]
        )
        monkeypatch.setattr(ct, "tenant_session", _tenant_session_falsa(sesion))
        monkeypatch.setattr(
            ct, "deliver_message", AsyncMock(side_effect=RuntimeError("proveedor caido"))
        )

        resultado = await ct._enviar_encuesta(str(conv.client_id), str(conv.id))

        assert resultado == {"status": "skipped_send_failed"}
        assert sesion.added == []

    @pytest.mark.asyncio
    async def test_camino_feliz_envia_y_registra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conv = _conversation(channel="telegram")
        contacto = _contact(id=conv.contact_id, first_name="Grace")
        sesion = _SesionFalsa(
            resultados=[_Resultado([conv]), _Resultado([]), _Resultado([contacto])]
        )
        monkeypatch.setattr(ct, "tenant_session", _tenant_session_falsa(sesion))
        envio = AsyncMock(return_value="ext-123")
        monkeypatch.setattr(ct, "deliver_message", envio)

        resultado = await ct._enviar_encuesta(str(conv.client_id), str(conv.id))

        assert resultado == {"status": "sent"}
        assert len(sesion.added) == 1
        encuesta = sesion.added[0]
        assert encuesta.conversation_id == conv.id
        assert encuesta.contact_id == conv.contact_id
        assert encuesta.channel == "telegram"
        assert encuesta.survey_message_id == "ext-123"
        assert encuesta.status == "sent"
        # El texto que se envio incluye el nombre del contacto.
        assert "Grace" in envio.call_args.kwargs["text"]


class TestExpireOldSurveys:
    @pytest.mark.asyncio
    async def test_recorre_tenants_y_suma_expiradas(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        monkeypatch.setattr(
            ct, "_load_active_client_ids", AsyncMock(return_value=[tenant_a, tenant_b])
        )

        async def _expirar(tenant_id):
            return 3 if tenant_id == tenant_a else 1

        monkeypatch.setattr(ct, "_expirar_encuestas", _expirar)

        resultado = await ct._expirar_todas()

        assert resultado == {"expired": 4, "tenants": 2, "tenants_failed": 0}

    @pytest.mark.asyncio
    async def test_un_tenant_con_error_no_corta_el_recorrido(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        monkeypatch.setattr(
            ct, "_load_active_client_ids", AsyncMock(return_value=[tenant_a, tenant_b])
        )

        async def _expirar(tenant_id):
            if tenant_id == tenant_a:
                raise RuntimeError("boom")
            return 2

        monkeypatch.setattr(ct, "_expirar_encuestas", _expirar)

        resultado = await ct._expirar_todas()

        assert resultado == {"expired": 2, "tenants": 2, "tenants_failed": 1}


class TestOnConversationResolved:
    @pytest.mark.asyncio
    async def test_sin_conversation_id_no_hace_nada(self, monkeypatch: pytest.MonkeyPatch) -> None:
        apply_async = AsyncMock()
        monkeypatch.setattr(ct.send_csat_survey, "apply_async", apply_async)

        await ct._on_conversation_resolved("conversation.resolved", str(uuid4()), {})

        apply_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_encola_con_el_delay_configurado(self, monkeypatch: pytest.MonkeyPatch) -> None:
        llamadas = []
        monkeypatch.setattr(ct.send_csat_survey, "apply_async", lambda **kw: llamadas.append(kw))
        monkeypatch.setattr(
            ct, "get_settings", lambda: SimpleNamespace(CSAT_SURVEY_DELAY_MINUTES=7)
        )

        conversation_id = str(uuid4())
        client_id = str(uuid4())
        await ct._on_conversation_resolved(
            "conversation.resolved", client_id, {"conversation_id": conversation_id}
        )

        assert len(llamadas) == 1
        assert llamadas[0]["kwargs"] == {
            "client_id": client_id,
            "conversation_id": conversation_id,
        }
        assert llamadas[0]["countdown"] == 7 * 60

    @pytest.mark.asyncio
    async def test_broker_caido_no_propaga(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _revienta(**_kw):
            raise ConnectionError("Redis no responde")

        monkeypatch.setattr(ct.send_csat_survey, "apply_async", _revienta)

        await ct._on_conversation_resolved(
            "conversation.resolved", str(uuid4()), {"conversation_id": str(uuid4())}
        )
