"""Tests del CRUD de documentos, del cliente de Storage y del worker de ingesta.

No se toca ni Supabase Storage ni la base de datos: `tenant_session` y las funciones
de storage se sustituyen por dobles, de modo que estos tests corren sin `--run-db`.
El recorrido con base real vive en `tests/integration/test_document_pipeline.py`.
"""

import uuid
from typing import Any

import httpx
import pytest

from app.api.v1 import documents as documents_module
from app.services import storage as storage_module
from app.tasks import document_ingestion as ingestion_module

UPLOAD_URL = "/api/v1/documents"
TXT = ("faq.txt", b"contenido de prueba", "text/plain")


# ─── Dobles ──────────────────────────────────────────────────────────────────


class FakeDocument:
    """Sustituto de Document con lo que leen los endpoints."""

    def __init__(self, **kwargs: Any) -> None:
        self.id = kwargs.get("id") or uuid.uuid4()
        self.client_id = kwargs.get("client_id") or uuid.uuid4()
        self.title = kwargs.get("title", "doc")
        self.file_url = kwargs.get("file_url", "ruta/objeto.txt")
        self.file_type = kwargs.get("file_type", "txt")
        self.file_size = kwargs.get("file_size", 10)
        self.chunk_count = kwargs.get("chunk_count", 0)
        self.status = kwargs.get("status", "pending")
        self.metadata_ = kwargs.get("metadata_", {})
        self.created_at = kwargs.get("created_at") or __import__("datetime").datetime(
            2026, 9, 14, tzinfo=__import__("datetime").timezone.utc
        )
        self.updated_at = kwargs.get("updated_at") or self.created_at


class FakeSession:
    """AsyncSession minima con resultados y documentos prefijados."""

    def __init__(
        self,
        doc: FakeDocument | None = None,
        scalars: list[Any] | None = None,
        rows: list[Any] | None = None,
    ) -> None:
        self.doc = doc
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.executed: list[Any] = []
        self.flushes = 0
        self._scalars = list(scalars or [])
        self._rows = list(rows or [])

    async def get(self, model: Any, pk: Any) -> Any:
        """Devuelve el documento prefijado."""
        return self.doc

    async def scalar(self, *args: Any, **kwargs: Any) -> Any:
        """Consume el siguiente escalar programado (los counts)."""
        return self._scalars.pop(0) if self._scalars else 0

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        """Registra la sentencia y devuelve las filas programadas."""
        self.executed.append(args[0] if args else None)
        filas = self._rows.pop(0) if self._rows else []

        class _Result:
            def scalars(self) -> Any:
                return self

            def all(self) -> list[Any]:
                return filas

        return _Result()

    def add(self, obj: Any) -> None:
        """Registra el objeto agregado."""
        self.added.append(obj)

    async def delete(self, obj: Any) -> None:
        """Registra el objeto borrado."""
        self.deleted.append(obj)

    async def flush(self) -> None:
        """Cuenta los flush."""
        self.flushes += 1

    async def refresh(self, obj: Any) -> None:
        """Emula los server_default que PostgreSQL rellena en el INSERT."""
        import datetime as _dt

        ahora = _dt.datetime(2026, 9, 14, tzinfo=_dt.timezone.utc)
        for campo in ("created_at", "updated_at"):
            if getattr(obj, campo, None) is None:
                setattr(obj, campo, ahora)
        if getattr(obj, "chunk_count", None) is None:
            obj.chunk_count = 0


class FakeTenantSession:
    """Context manager que devuelve siempre la misma FakeSession."""

    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> FakeSession:
        """Entra al contexto."""
        return self.session

    async def __aexit__(self, *exc: Any) -> None:
        """Sale del contexto."""
        return


class FakeIngestTask:
    """Captura las llamadas a delay() del worker de ingesta."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def delay(self, document_id: str, client_id: str) -> None:
        """Registra el encolado."""
        self.calls.append((document_id, client_id))


@pytest.fixture
def fake_ingest(monkeypatch: pytest.MonkeyPatch) -> FakeIngestTask:
    """Sustituye la tarea Celery de ingesta."""
    tarea = FakeIngestTask()
    monkeypatch.setattr(ingestion_module, "ingest_document", tarea)
    return tarea


@pytest.fixture
def fake_storage(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Sustituye las llamadas a Storage y registra lo que recibieron."""
    registro: dict[str, Any] = {"subidas": [], "borrados": [], "falla_subida": False}

    async def _upload(object_path: str, content: bytes, content_type: str) -> str:
        if registro["falla_subida"]:
            raise storage_module.StorageError("storage caido")
        registro["subidas"].append((object_path, len(content), content_type))
        return object_path

    async def _delete(object_path: str) -> bool:
        registro["borrados"].append(object_path)
        return True

    monkeypatch.setattr(documents_module, "upload_to_storage", _upload)
    monkeypatch.setattr(documents_module, "delete_from_storage", _delete)
    return registro


def _use_session(monkeypatch: pytest.MonkeyPatch, session: FakeSession) -> None:
    """Hace que los endpoints usen la sesion falsa indicada."""
    monkeypatch.setattr(documents_module, "tenant_session", lambda _cid: FakeTenantSession(session))


# ─── POST /documents ─────────────────────────────────────────────────────────


class TestUpload:
    """Subida de documentos."""

    async def test_archivo_valido_crea_documento_y_encola(
        self,
        authenticated_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        fake_storage: dict[str, Any],
        fake_ingest: FakeIngestTask,
    ) -> None:
        """Un txt valido responde 201, guarda en Storage y encola la ingesta."""
        session = FakeSession()
        _use_session(monkeypatch, session)

        response = await authenticated_client.post(UPLOAD_URL, files={"file": TXT})

        assert response.status_code == 201
        cuerpo = response.json()
        assert cuerpo["status"] == "pending"
        assert cuerpo["title"] == "faq.txt"
        assert cuerpo["file_type"] == "txt"
        assert len(session.added) == 1
        assert len(fake_storage["subidas"]) == 1
        assert len(fake_ingest.calls) == 1

    async def test_el_id_del_documento_coincide_con_la_ruta_en_storage(
        self,
        authenticated_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        fake_storage: dict[str, Any],
        fake_ingest: FakeIngestTask,
    ) -> None:
        """Archivo y fila comparten identificador, y el prefijo es el tenant."""
        _use_session(monkeypatch, FakeSession())

        response = await authenticated_client.post(UPLOAD_URL, files={"file": TXT})

        documento_id = response.json()["id"]
        tenant_id = response.json()["client_id"]
        ruta = fake_storage["subidas"][0][0]
        assert ruta == f"{tenant_id}/{documento_id}/faq.txt"

    async def test_encola_con_el_id_creado(
        self,
        authenticated_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        fake_storage: dict[str, Any],
        fake_ingest: FakeIngestTask,
    ) -> None:
        """El worker recibe el documento y el tenant correctos."""
        _use_session(monkeypatch, FakeSession())

        response = await authenticated_client.post(UPLOAD_URL, files={"file": TXT})

        assert fake_ingest.calls[0] == (response.json()["id"], response.json()["client_id"])

    async def test_titulo_personalizado(
        self,
        authenticated_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        fake_storage: dict[str, Any],
        fake_ingest: FakeIngestTask,
    ) -> None:
        """Si se envia title, gana sobre el nombre del archivo."""
        _use_session(monkeypatch, FakeSession())

        response = await authenticated_client.post(
            UPLOAD_URL, files={"file": TXT}, data={"title": "Preguntas frecuentes"}
        )

        assert response.json()["title"] == "Preguntas frecuentes"

    async def test_tipo_no_soportado_devuelve_400(
        self,
        authenticated_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        fake_storage: dict[str, Any],
        fake_ingest: FakeIngestTask,
    ) -> None:
        """Un ejecutable no entra al knowledge base."""
        _use_session(monkeypatch, FakeSession())

        response = await authenticated_client.post(
            UPLOAD_URL, files={"file": ("virus.exe", b"MZ", "application/x-msdownload")}
        )

        assert response.status_code == 400
        assert fake_storage["subidas"] == []
        assert fake_ingest.calls == []

    async def test_archivo_vacio_devuelve_400(
        self,
        authenticated_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        fake_storage: dict[str, Any],
        fake_ingest: FakeIngestTask,
    ) -> None:
        """Un archivo de 0 bytes no tiene nada que ingerir."""
        _use_session(monkeypatch, FakeSession())

        response = await authenticated_client.post(
            UPLOAD_URL, files={"file": ("vacio.txt", b"", "text/plain")}
        )

        assert response.status_code == 400

    async def test_archivo_demasiado_grande_devuelve_400(
        self,
        authenticated_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        fake_storage: dict[str, Any],
        fake_ingest: FakeIngestTask,
    ) -> None:
        """Por encima de 50 MB se rechaza antes de tocar Storage."""
        _use_session(monkeypatch, FakeSession())
        grande = b"x" * (documents_module.MAX_FILE_SIZE + 1)

        response = await authenticated_client.post(
            UPLOAD_URL, files={"file": ("grande.txt", grande, "text/plain")}
        )

        assert response.status_code == 400
        assert fake_storage["subidas"] == []

    async def test_storage_caido_devuelve_503_y_no_crea_fila(
        self,
        authenticated_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        fake_storage: dict[str, Any],
        fake_ingest: FakeIngestTask,
    ) -> None:
        """Sin archivo guardado no debe quedar un documento huerfano en base."""
        session = FakeSession()
        _use_session(monkeypatch, session)
        fake_storage["falla_subida"] = True

        response = await authenticated_client.post(UPLOAD_URL, files={"file": TXT})

        assert response.status_code == 503
        assert session.added == []
        assert fake_ingest.calls == []

    async def test_rol_agent_no_puede_subir(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        fake_storage: dict[str, Any],
        fake_ingest: FakeIngestTask,
    ) -> None:
        """Subir al knowledge base es de admin o supervisor, no de agent."""
        _use_session(monkeypatch, FakeSession())
        client = authenticated_client_factory(role="agent")

        response = await client.post(UPLOAD_URL, files={"file": TXT})

        assert response.status_code == 403
        assert fake_storage["subidas"] == []

    async def test_sin_token_devuelve_401(self, api_client: Any) -> None:
        """El router de documentos si pasa por TenantContextMiddleware."""
        response = await api_client.post(UPLOAD_URL, files={"file": TXT})

        assert response.status_code == 401


# ─── GET /documents ──────────────────────────────────────────────────────────


class TestListado:
    """Listado paginado."""

    async def test_devuelve_pagina_y_total(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El total viene del count, no del tamano de la pagina."""
        docs = [FakeDocument(title=f"doc-{i}") for i in range(3)]
        _use_session(monkeypatch, FakeSession(scalars=[7], rows=[docs]))

        response = await authenticated_client.get(UPLOAD_URL)

        cuerpo = response.json()
        assert response.status_code == 200
        assert cuerpo["total"] == 7
        assert len(cuerpo["items"]) == 3
        assert cuerpo["page"] == 1
        assert cuerpo["page_size"] == 20

    async def test_status_invalido_devuelve_400(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un status fuera del ciclo de vida es un error del cliente."""
        _use_session(monkeypatch, FakeSession(scalars=[0], rows=[[]]))

        response = await authenticated_client.get(UPLOAD_URL, params={"status": "inventado"})

        assert response.status_code == 400

    @pytest.mark.parametrize("status", ["pending", "processing", "completed", "failed"])
    async def test_acepta_los_status_del_ciclo_de_vida(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, status: str
    ) -> None:
        """Los cuatro estados reales del pipeline se aceptan."""
        _use_session(monkeypatch, FakeSession(scalars=[0], rows=[[]]))

        response = await authenticated_client.get(UPLOAD_URL, params={"status": status})

        assert response.status_code == 200

    async def test_page_size_fuera_de_rango_devuelve_422(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El tope de 100 lo valida FastAPI antes de llegar al endpoint."""
        _use_session(monkeypatch, FakeSession(scalars=[0], rows=[[]]))

        response = await authenticated_client.get(UPLOAD_URL, params={"page_size": 500})

        assert response.status_code == 422

    async def test_rol_agent_si_puede_listar(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Leer el knowledge base si es operacion de agent."""
        _use_session(monkeypatch, FakeSession(scalars=[0], rows=[[]]))
        client = authenticated_client_factory(role="agent")

        response = await client.get(UPLOAD_URL)

        assert response.status_code == 200


# ─── GET /documents/{id} ─────────────────────────────────────────────────────


class TestDetalle:
    """Detalle de un documento."""

    async def test_recuenta_los_chunks_reales(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El detalle no confia en chunk_count, que escribe el pipeline al final."""
        doc = FakeDocument(chunk_count=0, status="completed")
        _use_session(monkeypatch, FakeSession(doc=doc, scalars=[42]))

        response = await authenticated_client.get(f"{UPLOAD_URL}/{doc.id}")

        assert response.status_code == 200
        assert response.json()["chunk_count"] == 42

    async def test_documento_inexistente_devuelve_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un id de otro tenant se comporta igual que uno inexistente (RLS)."""
        _use_session(monkeypatch, FakeSession(doc=None))

        response = await authenticated_client.get(f"{UPLOAD_URL}/{uuid.uuid4()}")

        assert response.status_code == 404

    async def test_id_no_uuid_devuelve_422(self, authenticated_client: Any) -> None:
        """La ruta tipa el id como UUID."""
        response = await authenticated_client.get(f"{UPLOAD_URL}/no-es-uuid")

        assert response.status_code == 422


# ─── DELETE /documents/{id} ──────────────────────────────────────────────────


class TestBorrado:
    """Borrado de documentos."""

    async def test_borra_chunks_documento_y_archivo(
        self,
        authenticated_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        fake_storage: dict[str, Any],
    ) -> None:
        """Los chunks se borran explicitamente: no hay ON DELETE CASCADE."""
        doc = FakeDocument(file_url="tenant/doc/faq.txt")
        session = FakeSession(doc=doc)
        _use_session(monkeypatch, session)

        response = await authenticated_client.delete(f"{UPLOAD_URL}/{doc.id}")

        assert response.status_code == 200
        assert len(session.executed) == 1, "debe emitirse el DELETE de chunks"
        assert session.deleted == [doc]
        assert fake_storage["borrados"] == ["tenant/doc/faq.txt"]

    async def test_inexistente_devuelve_404(
        self,
        authenticated_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        fake_storage: dict[str, Any],
    ) -> None:
        """No se toca Storage si el documento no existe."""
        _use_session(monkeypatch, FakeSession(doc=None))

        response = await authenticated_client.delete(f"{UPLOAD_URL}/{uuid.uuid4()}")

        assert response.status_code == 404
        assert fake_storage["borrados"] == []

    async def test_rol_supervisor_no_puede_borrar(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        fake_storage: dict[str, Any],
    ) -> None:
        """Borrar del knowledge base es solo de admin."""
        doc = FakeDocument()
        _use_session(monkeypatch, FakeSession(doc=doc))
        client = authenticated_client_factory(role="supervisor")

        response = await client.delete(f"{UPLOAD_URL}/{doc.id}")

        assert response.status_code == 403


# ─── POST /documents/{id}/reprocess ──────────────────────────────────────────


class TestReprocesado:
    """Reencolado de la ingesta."""

    async def test_reencola_y_limpia_chunks(
        self,
        authenticated_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        fake_ingest: FakeIngestTask,
    ) -> None:
        """Los chunks viejos se van: no deben mezclarse embeddings de dos modelos."""
        doc = FakeDocument(status="failed", chunk_count=12)
        session = FakeSession(doc=doc)
        _use_session(monkeypatch, session)

        response = await authenticated_client.post(f"{UPLOAD_URL}/{doc.id}/reprocess")

        assert response.status_code == 202
        assert response.json()["status"] == "pending"
        assert doc.status == "pending"
        assert doc.chunk_count == 0
        assert len(session.executed) == 1
        assert len(fake_ingest.calls) == 1

    async def test_documento_en_proceso_devuelve_400(
        self,
        authenticated_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        fake_ingest: FakeIngestTask,
    ) -> None:
        """Reencolar algo que ya corre duplicaria el trabajo."""
        doc = FakeDocument(status="processing")
        _use_session(monkeypatch, FakeSession(doc=doc))

        response = await authenticated_client.post(f"{UPLOAD_URL}/{doc.id}/reprocess")

        assert response.status_code == 400
        assert fake_ingest.calls == []

    async def test_documento_sin_archivo_devuelve_400(
        self,
        authenticated_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        fake_ingest: FakeIngestTask,
    ) -> None:
        """Sin archivo en Storage no hay nada que reprocesar."""
        doc = FakeDocument(file_url=None, status="failed")
        _use_session(monkeypatch, FakeSession(doc=doc))

        response = await authenticated_client.post(f"{UPLOAD_URL}/{doc.id}/reprocess")

        assert response.status_code == 400
        assert fake_ingest.calls == []


# ─── Cliente de Storage ──────────────────────────────────────────────────────


class TestStorage:
    """`app/services/storage.py`."""

    @pytest.mark.parametrize(
        ("entrada", "esperado"),
        [
            ("factura.pdf", "factura.pdf"),
            # basename: de una ruta solo sobrevive el ultimo segmento.
            ("../../etc/passwd", "passwd"),
            ("..\\..\\windows\\system32", "system32"),
            ("carpeta/sub/archivo.txt", "archivo.txt"),
            ("nombre con espacios.txt", "nombre_con_espacios.txt"),
            ("", "archivo"),
            ("...", "archivo"),
        ],
    )
    def test_sanitize_filename(self, entrada: str, esperado: str) -> None:
        """El nombre lo elige el cliente: no puede salirse de su prefijo."""
        assert storage_module.sanitize_filename(entrada) == esperado

    def test_build_object_path_aisla_por_tenant(self) -> None:
        """El prefijo de la ruta es el client_id: es el aislamiento del bucket."""
        client_id = uuid.uuid4()
        document_id = uuid.uuid4()

        ruta = storage_module.build_object_path(client_id, document_id, "faq.txt")

        assert ruta == f"{client_id}/{document_id}/faq.txt"
        assert ruta.startswith(str(client_id))

    def test_endpoint_sin_supabase_url_falla_explicito(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin configuracion no se debe construir una URL a medias."""
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "SUPABASE_URL", "")

        with pytest.raises(storage_module.StorageError):
            storage_module._storage_endpoint("ruta/x.txt")

    def test_auth_headers_sin_secret_key_falla_explicito(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nunca se debe llamar a Storage sin credencial."""
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "SUPABASE_SECRET_KEY", "")

        with pytest.raises(storage_module.StorageError):
            storage_module._auth_headers()

    async def test_delete_no_levanta_si_falla(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Borrar un objeto ausente no debe tumbar el endpoint de borrado."""

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise httpx.ConnectError("sin red")

        monkeypatch.setattr(httpx, "AsyncClient", _boom)

        assert await storage_module.delete_from_storage("ruta/x.txt") is False

    def test_storage_metadata_es_serializable(self) -> None:
        """Va a JSONB: nada de tipos raros."""
        import json

        metadata = storage_module.storage_metadata("t/d/f.txt", "text/plain", 123)

        json.dumps(metadata)
        assert metadata["storage_path"] == "t/d/f.txt"
        assert metadata["size_bytes"] == 123


# ─── Worker de ingesta ───────────────────────────────────────────────────────


class TestWorkerDeIngesta:
    """`app/tasks/document_ingestion.py`."""

    cuerpo = staticmethod(ingestion_module.ingest_document.__wrapped__.__func__)

    def test_exito_devuelve_completed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un pipeline que termina bien marca el documento como procesado."""

        async def ok(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(ingestion_module, "_run_pipeline", ok)

        resultado = self.cuerpo(None, str(uuid.uuid4()), str(uuid.uuid4()))

        assert resultado == {"status": "completed"}

    def test_pipeline_ausente_marca_failed_sin_reintentar(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reintentar no va a hacer aparecer el modulo que falta."""

        async def sin_pipeline(*args: Any, **kwargs: Any) -> None:
            raise ingestion_module.PipelineUnavailableError("no esta")

        marcados: list[tuple[Any, ...]] = []

        async def marcar(document_id: Any, client_id: Any, error: str) -> None:
            marcados.append((document_id, client_id, error))

        monkeypatch.setattr(ingestion_module, "_run_pipeline", sin_pipeline)
        monkeypatch.setattr(ingestion_module, "_mark_document_failed", marcar)

        class TaskSelf:
            request = type("R", (), {"retries": 0})()
            max_retries = 2

            def retry(self, exc: Exception | None = None) -> Exception:
                raise AssertionError("no debe reintentarse")

        resultado = self.cuerpo(TaskSelf(), str(uuid.uuid4()), str(uuid.uuid4()))

        assert resultado == {"status": "failed"}
        assert len(marcados) == 1

    def test_timeout_marca_failed_sin_reintentar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Si no cupo en 9 minutos, otro intento tampoco va a caber."""
        from celery.exceptions import SoftTimeLimitExceeded

        async def lento(*args: Any, **kwargs: Any) -> None:
            raise SoftTimeLimitExceeded

        marcados: list[str] = []

        async def marcar(document_id: Any, client_id: Any, error: str) -> None:
            marcados.append(error)

        monkeypatch.setattr(ingestion_module, "_run_pipeline", lento)
        monkeypatch.setattr(ingestion_module, "_mark_document_failed", marcar)

        class TaskSelf:
            request = type("R", (), {"retries": 0})()
            max_retries = 2

            def retry(self, exc: Exception | None = None) -> Exception:
                raise AssertionError("no debe reintentarse")

        resultado = self.cuerpo(TaskSelf(), str(uuid.uuid4()), str(uuid.uuid4()))

        assert resultado == {"status": "timeout"}
        assert "Timeout" in marcados[0]

    def test_fallo_transitorio_reintenta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un corte de red hacia Storage o embeddings si merece reintento."""

        async def boom(*args: Any, **kwargs: Any) -> None:
            raise ConnectionError("sin red")

        monkeypatch.setattr(ingestion_module, "_run_pipeline", boom)

        class TaskSelf:
            def __init__(self) -> None:
                self.request = type("R", (), {"retries": 0})()
                self.max_retries = 2
                self.reintentos = 0

            def retry(self, exc: Exception | None = None) -> Exception:
                self.reintentos += 1
                return RuntimeError("retry")

        task_self = TaskSelf()
        with pytest.raises(RuntimeError):
            self.cuerpo(task_self, str(uuid.uuid4()), str(uuid.uuid4()))

        assert task_self.reintentos == 1

    def test_reintentos_agotados_marcan_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Agotados los intentos, el motivo queda visible en el documento."""

        async def boom(*args: Any, **kwargs: Any) -> None:
            raise ConnectionError("sin red")

        marcados: list[str] = []

        async def marcar(document_id: Any, client_id: Any, error: str) -> None:
            marcados.append(error)

        monkeypatch.setattr(ingestion_module, "_run_pipeline", boom)
        monkeypatch.setattr(ingestion_module, "_mark_document_failed", marcar)

        class TaskSelf:
            request = type("R", (), {"retries": 2})()
            max_retries = 2

            def retry(self, exc: Exception | None = None) -> Exception:
                raise AssertionError("ya no debe reintentarse")

        resultado = self.cuerpo(TaskSelf(), str(uuid.uuid4()), str(uuid.uuid4()))

        assert resultado == {"status": "failed"}
        assert "sin red" in marcados[0]

    def test_la_tarea_va_a_la_cola_documents(self) -> None:
        """El nombre y la cola tienen que casar con celery_config."""
        assert ingestion_module.ingest_document.name == "app.tasks.document_ingest"
        assert ingestion_module.ingest_document.queue == "documents"
