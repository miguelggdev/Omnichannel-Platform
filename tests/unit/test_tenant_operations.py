"""Tests unitarios de la task `bulk_clone_tenant_from_template` (Sprint 10).

`TenantCloner` se mockea entero: lo que se prueba aca es la orquestacion (el
orden de los pasos, que estado deja en `template_instantiations`, que un
fallo se marca como `failed` sin tumbar el proceso), no la logica de
clonacion en si (eso es `test_tenant_cloner.py`).
"""

import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-testing-only-minimum-32-chars")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-minimum-32-characters-long")

from app.models.tenant_template import InstantiationStatus
from app.tasks.celery_config import celery_app
from app.tasks.tenant_operations import _clone


def _instantiation(target_client_id: object | None = None):
    """Fila de `template_instantiations` tal como la lee la task."""
    return type(
        "I", (), {"id": uuid4(), "target_client_id": target_client_id, "status": "pending"}
    )()


class _SesionFalsa:
    """Sesion falsa para `AsyncSessionLocal`: registra cada UPDATE ejecutado.

    Distingue por la tabla del statement: los SELECT sobre
    `template_instantiations` devuelven la instanciacion, los de
    `tenant_templates` el template, y los UPDATE se registran en `updates`.
    """

    def __init__(self, template: object | None, instantiation: object | None = None) -> None:
        self._template = template
        self._instantiation = instantiation if instantiation is not None else _instantiation()
        self.updates: list[dict] = []

    async def __aenter__(self) -> "_SesionFalsa":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return

    @asynccontextmanager
    async def begin(self):
        yield

    async def execute(self, stmt: object = None, params: object = None):
        sql = str(stmt.compile(compile_kwargs={"literal_binds": False}))
        if "template_instantiations" in sql and "UPDATE" in sql.upper():
            self.updates.append(dict(stmt.compile().params))
            return None
        if "template_instantiations" in sql:
            return _ResultadoFila(self._instantiation)
        return _ResultadoFila(self._template)


class _ResultadoFila:
    def __init__(self, fila: object | None) -> None:
        self._fila = fila

    def scalar_one_or_none(self):
        return self._fila


def _session_local_falsa(sesion: _SesionFalsa):
    def _factory():
        return sesion

    return _factory


class TestClone:
    @pytest.mark.asyncio
    async def test_flujo_exitoso_marca_processing_y_completed(self) -> None:
        template = type("T", (), {"id": uuid4(), "config": {}})()
        sesion = _SesionFalsa(template)
        new_client_id = uuid4()

        with (
            patch("app.tasks.tenant_operations.AsyncSessionLocal", _session_local_falsa(sesion)),
            patch(
                "app.tasks.tenant_operations.TenantCloner.instantiate",
                AsyncMock(return_value=(new_client_id, "temp-pass-123")),
            ),
            patch(
                "app.tasks.tenant_operations.TenantCloner.clone_documents",
                AsyncMock(return_value=[uuid4(), uuid4()]),
            ),
            patch("app.tasks.tenant_operations.ingest_document") as mock_ingest,
        ):
            resultado = await _clone(
                str(template.id), str(uuid4()), "Nuevo Tenant", "admin@nuevo.com", None
            )

        assert resultado == {"status": "completed", "target_client_id": str(new_client_id)}
        estados = [u.get("status") for u in sesion.updates if u.get("status")]
        assert estados == [
            InstantiationStatus.PROCESSING.value,
            InstantiationStatus.COMPLETED.value,
        ]
        assert mock_ingest.delay.call_count == 2

    @pytest.mark.asyncio
    async def test_guarda_el_target_client_id_y_el_password_temporal_en_progress(self) -> None:
        template = type("T", (), {"id": uuid4(), "config": {}})()
        sesion = _SesionFalsa(template)
        new_client_id = uuid4()

        with (
            patch("app.tasks.tenant_operations.AsyncSessionLocal", _session_local_falsa(sesion)),
            patch(
                "app.tasks.tenant_operations.TenantCloner.instantiate",
                AsyncMock(return_value=(new_client_id, "temp-pass-123")),
            ),
            patch(
                "app.tasks.tenant_operations.TenantCloner.clone_documents",
                AsyncMock(return_value=[]),
            ),
            patch("app.tasks.tenant_operations.ingest_document"),
        ):
            await _clone(str(template.id), str(uuid4()), "Nuevo Tenant", "admin@nuevo.com", None)

        con_password = [u for u in sesion.updates if "progress" in u]
        assert con_password
        assert con_password[0]["progress"]["admin_temp_password"] == "temp-pass-123"
        con_target = [u for u in sesion.updates if u.get("target_client_id")]
        assert con_target[0]["target_client_id"] == new_client_id

    @pytest.mark.asyncio
    async def test_template_inexistente_marca_failed_sin_propagar_el_traceback(self) -> None:
        sesion = _SesionFalsa(None)

        with (
            patch("app.tasks.tenant_operations.AsyncSessionLocal", _session_local_falsa(sesion)),
            pytest.raises(ValueError, match="no encontrado"),
        ):
            await _clone(str(uuid4()), str(uuid4()), "Nuevo Tenant", "admin@nuevo.com", None)

        estados = [u.get("status") for u in sesion.updates if u.get("status")]
        assert estados == [InstantiationStatus.PROCESSING.value, InstantiationStatus.FAILED.value]
        con_error = [u for u in sesion.updates if "error_message" in u]
        assert "no encontrado" in con_error[0]["error_message"]

    @pytest.mark.asyncio
    async def test_fallo_durante_instantiate_marca_failed_y_relanza(self) -> None:
        template = type("T", (), {"id": uuid4(), "config": {}})()
        sesion = _SesionFalsa(template)

        with (
            patch("app.tasks.tenant_operations.AsyncSessionLocal", _session_local_falsa(sesion)),
            patch(
                "app.tasks.tenant_operations.TenantCloner.instantiate",
                AsyncMock(side_effect=RuntimeError("email duplicado")),
            ),
            pytest.raises(RuntimeError),
        ):
            await _clone(str(template.id), str(uuid4()), "Nuevo Tenant", "admin@nuevo.com", None)

        estados = [u.get("status") for u in sesion.updates if u.get("status")]
        assert estados[-1] == InstantiationStatus.FAILED.value
        con_error = [u for u in sesion.updates if "error_message" in u]
        assert con_error[-1]["error_message"] == "email duplicado"

    @pytest.mark.asyncio
    async def test_error_message_se_recorta(self) -> None:
        template = type("T", (), {"id": uuid4(), "config": {}})()
        sesion = _SesionFalsa(template)

        with (
            patch("app.tasks.tenant_operations.AsyncSessionLocal", _session_local_falsa(sesion)),
            patch(
                "app.tasks.tenant_operations.TenantCloner.instantiate",
                AsyncMock(side_effect=RuntimeError("x" * 1000)),
            ),
            pytest.raises(RuntimeError),
        ):
            await _clone(str(template.id), str(uuid4()), "Nuevo Tenant", "admin@nuevo.com", None)

        con_error = [u for u in sesion.updates if "error_message" in u]
        assert len(con_error[-1]["error_message"]) == 500

    @pytest.mark.asyncio
    async def test_una_reentrega_no_crea_un_segundo_tenant(self) -> None:
        """`acks_late`: si un intento anterior ya creo el tenant, no se vuelve a clonar."""
        template = type("T", (), {"id": uuid4(), "config": {}})()
        ya_creado = uuid4()
        sesion = _SesionFalsa(template, _instantiation(target_client_id=ya_creado))

        with (
            patch("app.tasks.tenant_operations.AsyncSessionLocal", _session_local_falsa(sesion)),
            patch(
                "app.tasks.tenant_operations.TenantCloner.instantiate", AsyncMock()
            ) as mock_instantiate,
            patch(
                "app.tasks.tenant_operations.TenantCloner.clone_documents", AsyncMock()
            ) as mock_clone_documents,
        ):
            resultado = await _clone(
                str(template.id), str(uuid4()), "Nuevo Tenant", "admin@nuevo.com", None
            )

        mock_instantiate.assert_not_awaited()
        mock_clone_documents.assert_not_awaited()
        assert resultado == {"status": "failed", "target_client_id": str(ya_creado)}
        estados = [u.get("status") for u in sesion.updates if u.get("status")]
        assert estados == [InstantiationStatus.FAILED.value]
        con_error = [u for u in sesion.updates if "error_message" in u]
        assert str(ya_creado) in con_error[-1]["error_message"]

    @pytest.mark.asyncio
    async def test_instanciacion_inexistente_no_clona_nada(self) -> None:
        template = type("T", (), {"id": uuid4(), "config": {}})()
        sesion = _SesionFalsa(template, instantiation=None)
        sesion._instantiation = None

        with (
            patch("app.tasks.tenant_operations.AsyncSessionLocal", _session_local_falsa(sesion)),
            patch(
                "app.tasks.tenant_operations.TenantCloner.instantiate", AsyncMock()
            ) as mock_instantiate,
            pytest.raises(ValueError, match="no encontrada"),
        ):
            await _clone(str(template.id), str(uuid4()), "Nuevo Tenant", "admin@nuevo.com", None)

        mock_instantiate.assert_not_awaited()
        assert sesion.updates == []

    def test_la_task_esta_registrada_en_la_cola_bulk(self) -> None:
        assert "app.tasks.bulk_clone_tenant_from_template" in celery_app.tasks
        tarea = celery_app.tasks["app.tasks.bulk_clone_tenant_from_template"]
        assert tarea.queue == "bulk"

    def test_el_modulo_esta_en_task_modules(self) -> None:
        """Sin esto, el worker de la cola bulk levanta sin conocer la tarea (BUG-014)."""
        from app.tasks.celery_app import TASK_MODULES

        assert "app.tasks.tenant_operations" in TASK_MODULES

    @pytest.mark.asyncio
    async def test_overrides_se_pasan_tal_cual_a_instantiate(self) -> None:
        template = type("T", (), {"id": uuid4(), "config": {}})()
        sesion = _SesionFalsa(template)
        overrides = {"agent_configs": {"general": {"model": "gpt-4o-mini"}}}

        with (
            patch("app.tasks.tenant_operations.AsyncSessionLocal", _session_local_falsa(sesion)),
            patch(
                "app.tasks.tenant_operations.TenantCloner.instantiate",
                AsyncMock(return_value=(uuid4(), "pw")),
            ) as mock_instantiate,
            patch(
                "app.tasks.tenant_operations.TenantCloner.clone_documents",
                AsyncMock(return_value=[]),
            ),
        ):
            await _clone(
                str(template.id), str(uuid4()), "Nuevo Tenant", "admin@nuevo.com", overrides
            )

        mock_instantiate.assert_awaited_once_with(
            template, "Nuevo Tenant", "admin@nuevo.com", overrides
        )
