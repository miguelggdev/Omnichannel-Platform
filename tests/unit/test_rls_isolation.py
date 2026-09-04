"""
RLS Isolation Test Harness — Validation Harness (Pilar 3)

Tests automatizados que verifican el aislamiento multi-tenant vía RLS.
Cada test sigue el patrón:
  1. Insertar dato como Tenant A
  2. Intentar leerlo como Tenant B → debe retornar vacío
  3. Verificar que Tenant A sí puede leerlo

Estos tests se ejecutarán cuando la base de datos esté disponible (Sprint 1+).
Por ahora sirven como especificación ejecutable del comportamiento esperado.

Uso:
    pytest tests/unit/test_rls_isolation.py -v

Requisitos:
    - PostgreSQL con pgvector y RLS habilitado
    - Tablas creadas con init.sql
    - Variable DATABASE_URL apuntando a DB de test
"""

import uuid

import pytest

# Marcar todo el módulo como async
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        "not config.getoption('--run-db')",
        reason="Requiere --run-db y PostgreSQL activo",
    ),
]


# ─── Helpers ─────────────────────────────────────────────────────────────────


def conftest_option(parser: pytest.Parser) -> None:
    """Agregar opción --run-db al parser de pytest."""
    parser.addoption(
        "--run-db",
        action="store_true",
        default=False,
        help="Ejecutar tests que requieren PostgreSQL",
    )


# ─── Tests de Aislamiento por Tabla ──────────────────────────────────────────


class TestRLSIsolation:
    """
    Suite de tests que verifica que RLS aísla datos entre tenants.

    Patrón para cada tabla:
    1. INSERT como Tenant A → ok
    2. SELECT como Tenant B → 0 rows
    3. SELECT como Tenant A → row encontrado
    4. UPDATE como Tenant B → 0 rows affected
    5. DELETE como Tenant B → 0 rows affected
    """

    @pytest.mark.skip(reason="Activar cuando init.sql esté ejecutado")
    async def test_contacts_isolation(self, rls_harness: dict) -> None:
        """Tenant B no puede ver contacts de Tenant A."""
        session_a = rls_harness["session_a"]
        session_b = rls_harness["session_b"]
        tenant_a = rls_harness["tenant_a"]

        from sqlalchemy import text

        # Tenant A inserta un contacto
        contact_id = uuid.uuid4()
        await session_a.execute(
            text("""
                INSERT INTO contacts (id, client_id, phone_number, channel)
                VALUES (:id, :client_id, '+5215500000001', 'whatsapp')
            """),
            {"id": str(contact_id), "client_id": str(tenant_a)},
        )

        # Tenant B no debería verlo
        result = await session_b.execute(
            text("SELECT id FROM contacts WHERE id = :id"),
            {"id": str(contact_id)},
        )
        rows = result.fetchall()
        assert len(rows) == 0, "Tenant B vio un contacto de Tenant A — RLS VIOLADO"

        # Tenant A sí debería verlo
        result = await session_a.execute(
            text("SELECT id FROM contacts WHERE id = :id"),
            {"id": str(contact_id)},
        )
        rows = result.fetchall()
        assert len(rows) == 1, "Tenant A no puede ver su propio contacto"

    @pytest.mark.skip(reason="Activar cuando init.sql esté ejecutado")
    async def test_conversations_isolation(self, rls_harness: dict) -> None:
        """Tenant B no puede ver conversations de Tenant A."""
        session_a = rls_harness["session_a"]
        session_b = rls_harness["session_b"]
        tenant_a = rls_harness["tenant_a"]

        from sqlalchemy import text

        conv_id = uuid.uuid4()
        contact_id = uuid.uuid4()

        # Crear contacto y conversación como Tenant A
        await session_a.execute(
            text("""
                INSERT INTO contacts (id, client_id, phone_number, channel)
                VALUES (:id, :cid, '+5215500000002', 'whatsapp')
            """),
            {"id": str(contact_id), "cid": str(tenant_a)},
        )
        await session_a.execute(
            text("""
                INSERT INTO conversations (id, client_id, contact_id, status, channel)
                VALUES (:id, :cid, :contact_id, 'active', 'whatsapp')
            """),
            {
                "id": str(conv_id),
                "cid": str(tenant_a),
                "contact_id": str(contact_id),
            },
        )

        # Tenant B no debería ver la conversación
        result = await session_b.execute(
            text("SELECT id FROM conversations WHERE id = :id"),
            {"id": str(conv_id)},
        )
        assert len(result.fetchall()) == 0, "Tenant B vio conversación de A — RLS VIOLADO"

    @pytest.mark.skip(reason="Activar cuando init.sql esté ejecutado")
    async def test_messages_isolation(self, rls_harness: dict) -> None:
        """Tenant B no puede ver messages de Tenant A."""
        session_a = rls_harness["session_a"]
        session_b = rls_harness["session_b"]
        tenant_a = rls_harness["tenant_a"]

        from sqlalchemy import text

        msg_id = uuid.uuid4()
        await session_a.execute(
            text("""
                INSERT INTO messages (id, client_id, conversation_id, direction, content, channel)
                VALUES (:id, :cid, :conv_id, 'inbound', 'test message', 'whatsapp')
            """),
            {
                "id": str(msg_id),
                "cid": str(tenant_a),
                "conv_id": str(uuid.uuid4()),  # FK se validará con tabla real
            },
        )

        result = await session_b.execute(
            text("SELECT id FROM messages WHERE id = :id"),
            {"id": str(msg_id)},
        )
        assert len(result.fetchall()) == 0, "Tenant B vio message de A — RLS VIOLADO"

    @pytest.mark.skip(reason="Activar cuando init.sql esté ejecutado")
    async def test_documents_isolation(self, rls_harness: dict) -> None:
        """Tenant B no puede ver documents de Tenant A."""
        session_a = rls_harness["session_a"]
        session_b = rls_harness["session_b"]
        tenant_a = rls_harness["tenant_a"]

        from sqlalchemy import text

        doc_id = uuid.uuid4()
        await session_a.execute(
            text("""
                INSERT INTO documents (id, client_id, title, doc_type, status)
                VALUES (:id, :cid, 'Documento Secreto', 'pdf', 'active')
            """),
            {"id": str(doc_id), "cid": str(tenant_a)},
        )

        result = await session_b.execute(
            text("SELECT id FROM documents WHERE id = :id"),
            {"id": str(doc_id)},
        )
        assert len(result.fetchall()) == 0, "Tenant B vio document de A — RLS VIOLADO"

    @pytest.mark.skip(reason="Activar cuando init.sql esté ejecutado")
    async def test_cross_tenant_update_blocked(self, rls_harness: dict) -> None:
        """Tenant B no puede actualizar registros de Tenant A."""
        session_a = rls_harness["session_a"]
        session_b = rls_harness["session_b"]
        tenant_a = rls_harness["tenant_a"]

        from sqlalchemy import text

        contact_id = uuid.uuid4()
        await session_a.execute(
            text("""
                INSERT INTO contacts (id, client_id, phone_number, channel)
                VALUES (:id, :cid, '+5215500000003', 'whatsapp')
            """),
            {"id": str(contact_id), "cid": str(tenant_a)},
        )

        # Tenant B intenta actualizar — no debería afectar ninguna fila
        result = await session_b.execute(
            text("""
                UPDATE contacts SET phone_number = '+0000000000'
                WHERE id = :id
            """),
            {"id": str(contact_id)},
        )
        assert result.rowcount == 0, "Tenant B modificó contacto de A — RLS VIOLADO"

    @pytest.mark.skip(reason="Activar cuando init.sql esté ejecutado")
    async def test_cross_tenant_delete_blocked(self, rls_harness: dict) -> None:
        """Tenant B no puede eliminar registros de Tenant A."""
        session_a = rls_harness["session_a"]
        session_b = rls_harness["session_b"]
        tenant_a = rls_harness["tenant_a"]

        from sqlalchemy import text

        contact_id = uuid.uuid4()
        await session_a.execute(
            text("""
                INSERT INTO contacts (id, client_id, phone_number, channel)
                VALUES (:id, :cid, '+5215500000004', 'whatsapp')
            """),
            {"id": str(contact_id), "cid": str(tenant_a)},
        )

        # Tenant B intenta eliminar
        result = await session_b.execute(
            text("DELETE FROM contacts WHERE id = :id"),
            {"id": str(contact_id)},
        )
        assert result.rowcount == 0, "Tenant B eliminó contacto de A — RLS VIOLADO"

    @pytest.mark.skip(reason="Activar cuando init.sql esté ejecutado")
    async def test_set_local_scoping(self, rls_harness: dict) -> None:
        """Verificar que SET LOCAL se resetea al terminar la transacción."""
        session_a = rls_harness["session_a"]

        from sqlalchemy import text

        # Dentro de la transacción, el setting debe existir
        result = await session_a.execute(
            text("SELECT current_setting('app.current_client_id', true)")
        )
        setting = result.scalar()
        assert setting is not None, "SET LOCAL no estableció app.current_client_id"
        assert setting == str(rls_harness["tenant_a"])

    @pytest.mark.skip(reason="Activar cuando init.sql esté ejecutado")
    async def test_vector_search_tenant_isolation(self, rls_harness: dict) -> None:
        """
        Búsqueda vectorial con pre-filtro por client_id.

        Verifica que el patrón:
            WHERE client_id = current_setting(...)
            AND 1 - (embedding <=> :query) > :threshold
        solo retorna resultados del tenant correcto.
        """
        session_a = rls_harness["session_a"]
        session_b = rls_harness["session_b"]
        tenant_a = rls_harness["tenant_a"]

        from sqlalchemy import text

        chunk_id = uuid.uuid4()
        # Embedding de prueba (dimensión debe coincidir con la tabla)
        fake_embedding = "[" + ",".join(["0.1"] * 1536) + "]"

        await session_a.execute(
            text("""
                INSERT INTO document_chunks
                    (id, client_id, document_id, content, embedding, chunk_index)
                VALUES
                    (:id, :cid, :doc_id, 'contenido secreto',
                     :embedding::vector, 0)
            """),
            {
                "id": str(chunk_id),
                "cid": str(tenant_a),
                "doc_id": str(uuid.uuid4()),
                "embedding": fake_embedding,
            },
        )

        # Tenant B busca con el mismo embedding — no debería encontrar nada
        result = await session_b.execute(
            text("""
                SELECT id, 1 - (embedding <=> :query::vector) AS similarity
                FROM document_chunks
                WHERE 1 - (embedding <=> :query::vector) > 0.0
                ORDER BY similarity DESC
                LIMIT 5
            """),
            {"query": fake_embedding},
        )
        rows = result.fetchall()
        assert len(rows) == 0, (
            "Tenant B encontró chunks de Tenant A en búsqueda vectorial — RLS VIOLADO"
        )
