"""Tests del worker de auto-cierre de conversaciones.

No tocan la base: `tenant_session` y el listado de tenants se sustituyen. El
recorrido con PostgreSQL real (y RLS activa) vive en
`tests/integration/test_crm_api.py`.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.services.conversation_lifecycle import ConversationLifecycle
from app.tasks import auto_close as auto_close_module
from app.tasks.auto_close import (
    RESOLVED_DAYS,
    WAITING_CLIENT_HOURS,
    _auto_close,
    _load_active_client_ids,
)
from app.tasks.celery_config import celery_app
from tests.unit.agent_doubles import fake_tenant_session
from tests.unit.crm_doubles import CrmSession

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _fabrica_de_sesiones(session: CrmSession) -> Any:
    """Sustituto de `AsyncSessionLocal`, que se llama sin argumentos."""

    @asynccontextmanager
    async def _cm() -> AsyncIterator[CrmSession]:
        yield session

    return _cm


def _tenants(monkeypatch: pytest.MonkeyPatch, ids: list[uuid.UUID]) -> None:
    """Sustituye el listado de tenants por una lista fija."""

    async def _fake() -> list[uuid.UUID]:
        return ids

    monkeypatch.setattr(auto_close_module, "_load_active_client_ids", _fake)


# ─── Registro en Celery ──────────────────────────────────────────────────────


class TestRegistro:
    """La tarea tiene que ser la que Beat ya declara."""

    def test_el_nombre_coincide_con_el_beat_schedule(self) -> None:
        """`celery_config.beat_schedule` apunta a este nombre desde Sprint 2.

        Con el nombre de la spec (`app.tasks.auto_close_conversations`) la
        entrada de Beat quedaria apuntando a una tarea inexistente.
        """
        programada = celery_app.conf.beat_schedule["auto-close-conversations"]["task"]
        assert programada == "app.tasks.bulk_auto_close_conversations"

    def test_la_tarea_esta_registrada(self) -> None:
        """El worker la conoce al importar el modulo."""
        assert "app.tasks.bulk_auto_close_conversations" in celery_app.tasks

    def test_el_modulo_esta_en_task_modules(self) -> None:
        """Sin esto, el worker de la cola bulk levanta sin conocer la tarea (BUG-014)."""
        from app.tasks.celery_app import TASK_MODULES

        assert "app.tasks.auto_close" in TASK_MODULES

    def test_va_a_la_cola_bulk(self) -> None:
        """El barrido masivo no compite con los webhooks ni con la inferencia."""
        tarea = celery_app.tasks["app.tasks.bulk_auto_close_conversations"]
        assert tarea.queue == "bulk"


# ─── Coherencia con la maquina de estados ────────────────────────────────────


class TestCoherencia:
    """Las reglas del worker no pueden contradecir al ciclo de vida."""

    def test_las_dos_reglas_son_transiciones_validas(self) -> None:
        """waiting_client -> resolved y resolved -> archived estan en la tabla.

        Si alguien recorta la tabla de transiciones, este test cae antes de que
        el worker empiece a escribir estados que la API rechazaria.
        """
        assert ConversationLifecycle.validate_transition("waiting_client", "resolved")
        assert ConversationLifecycle.validate_transition("resolved", "archived")

    def test_los_umbrales_son_los_de_la_spec(self) -> None:
        """24 horas y 7 dias (spec §14)."""
        assert WAITING_CLIENT_HOURS == 24
        assert RESOLVED_DAYS == 7


# ─── Barrido ─────────────────────────────────────────────────────────────────


class TestBarrido:
    """Recorrido por tenants y conteo de lo cerrado."""

    async def test_suma_lo_resuelto_y_lo_archivado(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """El resultado reporta cuantas conversaciones movio cada regla."""
        _tenants(monkeypatch, [TENANT])
        session = CrmSession(rowcounts=[4, 2])
        monkeypatch.setattr(auto_close_module, "tenant_session", fake_tenant_session(session))

        resultado = await _auto_close()

        assert resultado == {
            "resolved": 4,
            "archived": 2,
            "tenants": 1,
            "tenants_failed": 0,
        }

    async def test_recorre_todos_los_tenants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Con varios tenants, los conteos se acumulan."""
        _tenants(monkeypatch, [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()])
        session = CrmSession(rowcounts=[1, 1, 1, 1, 1, 1])
        monkeypatch.setattr(auto_close_module, "tenant_session", fake_tenant_session(session))

        resultado = await _auto_close()

        assert resultado["tenants"] == 3
        assert resultado["resolved"] == 3
        assert resultado["archived"] == 3

    async def test_un_tenant_roto_no_corta_el_barrido(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si un tenant falla, se registra y se sigue con los demas."""
        bueno, malo = uuid.uuid4(), uuid.uuid4()
        _tenants(monkeypatch, [malo, bueno])

        sesiones = {malo: None, bueno: CrmSession(rowcounts=[5, 0])}

        def _tenant_session(client_id: uuid.UUID) -> Any:
            if sesiones[client_id] is None:
                raise RuntimeError("conexion caida")
            return fake_tenant_session(sesiones[client_id])(client_id)

        monkeypatch.setattr(auto_close_module, "tenant_session", _tenant_session)

        resultado = await _auto_close()

        assert resultado["tenants_failed"] == 1
        assert resultado["resolved"] == 5

    async def test_sin_tenants_no_hace_nada(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin tenants alcanzables, la pasada termina en cero sin reventar."""
        _tenants(monkeypatch, [])

        resultado = await _auto_close()

        assert resultado == {
            "resolved": 0,
            "archived": 0,
            "tenants": 0,
            "tenants_failed": 0,
        }

    async def test_las_dos_reglas_van_en_la_misma_transaccion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un solo `tenant_session` por tenant: dos UPDATE, una transaccion."""
        _tenants(monkeypatch, [TENANT])
        session = CrmSession(rowcounts=[0, 0])
        monkeypatch.setattr(auto_close_module, "tenant_session", fake_tenant_session(session))

        await _auto_close()

        sentencias = [str(s).lower() for s in session.executed]
        assert len(sentencias) == 2, "dos UPDATE, no uno por regla y tenant"
        assert all(s.startswith("update conversations") for s in sentencias)
        # El estado destino viaja como bind param, por eso se mira el origen.
        assert "conversations.status =" in sentencias[0]
        assert "conversations.updated_at <" in sentencias[0]
        assert "conversations.resolved_at <" in sentencias[1]

    async def test_el_client_id_va_explicito_en_los_update(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RLS es la segunda barrera, no la unica (CLAUDE.md §2)."""
        _tenants(monkeypatch, [TENANT])
        session = CrmSession(rowcounts=[0, 0])
        monkeypatch.setattr(auto_close_module, "tenant_session", fake_tenant_session(session))

        await _auto_close()

        for stmt in session.executed:
            assert "conversations.client_id =" in str(stmt)

    async def test_solo_archiva_lo_que_tiene_resolved_at(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Una conversacion resolved sin sello es un dato inconsistente, no se archiva."""
        _tenants(monkeypatch, [TENANT])
        session = CrmSession(rowcounts=[0, 0])
        monkeypatch.setattr(auto_close_module, "tenant_session", fake_tenant_session(session))

        await _auto_close()

        assert "resolved_at is not null" in str(session.executed[1]).lower()


# ─── Resolucion de tenants ───────────────────────────────────────────────────


class TestListadoDeTenants:
    """De donde sale la lista de tenants a barrer."""

    async def test_usa_clients_cuando_es_legible(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Con un rol que ve `clients`, se barren todos los tenants."""
        ids = [uuid.uuid4(), uuid.uuid4()]
        session = CrmSession(resultados=[[(i,) for i in ids]])
        monkeypatch.setattr(auto_close_module, "AsyncSessionLocal", _fabrica_de_sesiones(session))

        assert await _load_active_client_ids() == ids

    async def test_si_rls_bloquea_clients_cae_a_default_client_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Es el caso normal hoy: el rol de la app no puede listar `clients`.

        `webhook_processor` resuelve TODOS los mensajes entrantes a
        DEFAULT_CLIENT_ID, asi que ese tenant cubre lo que existe en el MVP.
        """

        def _revienta(*_: Any, **__: Any) -> Any:
            raise RuntimeError("permission denied for table clients")

        monkeypatch.setattr(auto_close_module, "AsyncSessionLocal", _revienta)
        monkeypatch.setattr(
            auto_close_module,
            "get_settings",
            lambda: type("S", (), {"DEFAULT_CLIENT_ID": str(TENANT)})(),
        )

        assert await _load_active_client_ids() == [TENANT]

    async def test_sin_clients_ni_default_devuelve_vacio(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin ninguna forma de saber a quien barrer, no se inventa nada."""

        def _revienta(*_: Any, **__: Any) -> Any:
            raise RuntimeError("permission denied for table clients")

        monkeypatch.setattr(auto_close_module, "AsyncSessionLocal", _revienta)
        monkeypatch.setattr(
            auto_close_module,
            "get_settings",
            lambda: type("S", (), {"DEFAULT_CLIENT_ID": ""})(),
        )

        assert await _load_active_client_ids() == []

    async def test_default_client_id_invalido_devuelve_vacio(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un DEFAULT_CLIENT_ID mal escrito se registra y no rompe la tarea."""

        def _revienta(*_: Any, **__: Any) -> Any:
            raise RuntimeError("permission denied for table clients")

        monkeypatch.setattr(auto_close_module, "AsyncSessionLocal", _revienta)
        monkeypatch.setattr(
            auto_close_module,
            "get_settings",
            lambda: type("S", (), {"DEFAULT_CLIENT_ID": "no-es-uuid"})(),
        )

        assert await _load_active_client_ids() == []
