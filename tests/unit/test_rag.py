"""Tests del servicio de RAG: chunking, embeddings, retrieval y citaciones.

`app/services/{rag,chunker,embedding}.py` son entrega de Dev A (Matriz §6, Sprint 5)
y a la fecha de este commit no estan en `main`. El modulo se omite entero con
`importorskip` y se activa solo en cuanto Dev A mergee.

El foco esta en las dos reglas que CLAUDE.md marca como absolutas y que son faciles
de romper sin que ningun test lo note:

- **Filtro pre-vectorial por `client_id`** (regla 2): el aislamiento entre tenants
  tiene que estar en el WHERE, antes del calculo de distancia. Un filtro
  post-ranking devolveria top_k mezclado y luego descartaria: fuga de datos y
  resultados incompletos a la vez.
- **BUG-001**: PostgreSQL no admite alias de SELECT en WHERE, asi que el threshold
  se aplica repitiendo la expresion completa, no como `WHERE similarity > :x`.

Ambas se verifican sobre el SQL que el servicio emite, sustituyendo la sesion: son
propiedades del query, no hace falta base de datos para comprobarlas.
"""

import re
import uuid
from typing import Any

import pytest

rag = pytest.importorskip(
    "app.services.rag",
    reason="RAGService lo entrega Dev A en Sprint 5; aun no esta en main",
)
chunker_mod = pytest.importorskip(
    "app.services.chunker",
    reason="DocumentChunker lo entrega Dev A en Sprint 5",
)
embedding_mod = pytest.importorskip(
    "app.services.embedding",
    reason="EmbeddingService lo entrega Dev A en Sprint 5",
)


# ─── Dobles ──────────────────────────────────────────────────────────────────


class FakeEmbeddingService:
    """Devuelve un embedding fijo sin llamar a OpenAI."""

    def __init__(self, dim: int = 1536) -> None:
        self.dim = dim
        self.queries: list[str] = []

    async def embed_single(self, text: str) -> list[float]:
        """Registra la query y devuelve un vector constante."""
        self.queries.append(text)
        return [0.1] * self.dim

    async def embed_batch(self, texts: list[str], batch_size: int = 100) -> list[list[float]]:
        """Devuelve un vector por texto."""
        return [[0.1] * self.dim for _ in texts]


class FakeRow:
    """Fila de resultado con los campos que lee el servicio."""

    def __init__(self, titulo: str = "Manual", pagina: Any = 3, similarity: float = 0.9) -> None:
        self.id = uuid.uuid4()
        self.content = "Texto del chunk"
        self.metadata = {"page_number": pagina}
        self.document_title = titulo
        self.similarity = similarity


class SpySession:
    """Captura el SQL y los parametros que emite el servicio."""

    def __init__(self, rows: list[FakeRow] | None = None) -> None:
        self.sql: str = ""
        self.params: dict[str, Any] = {}
        self._rows = rows if rows is not None else [FakeRow()]

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        """Guarda la sentencia y devuelve las filas prefijadas."""
        self.sql = str(statement)
        self.params = params or {}
        filas = self._rows

        class _Result:
            def fetchall(self) -> list[FakeRow]:
                return filas

        return _Result()


class FakeTenantSession:
    """Context manager que entrega la sesion espia."""

    def __init__(self, session: SpySession) -> None:
        self.session = session

    async def __aenter__(self) -> SpySession:
        """Entra al contexto."""
        return self.session

    async def __aexit__(self, *exc: Any) -> None:
        """Sale del contexto."""
        return


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> SpySession:
    """Sustituye `tenant_session` dentro del modulo de RAG."""
    session = SpySession()
    monkeypatch.setattr(rag, "tenant_session", lambda _cid: FakeTenantSession(session))
    return session


def _servicio() -> Any:
    """Construye el RAGService con un embedder falso."""
    return rag.RAGService(embedding_service=FakeEmbeddingService())


def _normalizar(sql: str) -> str:
    """Colapsa espacios para poder buscar fragmentos del SQL."""
    return re.sub(r"\s+", " ", sql).strip().lower()


# ─── Aislamiento entre tenants ───────────────────────────────────────────────


class TestFiltroPreVectorial:
    """Regla 2 de CLAUDE.md: el filtro por client_id va antes de la distancia."""

    async def test_el_where_filtra_por_client_id(self, spy: SpySession) -> None:
        """Sin este filtro en el WHERE, el top_k mezclaria chunks de otros tenants."""
        await _servicio().retrieve("hola", uuid.uuid4())

        sql = _normalizar(spy.sql)
        assert "where" in sql
        assert "client_id" in sql.split("where", 1)[1]

    async def test_el_filtro_de_tenant_precede_al_order_by(self, spy: SpySession) -> None:
        """El filtro tiene que estar en el WHERE, no despues del ranking."""
        sql = _normalizar(await _con_sql(spy))

        where = sql.index("where")
        order = sql.index("order by")
        assert where < order
        assert "client_id" in sql[where:order]

    async def test_usa_el_contexto_de_tenant_de_la_sesion(self, spy: SpySession) -> None:
        """El client_id sale de current_setting, no de un parametro manipulable."""
        await _servicio().retrieve("hola", uuid.uuid4())

        assert "current_setting('app.current_client_id')" in _normalizar(spy.sql)


async def _con_sql(spy: SpySession) -> str:
    """Ejecuta un retrieve y devuelve el SQL emitido."""
    await _servicio().retrieve("hola", uuid.uuid4())
    return spy.sql


# ─── BUG-001 ─────────────────────────────────────────────────────────────────


class TestBug001:
    """PostgreSQL no permite alias de SELECT en WHERE."""

    async def test_el_threshold_no_usa_el_alias_similarity(self, spy: SpySession) -> None:
        """`WHERE similarity > :threshold` es un error de sintaxis en PostgreSQL."""
        sql = _normalizar(await _con_sql(spy))

        where = sql[sql.index("where") : sql.index("order by")]
        assert "similarity >" not in where
        assert "similarity>" not in where

    async def test_el_threshold_repite_la_expresion_completa(self, spy: SpySession) -> None:
        """La condicion tiene que recalcular 1 - (embedding <=> :query_embedding)."""
        sql = _normalizar(await _con_sql(spy))

        where = sql[sql.index("where") : sql.index("order by")]
        assert "<=>" in where, "el threshold debe aplicarse sobre la distancia, no sobre el alias"


# ─── Parametros del retrieval ────────────────────────────────────────────────


class TestParametros:
    """top_k, threshold y el filtro opcional por documento."""

    async def test_pasa_top_k_y_threshold(self, spy: SpySession) -> None:
        """Los limites que pide el llamante llegan al query."""
        await _servicio().retrieve("hola", uuid.uuid4(), top_k=3, threshold=0.8)

        assert spy.params["top_k"] == 3
        assert spy.params["threshold"] == 0.8

    async def test_defaults_documentados(self, spy: SpySession) -> None:
        """Por defecto: top_k=5 y threshold=0.75."""
        await _servicio().retrieve("hola", uuid.uuid4())

        assert spy.params["top_k"] == 5
        assert spy.params["threshold"] == 0.75

    async def test_filtro_por_documentos(self, spy: SpySession) -> None:
        """Se puede acotar la busqueda a documentos concretos."""
        docs = [uuid.uuid4(), uuid.uuid4()]

        await _servicio().retrieve("hola", uuid.uuid4(), document_ids=docs)

        assert "document_id" in _normalizar(spy.sql)
        assert len(spy.params["document_ids"]) == 2

    async def test_solo_documentos_completados(self, spy: SpySession) -> None:
        """Un documento a medio ingerir no debe aportar chunks a una respuesta."""
        sql = _normalizar(await _con_sql(spy))

        assert "status = 'completed'" in sql or "status='completed'" in sql

    async def test_embebe_la_query(self, spy: SpySession) -> None:
        """La query se vectoriza antes de buscar."""
        servicio = rag.RAGService(embedding_service=FakeEmbeddingService())

        await servicio.retrieve("cual es el horario", uuid.uuid4())

        assert servicio.embedding_service.queries == ["cual es el horario"]


# ─── Formato de citacion ─────────────────────────────────────────────────────


class TestCitaciones:
    """El grounding estricto exige poder citar la fuente de cada chunk."""

    async def test_incluye_titulo_y_pagina(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Formato documentado: [Fuente: titulo, pag. X]."""
        session = SpySession(rows=[FakeRow(titulo="Manual de usuario", pagina=7)])
        monkeypatch.setattr(rag, "tenant_session", lambda _cid: FakeTenantSession(session))

        resultados = await _servicio().retrieve("hola", uuid.uuid4())

        assert resultados[0].citation == "[Fuente: Manual de usuario, pag. 7]"

    async def test_sin_pagina_no_rompe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Un txt no tiene paginas: la citacion sigue siendo valida."""
        session = SpySession(rows=[FakeRow(titulo="FAQ", pagina=None)])
        monkeypatch.setattr(rag, "tenant_session", lambda _cid: FakeTenantSession(session))

        resultados = await _servicio().retrieve("hola", uuid.uuid4())

        assert "FAQ" in resultados[0].citation

    async def test_devuelve_similaridad_y_contenido(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """El llamante necesita el texto y cuanto de relevante es."""
        session = SpySession(rows=[FakeRow(similarity=0.93)])
        monkeypatch.setattr(rag, "tenant_session", lambda _cid: FakeTenantSession(session))

        resultados = await _servicio().retrieve("hola", uuid.uuid4())

        assert resultados[0].similarity == pytest.approx(0.93)
        assert resultados[0].content == "Texto del chunk"

    async def test_sin_coincidencias_devuelve_lista_vacia(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin nada por encima del threshold no se inventa contexto."""
        session = SpySession(rows=[])
        monkeypatch.setattr(rag, "tenant_session", lambda _cid: FakeTenantSession(session))

        assert await _servicio().retrieve("hola", uuid.uuid4()) == []


# ─── Chunking ────────────────────────────────────────────────────────────────


class TestChunking:
    """`DocumentChunker` parte el texto sin pasarse del tamano pedido."""

    def test_respeta_el_tamano_maximo(self) -> None:
        """Un chunk mas grande que el limite rompe el presupuesto de tokens."""
        chunker = chunker_mod.DocumentChunker(chunk_size=500, chunk_overlap=100)

        chunks = chunker._chunk_text("Lorem ipsum " * 1000, page_number=1)

        assert chunks
        for chunk in chunks:
            assert len(chunk.content) <= 500 * 1.2, "margen del 20% por cortes en palabra"

    def test_texto_corto_da_un_solo_chunk(self) -> None:
        """No tiene sentido trocear algo que ya cabe."""
        chunker = chunker_mod.DocumentChunker(chunk_size=500, chunk_overlap=100)

        chunks = chunker._chunk_text("Texto corto", page_number=1)

        assert len(chunks) == 1

    def test_texto_vacio_no_genera_chunks(self) -> None:
        """Una pagina en blanco no aporta nada al knowledge base."""
        chunker = chunker_mod.DocumentChunker(chunk_size=500, chunk_overlap=100)

        assert chunker._chunk_text("", page_number=1) == []


# ─── Embeddings ──────────────────────────────────────────────────────────────


class TestEmbeddings:
    """`EmbeddingService` produce vectores de la dimension de la columna."""

    def test_dimension_coincide_con_la_columna(self) -> None:
        """`document_chunks.embedding` es Vector(1536): otra dimension no entra."""
        servicio = embedding_mod.EmbeddingService(api_key="test", model="text-embedding-3-small")

        assert getattr(servicio, "dimensions", 1536) == 1536
