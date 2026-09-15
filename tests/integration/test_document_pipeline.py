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
    """Inserta un tenant commiteado.

    Va dentro de `tenant_session(client_id)` a proposito: la politica RLS de
    `clients` exige `id = current_setting('app.current_client_id')::uuid` tambien en
    el WITH CHECK, asi que el contexto tiene que ser el del tenant que se crea.
    """
    async with tenant_session(client_id) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active) "
                "VALUES (:id, :name, :slug, 'free', true)"
            ),
            {"id": str(client_id), "name": f"Tenant {slug}", "slug": slug},
        )


async def _borrar_cliente(client_id: uuid.UUID) -> None:
    """Limpia el tenant y todo lo que cuelga de el, en orden de FKs.

    Con contexto de tenant, igual que el alta: sin el, RLS no deja ver ni una fila
    que borrar.
    """
    async with tenant_session(client_id) as session:
        for tabla in ("document_chunks", "documents"):
            await session.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :cid"),  # noqa: S608
                {"cid": str(client_id)},
            )
        await session.execute(text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)})


@pytest_asyncio.fixture
async def motor_limpio() -> AsyncGenerator[None, None]:
    """Vacia el pool del engine antes del test.

    Mismo motivo que en `dos_tenants`: el engine es un singleton de modulo y
    pytest-asyncio abre un event loop por test, asi que reusar una conexion abierta
    en el loop de otro test revienta con "attached to a different loop".
    """
    await engine.dispose()
    yield


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


# ─── RAGService.retrieve() contra pgvector real ──────────────────────────────
#
# tests/unit/test_rag.py sustituye la sesion por un espia que solo captura el
# texto del SQL: nunca lo ejecuta contra Postgres. Eso dejo pasar un bug real:
# `:query_embedding` se bindeaba como texto plano, sin `::vector`, y `vector <=>
# text` no tiene cast implicito con el driver asyncpg (mismo root cause que el
# `::vector` que hubo que agregar en `_insertar_chunks` de este archivo y en
# los tests de RLS). Estos tests ejecutan `retrieve()` de verdad.


class _EmbeddingFalso:
    """Devuelve un vector fijo sin llamar a OpenAI."""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    async def embed_single(self, texto: str) -> list[float]:
        """Ignora el texto de la query; devuelve siempre el mismo vector."""
        return self._vector


def _vector_constante(valor: float) -> list[float]:
    """Vector de 1536 componentes iguales, coherente con `EMBEDDING` de este archivo."""
    return [valor] * 1536


async def test_retrieve_ejecuta_contra_pgvector_real(
    dos_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """El cast `::vector` es obligatorio.

    Sin el, `<=>` contra un bind sin tipar revienta con Postgres real — algo que
    un test con sesion mockeada, como los de `tests/unit/test_rag.py`, no puede ver.
    """
    from app.services.rag import RAGService

    tenant_a, _ = dos_tenants
    documento = await _insertar_documento(tenant_a, "Manual de RAG")
    await _insertar_chunks(tenant_a, documento, cantidad=1)

    rag_service = RAGService(embedding_service=_EmbeddingFalso(_vector_constante(0.1)))
    resultados = await rag_service.retrieve("cualquier pregunta", tenant_a, threshold=0.5)

    assert len(resultados) == 1
    assert resultados[0].similarity == pytest.approx(1.0, abs=1e-6)
    assert resultados[0].citation.startswith("[Fuente: Manual de RAG")


async def test_retrieve_aplica_el_threshold_con_pgvector_real(
    dos_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Un chunk lejano al embedding de la query queda fuera del threshold.

    Ejercita el cast en las tres posiciones donde aparece `:query_embedding`
    (SELECT, WHERE y ORDER BY): si alguna quedara sin castear, la query entera
    falla antes de llegar a filtrar nada.
    """
    from app.services.rag import RAGService

    tenant_a, _ = dos_tenants
    documento = await _insertar_documento(tenant_a, "Manual cercano")
    await _insertar_chunks(tenant_a, documento, cantidad=1)  # embedding [0.1]*1536

    lejano = await _insertar_documento(tenant_a, "Manual lejano")
    async with tenant_session(tenant_a) as session:
        await session.execute(
            text(
                "INSERT INTO document_chunks "
                "(client_id, document_id, chunk_index, content, embedding) "
                "VALUES (:cid, :did, 0, 'chunk lejano', CAST(:emb AS vector))"
            ),
            {
                "cid": str(tenant_a),
                "did": str(lejano),
                "emb": "[" + ",".join(["-0.9"] * 1536) + "]",
            },
        )

    rag_service = RAGService(embedding_service=_EmbeddingFalso(_vector_constante(0.1)))
    resultados = await rag_service.retrieve("cualquier pregunta", tenant_a, threshold=0.9)

    assert [r.citation for r in resultados] == ["[Fuente: Manual cercano, pag. ?]"]


async def test_retrieve_respeta_el_filtro_de_document_ids_con_pgvector_real(
    dos_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """`document_ids` acota la busqueda: tambien pasa por asyncpg sin cast explicito."""
    from app.services.rag import RAGService

    tenant_a, _ = dos_tenants
    incluido = await _insertar_documento(tenant_a, "Incluido")
    await _insertar_chunks(tenant_a, incluido, cantidad=1)
    excluido = await _insertar_documento(tenant_a, "Excluido")
    await _insertar_chunks(tenant_a, excluido, cantidad=1)

    rag_service = RAGService(embedding_service=_EmbeddingFalso(_vector_constante(0.1)))
    resultados = await rag_service.retrieve(
        "cualquier pregunta", tenant_a, threshold=0.5, document_ids=[incluido]
    )

    assert [r.citation for r in resultados] == ["[Fuente: Incluido, pag. ?]"]


async def test_retrieve_few_shot_examples_ejecuta_contra_pgvector_real(
    dos_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """`retrieve_few_shot_examples()` tiene el mismo bug de casteo, sobre `approved_responses`."""
    from app.services.rag import RAGService

    tenant_a, _ = dos_tenants
    async with tenant_session(tenant_a) as session:
        await session.execute(
            text(
                "INSERT INTO approved_responses (client_id, question, response, embedding) "
                "VALUES (:cid, :pregunta, :respuesta, CAST(:emb AS vector))"
            ),
            {
                "cid": str(tenant_a),
                "pregunta": "como reservo",
                "respuesta": "puedes reservar en la app",
                "emb": EMBEDDING,
            },
        )

    rag_service = RAGService(embedding_service=_EmbeddingFalso(_vector_constante(0.1)))
    ejemplos = await rag_service.retrieve_few_shot_examples("como reservo", tenant_a, threshold=0.5)

    assert ejemplos == [
        {
            "question": "como reservo",
            "answer": "puedes reservar en la app",
            "similarity": pytest.approx(1.0, abs=1e-6),
        }
    ]


async def test_retrieve_no_ve_chunks_de_otro_tenant_con_pgvector_real(
    dos_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Regla 2 de CLAUDE.md: el filtro pre-vectorial por client_id va antes que todo."""
    from app.services.rag import RAGService

    tenant_a, tenant_b = dos_tenants
    documento_a = await _insertar_documento(tenant_a, "Solo de A")
    await _insertar_chunks(tenant_a, documento_a, cantidad=1)

    rag_service = RAGService(embedding_service=_EmbeddingFalso(_vector_constante(0.1)))
    resultados = await rag_service.retrieve("cualquier pregunta", tenant_b, threshold=0.5)

    assert resultados == []


# ─── Guardia: que RLS este realmente activo ──────────────────────────────────
#
# NOTA-002 en MEMORY.md: durante varios sprints el CI daba verde en RLS sin
# ejercitarlo de verdad. Estos dos tests comprueban la premisa de la que dependen
# todos los tests de aislamiento del repo: que las politicas existan y que el rol
# con el que se conecta la app no las saltee. Si alguno falla, los ~30 tests de
# aislamiento de la suite estan pasando en vacio.


async def test_las_tablas_de_documentos_tienen_rls_activo(motor_limpio: None) -> None:
    """`documents` y `document_chunks` deben tener RLS con FORCE y su politica."""
    async with AsyncSessionLocal() as session:
        filas = (
            await session.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                    "       (SELECT count(*) FROM pg_policies p "
                    "        WHERE p.tablename = c.relname) AS politicas "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' "
                    "  AND c.relname IN ('documents', 'document_chunks')"
                )
            )
        ).all()

    assert len(filas) == 2, f"faltan tablas: {[f.relname for f in filas]}"
    for fila in filas:
        assert fila.relrowsecurity, f"{fila.relname} no tiene ROW LEVEL SECURITY activo"
        assert fila.relforcerowsecurity, (
            f"{fila.relname} no tiene FORCE: el dueno de la tabla se saltaria las politicas"
        )
        assert fila.politicas >= 1, f"{fila.relname} no tiene ninguna politica"


async def test_el_rol_de_la_app_no_saltea_rls(motor_limpio: None) -> None:
    """Un rol superusuario o con BYPASSRLS hace vacuos todos los tests de aislamiento.

    PostgreSQL ignora las politicas para superusuarios y para roles con BYPASSRLS,
    incluso con FORCE. Si el CI se conecta con uno de esos, el verde de RLS no
    significa nada.
    """
    async with AsyncSessionLocal() as session:
        fila = (
            await session.execute(
                text(
                    "SELECT current_user AS usuario, rolsuper, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()

    assert not fila.rolsuper, (
        f"el rol '{fila.usuario}' es superusuario y se saltea RLS: los tests de "
        f"aislamiento de toda la suite estan pasando en vacio"
    )
    assert not fila.rolbypassrls, f"el rol '{fila.usuario}' tiene BYPASSRLS"
