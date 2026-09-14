"""Flujo de documentos contra PostgreSQL real, con RLS activo.

Cubre lo que los tests unitarios no pueden: que el CRUD funcione contra el schema
de verdad y que un tenant no alcance los documentos de otro a traves de los
endpoints. Desde BUG-005, las politicas RLS las crea `002_rls_policies.py`, y el CI
levanta el schema con `alembic upgrade head`, asi que este aislamiento se verifica
sobre el mismo camino que corre en produccion.

Requiere base de datos: `pytest tests/ --run-db`.

Solo se sustituye Supabase Storage. La base, el middleware, RLS y los endpoints son
los reales.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.api.v1 import documents as documents_module
from app.core.database import AsyncSessionLocal, engine, tenant_session

pytestmark = pytest.mark.db

DOCS_URL = "/api/v1/documents"
TXT = ("faq.txt", b"contenido de prueba", "text/plain")

# pgvector acepta el literal de texto; la columna es Vector(1536).
EMBEDDING = "[" + ",".join(["0.1"] * 1536) + "]"


# ─── Fixtures ────────────────────────────────────────────────────────────────


async def _crear_cliente(client_id: uuid.UUID, slug: str) -> None:
    """Inserta un tenant commiteado."""
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active) "
                "VALUES (:id, :name, :slug, 'free', true)"
            ),
            {"id": str(client_id), "name": f"Tenant {slug}", "slug": slug},
        )


async def _borrar_cliente(client_id: uuid.UUID) -> None:
    """Limpia el tenant y todo lo que cuelga de el, en orden de FKs."""
    async with AsyncSessionLocal() as session, session.begin():
        for tabla in ("document_chunks", "documents"):
            await session.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :cid"),  # noqa: S608
                {"cid": str(client_id)},
            )
        await session.execute(text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)})


@pytest_asyncio.fixture
async def dos_tenants() -> AsyncGenerator[tuple[uuid.UUID, uuid.UUID], None]:
    """Crea dos tenants commiteados para poder probar el aislamiento.

    `engine.dispose()` antes de empezar: el engine es un singleton de modulo y
    pytest-asyncio abre un event loop por test, asi que una conexion del pool
    abierta en el loop de otro test revienta con "attached to a different loop".
    """
    await engine.dispose()

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    await _crear_cliente(tenant_a, f"docs-a-{tenant_a.hex[:8]}")
    await _crear_cliente(tenant_b, f"docs-b-{tenant_b.hex[:8]}")

    yield tenant_a, tenant_b

    await _borrar_cliente(tenant_a)
    await _borrar_cliente(tenant_b)


@pytest.fixture
def storage_falso(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Sustituye Supabase Storage; la base y los endpoints siguen siendo reales."""
    registro: dict[str, Any] = {"subidas": [], "borrados": []}

    async def _upload(object_path: str, content: bytes, content_type: str) -> str:
        registro["subidas"].append(object_path)
        return object_path

    async def _delete(object_path: str) -> bool:
        registro["borrados"].append(object_path)
        return True

    monkeypatch.setattr(documents_module, "upload_to_storage", _upload)
    monkeypatch.setattr(documents_module, "delete_from_storage", _delete)
    return registro


@pytest.fixture
def sin_celery(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Evita encolar de verdad; devuelve lo que se habria encolado."""
    from app.tasks import document_ingestion

    encolados: list[tuple[str, str]] = []

    class _Tarea:
        def delay(self, document_id: str, client_id: str) -> None:
            encolados.append((document_id, client_id))

    monkeypatch.setattr(document_ingestion, "ingest_document", _Tarea())
    return encolados


# ─── Helpers ─────────────────────────────────────────────────────────────────


async def _insertar_documento(
    client_id: uuid.UUID, titulo: str, status: str = "completed"
) -> uuid.UUID:
    """Crea un documento directamente en base, con contexto de tenant."""
    document_id = uuid.uuid4()
    async with tenant_session(client_id) as session:
        await session.execute(
            text(
                "INSERT INTO documents (id, client_id, title, file_url, file_type, "
                "file_size, status, chunk_count) "
                "VALUES (:id, :cid, :titulo, :url, 'txt', 10, :status, 0)"
            ),
            {
                "id": str(document_id),
                "cid": str(client_id),
                "titulo": titulo,
                "url": f"{client_id}/{document_id}/faq.txt",
                "status": status,
            },
        )
    return document_id


async def _insertar_chunks(client_id: uuid.UUID, document_id: uuid.UUID, cantidad: int) -> None:
    """Crea chunks con embeddings reales para el documento indicado."""
    async with tenant_session(client_id) as session:
        for i in range(cantidad):
            await session.execute(
                text(
                    "INSERT INTO document_chunks "
                    "(client_id, document_id, chunk_index, content, embedding) "
                    "VALUES (:cid, :did, :idx, :contenido, CAST(:emb AS vector))"
                ),
                {
                    "cid": str(client_id),
                    "did": str(document_id),
                    "idx": i,
                    "contenido": f"chunk {i}",
                    "emb": EMBEDDING,
                },
            )


async def _contar(client_id: uuid.UUID, tabla: str) -> int:
    """Cuenta filas del tenant, con contexto aplicado."""
    async with tenant_session(client_id) as session:
        result = await session.execute(
            text(f"SELECT count(*) FROM {tabla} WHERE client_id = :cid"),  # noqa: S608
            {"cid": str(client_id)},
        )
        return int(result.scalar_one())


# ─── Subida ──────────────────────────────────────────────────────────────────


async def test_subida_persiste_el_documento_en_pending(
    dos_tenants: tuple[uuid.UUID, uuid.UUID],
    authenticated_client_factory: Any,
    storage_falso: dict[str, Any],
    sin_celery: list[tuple[str, str]],
) -> None:
    """El POST crea la fila real y encola la ingesta con los ids correctos."""
    tenant_a, _ = dos_tenants
    client = authenticated_client_factory(role="admin", client_id=tenant_a)

    response = await client.post(DOCS_URL, files={"file": TXT})

    assert response.status_code == 201
    cuerpo = response.json()
    assert cuerpo["status"] == "pending"
    assert cuerpo["client_id"] == str(tenant_a)
    assert await _contar(tenant_a, "documents") == 1
    assert sin_celery == [(cuerpo["id"], str(tenant_a))]
    assert storage_falso["subidas"] == [f"{tenant_a}/{cuerpo['id']}/faq.txt"]


async def test_created_at_se_rellena_desde_la_base(
    dos_tenants: tuple[uuid.UUID, uuid.UUID],
    authenticated_client_factory: Any,
    storage_falso: dict[str, Any],
    sin_celery: list[tuple[str, str]],
) -> None:
    """created_at/updated_at son server_default: el endpoint debe refrescarlos."""
    tenant_a, _ = dos_tenants
    client = authenticated_client_factory(role="admin", client_id=tenant_a)

    response = await client.post(DOCS_URL, files={"file": TXT})

    assert response.json()["created_at"] is not None
    assert response.json()["updated_at"] is not None


# ─── Aislamiento entre tenants ───────────────────────────────────────────────


async def test_el_listado_solo_ve_los_documentos_propios(
    dos_tenants: tuple[uuid.UUID, uuid.UUID], authenticated_client_factory: Any
) -> None:
    """RLS: el listado de A no puede traer documentos de B."""
    tenant_a, tenant_b = dos_tenants
    await _insertar_documento(tenant_a, "Manual de A")
    await _insertar_documento(tenant_b, "Manual de B")
    await _insertar_documento(tenant_b, "Otro de B")

    client = authenticated_client_factory(role="admin", client_id=tenant_a)
    response = await client.get(DOCS_URL)

    cuerpo = response.json()
    assert cuerpo["total"] == 1
    assert [d["title"] for d in cuerpo["items"]] == ["Manual de A"]


async def test_detalle_de_documento_ajeno_da_404(
    dos_tenants: tuple[uuid.UUID, uuid.UUID], authenticated_client_factory: Any
) -> None:
    """Un documento de otro tenant tiene que ser indistinguible de uno inexistente."""
    tenant_a, tenant_b = dos_tenants
    documento_de_b = await _insertar_documento(tenant_b, "Confidencial de B")

    client = authenticated_client_factory(role="admin", client_id=tenant_a)
    response = await client.get(f"{DOCS_URL}/{documento_de_b}")

    assert response.status_code == 404


async def test_no_se_puede_borrar_un_documento_ajeno(
    dos_tenants: tuple[uuid.UUID, uuid.UUID],
    authenticated_client_factory: Any,
    storage_falso: dict[str, Any],
) -> None:
    """Ni se borra la fila, ni se toca el archivo del otro tenant."""
    tenant_a, tenant_b = dos_tenants
    documento_de_b = await _insertar_documento(tenant_b, "Confidencial de B")

    client = authenticated_client_factory(role="admin", client_id=tenant_a)
    response = await client.delete(f"{DOCS_URL}/{documento_de_b}")

    assert response.status_code == 404
    assert await _contar(tenant_b, "documents") == 1
    assert storage_falso["borrados"] == []


# ─── Borrado y reprocesado ───────────────────────────────────────────────────


async def test_borrado_elimina_documento_y_chunks(
    dos_tenants: tuple[uuid.UUID, uuid.UUID],
    authenticated_client_factory: Any,
    storage_falso: dict[str, Any],
) -> None:
    """document_chunks no tiene ON DELETE CASCADE: sin borrarlos, la FK falla."""
    tenant_a, _ = dos_tenants
    documento = await _insertar_documento(tenant_a, "Manual")
    await _insertar_chunks(tenant_a, documento, 3)
    assert await _contar(tenant_a, "document_chunks") == 3

    client = authenticated_client_factory(role="admin", client_id=tenant_a)
    response = await client.delete(f"{DOCS_URL}/{documento}")

    assert response.status_code == 200
    assert await _contar(tenant_a, "documents") == 0
    assert await _contar(tenant_a, "document_chunks") == 0
    assert len(storage_falso["borrados"]) == 1


async def test_detalle_recuenta_los_chunks_reales(
    dos_tenants: tuple[uuid.UUID, uuid.UUID], authenticated_client_factory: Any
) -> None:
    """chunk_count quedo en 0 en base; el detalle cuenta los chunks de verdad."""
    tenant_a, _ = dos_tenants
    documento = await _insertar_documento(tenant_a, "Manual")
    await _insertar_chunks(tenant_a, documento, 5)

    client = authenticated_client_factory(role="admin", client_id=tenant_a)
    response = await client.get(f"{DOCS_URL}/{documento}")

    assert response.json()["chunk_count"] == 5


async def test_reprocess_limpia_chunks_y_reencola(
    dos_tenants: tuple[uuid.UUID, uuid.UUID],
    authenticated_client_factory: Any,
    sin_celery: list[tuple[str, str]],
) -> None:
    """Los chunks viejos se van para no mezclar embeddings de dos modelos."""
    tenant_a, _ = dos_tenants
    documento = await _insertar_documento(tenant_a, "Manual", status="failed")
    await _insertar_chunks(tenant_a, documento, 4)

    client = authenticated_client_factory(role="admin", client_id=tenant_a)
    response = await client.post(f"{DOCS_URL}/{documento}/reprocess")

    assert response.status_code == 202
    assert await _contar(tenant_a, "document_chunks") == 0
    assert sin_celery == [(str(documento), str(tenant_a))]

    async with tenant_session(tenant_a) as session:
        status = await session.scalar(
            text("SELECT status FROM documents WHERE id = :id"), {"id": str(documento)}
        )
    assert status == "pending"


# ─── Filtro por status ───────────────────────────────────────────────────────


async def test_filtro_por_status(
    dos_tenants: tuple[uuid.UUID, uuid.UUID], authenticated_client_factory: Any
) -> None:
    """El filtro se aplica en base, no en memoria."""
    tenant_a, _ = dos_tenants
    await _insertar_documento(tenant_a, "Listo", status="completed")
    await _insertar_documento(tenant_a, "Fallido", status="failed")
    await _insertar_documento(tenant_a, "Otro fallido", status="failed")

    client = authenticated_client_factory(role="admin", client_id=tenant_a)
    response = await client.get(DOCS_URL, params={"status": "failed"})

    cuerpo = response.json()
    assert cuerpo["total"] == 2
    assert {d["title"] for d in cuerpo["items"]} == {"Fallido", "Otro fallido"}


async def test_paginacion(
    dos_tenants: tuple[uuid.UUID, uuid.UUID], authenticated_client_factory: Any
) -> None:
    """El total refleja todas las coincidencias, no el tamano de la pagina."""
    tenant_a, _ = dos_tenants
    for i in range(5):
        await _insertar_documento(tenant_a, f"Documento {i}")

    client = authenticated_client_factory(role="admin", client_id=tenant_a)
    response = await client.get(DOCS_URL, params={"page": 1, "page_size": 2})

    cuerpo = response.json()
    assert cuerpo["total"] == 5
    assert len(cuerpo["items"]) == 2


# ─── Worker de ingesta contra base real ──────────────────────────────────────


async def test_mark_document_failed_persiste_el_motivo(
    dos_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """El motivo del fallo tiene que quedar visible en el documento."""
    from app.tasks.document_ingestion import _mark_document_failed

    tenant_a, _ = dos_tenants
    documento = await _insertar_documento(tenant_a, "Manual", status="processing")

    await _mark_document_failed(documento, tenant_a, "OCR reviento")

    async with tenant_session(tenant_a) as session:
        fila = (
            await session.execute(
                text("SELECT status, metadata FROM documents WHERE id = :id"),
                {"id": str(documento)},
            )
        ).one()

    assert fila.status == "failed"
    assert fila.metadata["error"] == "OCR reviento"
