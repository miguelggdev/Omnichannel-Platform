"""Las busquedas por id llevan `client_id` explicito en el WHERE, no solo RLS.

`session.get(Modelo, id)` no deja ver ningun filtro: si la politica RLS fallara
en esa tabla, devolveria la fila de otro tenant sin que nada avisara
(`documents.py::_get_document_or_404` ya lo documenta). Estos tests fijan que
los sitios que antes usaban `session.get()` compilan un `select()` con
`client_id` en el WHERE.

Los de contactos viven en `test_webhook_processor.py` y `test_contact_unifier.py`;
los de conversacion y cita, en `test_respond_handoff.py` y `test_calendar_tools.py`
(su `FakeSession` ni siquiera tiene `get()`).
"""

import uuid
from typing import Any

import pytest

from app.services import document_pipeline as pipeline_module
from app.tasks import document_ingestion as ingestion_module
from tests.unit.agent_doubles import FakeSession, parchear_tenant_session


class _Documento:
    """Documento minimo, mutable como el real."""

    def __init__(self) -> None:
        self.status = "pending"
        self.title = "doc"
        self.file_url = "ruta/x.txt"
        self.file_type = "txt"
        self.metadata_: dict[str, Any] = {}


def _params(sentencia: Any) -> dict[str, Any]:
    """Parametros compilados de una sentencia.

    Args:
        sentencia: Sentencia de SQLAlchemy.

    Returns:
        Diccionario nombre -> valor.
    """
    return dict(sentencia.compile().params)


def _filtra_por_tenant(sentencia: Any, client_id: uuid.UUID) -> bool:
    """Indica si el WHERE lleva `documents.client_id = <client_id>`.

    Args:
        sentencia: Sentencia compilada de un `select()`.
        client_id: Tenant esperado.

    Returns:
        True si el filtro esta y con ese valor.
    """
    return "documents.client_id =" in str(sentencia) and client_id in _params(sentencia).values()


class TestDocumentosFiltranPorTenant:
    """Pipeline de ingesta y worker de Celery."""

    async def test_marcar_procesando(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`_marcar_procesando` busca el documento con `client_id` explicito."""
        client_id = uuid.uuid4()
        sesion = FakeSession(resultados=[_Documento()])
        parchear_tenant_session(monkeypatch, pipeline_module, sesion)
        pipeline = object.__new__(pipeline_module.DocumentPipeline)

        await pipeline._marcar_procesando(uuid.uuid4(), client_id)

        assert _filtra_por_tenant(sesion.executed[0], client_id)

    async def test_guardar_resultado(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`_guardar_resultado` busca el documento con `client_id` explicito."""
        client_id = uuid.uuid4()
        sesion = FakeSession(resultados=[_Documento()])
        parchear_tenant_session(monkeypatch, pipeline_module, sesion)
        pipeline = object.__new__(pipeline_module.DocumentPipeline)

        await pipeline._guardar_resultado(uuid.uuid4(), client_id, "doc", [], [])

        assert _filtra_por_tenant(sesion.executed[0], client_id)

    async def test_documento_de_otro_tenant_no_se_procesa(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si el filtro no devuelve fila, el pipeline se detiene: no toca nada."""
        sesion = FakeSession(resultados=[None])
        parchear_tenant_session(monkeypatch, pipeline_module, sesion)
        pipeline = object.__new__(pipeline_module.DocumentPipeline)

        with pytest.raises(ValueError, match="no encontrado"):
            await pipeline._marcar_procesando(uuid.uuid4(), uuid.uuid4())

    async def test_marcar_fallido(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`_mark_document_failed` busca el documento con `client_id` explicito."""
        client_id = uuid.uuid4()
        documento = _Documento()
        sesion = FakeSession(resultados=[documento])
        parchear_tenant_session(monkeypatch, ingestion_module, sesion)

        await ingestion_module._mark_document_failed(uuid.uuid4(), client_id, "fallo")

        assert _filtra_por_tenant(sesion.executed[0], client_id)
        assert documento.status == "failed"
