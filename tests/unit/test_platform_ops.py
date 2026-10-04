"""Operacion de la plataforma (Celery, Redis, sistema): normalizacion y permisos.

La salida de `inspect` se sustituye por datos de ejemplo con la forma real de Celery: corren
sin broker ni workers. El recorrido contra Redis real esta en
`tests/integration/test_platform_ops.py`.
"""

import uuid
from typing import Any

import pytest

from app.api.v1 import platform_ops as modulo
from app.api.v1.platform_ops import construir_tareas, construir_workers

# Forma real de lo que devuelven stats/active/reserved/scheduled/active_queues.
DATOS: dict[str, dict[str, Any]] = {
    "stats": {
        "celery@webhooks": {
            "pid": 101,
            "uptime": 3600,
            "pool": {"max-concurrency": 4},
            "total": {"app.tasks.webhook_processor.process": 120, "app.tasks.ai_processor.run": 30},
        },
        "celery@ai": {"pid": 102, "uptime": 60, "pool": {"max-concurrency": 2}, "total": {}},
    },
    "active": {
        "celery@webhooks": [
            {
                "id": "a1",
                "name": "app.tasks.webhook_processor.process",
                "args": ["TEXTO PRIVADO DEL CONTACTO"],
                "kwargs": {"phone": "+573001234567"},
                "delivery_info": {"routing_key": "webhooks"},
                "time_start": 1000.0,
            },
            {"id": "a0", "name": "app.tasks.ai_processor.run", "delivery_info": {"routing_key": "ai_inference"}, "time_start": 900.0},
        ],
        "celery@ai": [],
    },
    "reserved": {"celery@ai": [{"id": "r1", "name": "app.tasks.document_ingestion.run", "delivery_info": {"routing_key": "documents"}}]},
    "scheduled": {
        "celery@webhooks": [
            {"eta": "2026-10-05T00:00:00", "request": {"id": "s1", "name": "app.tasks.bulk_purge", "delivery_info": {"routing_key": "bulk"}}}
        ]
    },
    "queues": {"celery@webhooks": [{"name": "webhooks"}, {"name": "bulk"}], "celery@ai": [{"name": "ai_inference"}]},
}  # fmt: skip


class TestConstruirWorkers:
    def test_une_las_cinco_respuestas_en_una_fila_por_worker(self) -> None:
        workers = {w.hostname: w for w in construir_workers(DATOS)}

        w = workers["celery@webhooks"]
        assert (w.pid, w.concurrency, w.uptime_seconds) == (101, 4, 3600)
        assert w.active_tasks == 2
        assert w.reserved_tasks == 0
        assert w.processed_total == 150
        assert w.queues == ["bulk", "webhooks"]
        assert workers["celery@ai"].reserved_tasks == 1
        assert workers["celery@ai"].processed_total is None  # sin historial: no es "0"

    def test_ordena_por_nombre(self) -> None:
        assert [w.hostname for w in construir_workers(DATOS)] == ["celery@ai", "celery@webhooks"]

    def test_sin_respuestas_no_hay_workers(self) -> None:
        assert construir_workers({"stats": None, "active": None}) == []
        assert construir_workers({}) == []


class TestConstruirTareas:
    def test_normaliza_activas_reservadas_y_programadas(self) -> None:
        tareas = construir_tareas(DATOS)

        assert [(t.id, t.state) for t in tareas] == [
            ("a0", "active"),  # la mas antigua primero
            ("a1", "active"),
            ("r1", "reserved"),
            ("s1", "scheduled"),
        ]
        assert {t.id: t.queue for t in tareas} == {
            "a0": "ai_inference",
            "a1": "webhooks",
            "r1": "documents",
            "s1": "bulk",
        }

    def test_nunca_expone_argumentos(self) -> None:
        """Pueden llevar texto de contactos o telefonos."""
        volcado = " ".join(t.model_dump_json() for t in construir_tareas(DATOS))

        assert "TEXTO PRIVADO" not in volcado
        assert "+57300" not in volcado
        assert "args" not in volcado
        assert "kwargs" not in volcado

    def test_sin_datos_no_hay_tareas(self) -> None:
        assert construir_tareas({}) == []


async def _inspeccion_falsa(
    monkeypatch: pytest.MonkeyPatch, resultado: Any = None, falla: bool = False
) -> None:
    async def preguntar() -> tuple[bool, Any]:
        return (False, {}) if falla else (True, resultado if resultado is not None else DATOS)

    monkeypatch.setattr(modulo, "_preguntar", preguntar)


class TestEndpoints:
    async def test_workers(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _inspeccion_falsa(monkeypatch)

        r = await authenticated_client_factory(role="super_admin").get(
            "/api/v1/platform/celery/workers"
        )

        assert r.status_code == 200
        assert r.json()["broker_ok"] is True
        assert len(r.json()["workers"]) == 2

    async def test_un_broker_caido_no_se_confunde_con_cero_workers(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _inspeccion_falsa(monkeypatch, falla=True)

        r = await authenticated_client_factory(role="super_admin").get(
            "/api/v1/platform/celery/workers"
        )

        assert r.json() == {"broker_ok": False, "workers": []}

    async def test_revocar_pasa_el_id_y_no_termina_la_tarea(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llamadas: list[tuple[Any, dict[str, Any]]] = []
        monkeypatch.setattr(
            modulo.celery_app.control, "revoke", lambda *a, **k: llamadas.append((a, k))
        )
        tarea = uuid.uuid4()

        r = await authenticated_client_factory(role="super_admin").post(
            f"/api/v1/platform/celery/tasks/{tarea}/revoke"
        )

        assert r.status_code == 200
        assert r.json() == {"revoked": str(tarea)}
        assert llamadas == [((str(tarea),), {"terminate": False})]

    async def test_un_id_que_no_es_uuid_es_422_y_no_revoca_nada(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llamadas: list[Any] = []
        monkeypatch.setattr(modulo.celery_app.control, "revoke", lambda *a, **k: llamadas.append(a))

        r = await authenticated_client_factory(role="super_admin").post(
            "/api/v1/platform/celery/tasks/*/revoke"
        )

        assert r.status_code == 422
        assert llamadas == []

    @pytest.mark.parametrize("rol", ["admin", "supervisor", "agent", "medical"])
    @pytest.mark.parametrize(
        ("metodo", "ruta"),
        [
            ("get", "/celery/workers"),
            ("get", "/celery/queues"),
            ("get", "/celery/tasks"),
            ("get", "/redis"),
            ("get", "/system"),
            ("post", f"/celery/tasks/{uuid.uuid4()}/revoke"),
        ],
    )
    async def test_solo_el_super_admin(
        self, authenticated_client_factory: Any, rol: str, metodo: str, ruta: str
    ) -> None:
        r = await getattr(authenticated_client_factory(role=rol), metodo)(f"/api/v1/platform{ruta}")

        assert r.status_code == 403

    async def test_un_componente_caido_no_tumba_el_estado_y_no_filtra_el_error(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def db_ok() -> None:
            return None

        async def redis_roto() -> None:
            raise ConnectionError("fallo conectando a redis://:CLAVESECRETA@10.0.0.5:6379")

        async def celery_sin_workers() -> str:
            raise RuntimeError("sin workers")

        monkeypatch.setattr(modulo, "_comprobar_db", db_ok)
        monkeypatch.setattr(modulo, "_comprobar_redis", redis_roto)
        monkeypatch.setattr(modulo, "_comprobar_celery", celery_sin_workers)

        r = await authenticated_client_factory(role="super_admin").get("/api/v1/platform/system")

        assert r.status_code == 200
        estado = {c["name"]: c for c in r.json()["components"]}
        assert estado["database"]["ok"] is True
        assert estado["redis"]["ok"] is False
        assert estado["redis"]["detail"] == "ConnectionError"
        assert "CLAVESECRETA" not in r.text
        assert estado["celery"]["detail"] == "RuntimeError"
        assert all(c["latency_ms"] is not None for c in estado.values())
