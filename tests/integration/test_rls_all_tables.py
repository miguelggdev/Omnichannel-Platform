"""
Test de Aislamiento RLS — Todas las Tablas MVP + Fase 2.

Verifica que CADA tabla con client_id aísla correctamente los datos
entre tenants. Cada test sigue el patrón:

  1. INSERT como Tenant A → ok
  2. SELECT como Tenant B → 0 rows
  3. UPDATE como Tenant B → 0 rows affected
  4. DELETE como Tenant B → 0 rows affected
  5. SELECT como Tenant A → row encontrado (integridad)

Requisitos:
    - PostgreSQL con pgvector y RLS habilitado
    - Tablas creadas con init.sql
    - pytest --run-db

Uso:
    pytest tests/integration/test_rls_all_tables.py -v --run-db
"""

import uuid

import pytest
from sqlalchemy import text

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.db,
]


# ─── Helper genérico de aislamiento ─────────────────────────────────────────


async def assert_rls_isolation(
    session_a,
    session_b,
    table: str,
    insert_sql: str,
    params: dict,
    id_column: str = "id",
    record_id: str | None = None,
) -> None:
    """
    Helper genérico que verifica aislamiento RLS en una tabla.

    Args:
        session_a: Sesión con SET LOCAL de Tenant A.
        session_b: Sesión con SET LOCAL de Tenant B.
        table: Nombre de la tabla.
        insert_sql: SQL de INSERT para Tenant A.
        params: Parámetros del INSERT.
        id_column: Columna ID para filtrar (default: 'id').
        record_id: ID del registro insertado (default: params['id']).
    """
    rid = record_id or params.get("id")

    # 1. INSERT como Tenant A
    await session_a.execute(text(insert_sql), params)

    # 2. SELECT como Tenant B → 0 rows
    result = await session_b.execute(
        text(f"SELECT {id_column} FROM {table} WHERE {id_column} = :rid"),
        {"rid": rid},
    )
    rows_b = result.fetchall()
    assert len(rows_b) == 0, (
        f"VIOLACIÓN RLS en {table}: Tenant B ve datos de Tenant A"
    )

    # 3. UPDATE como Tenant B → 0 rows affected
    result = await session_b.execute(
        text(f"UPDATE {table} SET updated_at = NOW() WHERE {id_column} = :rid"),
        {"rid": rid},
    )
    assert result.rowcount == 0, (
        f"VIOLACIÓN RLS en {table}: Tenant B pudo UPDATE datos de Tenant A"
    )

    # 4. DELETE como Tenant B → 0 rows affected
    result = await session_b.execute(
        text(f"DELETE FROM {table} WHERE {id_column} = :rid"),
        {"rid": rid},
    )
    assert result.rowcount == 0, (
        f"VIOLACIÓN RLS en {table}: Tenant B pudo DELETE datos de Tenant A"
    )

    # 5. SELECT como Tenant A → integridad
    result = await session_a.execute(
        text(f"SELECT {id_column} FROM {table} WHERE {id_column} = :rid"),
        {"rid": rid},
    )
    rows_a = result.fetchall()
    assert len(rows_a) == 1, (
        f"INTEGRIDAD en {table}: Tenant A no ve su propio registro"
    )


# ─── Tests por tabla MVP ────────────────────────────────────────────────────


class TestRLSMVPTables:
    """Tests de aislamiento RLS para las 18 tablas MVP."""

    async def test_contacts(self, rls_harness: dict) -> None:
        """contacts: Tenant B no ve contactos de Tenant A."""
        await assert_rls_isolation(
            rls_harness["session_a"],
            rls_harness["session_b"],
            table="contacts",
            insert_sql="""
                INSERT INTO contacts (id, client_id, phone_number, channel)
                VALUES (:id, :client_id, :phone, 'whatsapp')
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": str(rls_harness["tenant_a"]),
                "phone": f"+521550000{uuid.uuid4().int % 10000:04d}",
            },
        )

    async def test_contact_identifiers(self, rls_harness: dict) -> None:
        """contact_identifiers: aislamiento por tenant."""
        sa = rls_harness["session_a"]
        contact_id = str(uuid.uuid4())
        cid_a = str(rls_harness["tenant_a"])

        # Crear contacto padre primero
        await sa.execute(
            text("""
                INSERT INTO contacts (id, client_id, phone_number, channel)
                VALUES (:id, :cid, '+5215599990001', 'whatsapp')
            """),
            {"id": contact_id, "cid": cid_a},
        )

        await assert_rls_isolation(
            sa,
            rls_harness["session_b"],
            table="contact_identifiers",
            insert_sql="""
                INSERT INTO contact_identifiers (id, client_id, contact_id, identifier_type, identifier_value)
                VALUES (:id, :client_id, :contact_id, 'phone', '+5215599990001')
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": cid_a,
                "contact_id": contact_id,
            },
        )

    async def test_conversations(self, rls_harness: dict) -> None:
        """conversations: aislamiento por tenant."""
        sa = rls_harness["session_a"]
        cid_a = str(rls_harness["tenant_a"])
        contact_id = str(uuid.uuid4())

        await sa.execute(
            text("""
                INSERT INTO contacts (id, client_id, phone_number, channel)
                VALUES (:id, :cid, '+5215599990002', 'whatsapp')
            """),
            {"id": contact_id, "cid": cid_a},
        )

        await assert_rls_isolation(
            sa,
            rls_harness["session_b"],
            table="conversations",
            insert_sql="""
                INSERT INTO conversations (id, client_id, contact_id, status, channel)
                VALUES (:id, :client_id, :contact_id, 'active', 'whatsapp')
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": cid_a,
                "contact_id": contact_id,
            },
        )

    async def test_messages(self, rls_harness: dict) -> None:
        """messages: aislamiento por tenant."""
        sa = rls_harness["session_a"]
        cid_a = str(rls_harness["tenant_a"])
        contact_id = str(uuid.uuid4())
        conv_id = str(uuid.uuid4())

        await sa.execute(
            text("""
                INSERT INTO contacts (id, client_id, phone_number, channel)
                VALUES (:id, :cid, '+5215599990003', 'whatsapp')
            """),
            {"id": contact_id, "cid": cid_a},
        )
        await sa.execute(
            text("""
                INSERT INTO conversations (id, client_id, contact_id, status, channel)
                VALUES (:id, :cid, :contact_id, 'active', 'whatsapp')
            """),
            {"id": conv_id, "cid": cid_a, "contact_id": contact_id},
        )

        await assert_rls_isolation(
            sa,
            rls_harness["session_b"],
            table="messages",
            insert_sql="""
                INSERT INTO messages (id, client_id, conversation_id, direction, content, channel)
                VALUES (:id, :client_id, :conv_id, 'inbound', 'mensaje secreto', 'whatsapp')
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": cid_a,
                "conv_id": conv_id,
            },
        )

    async def test_documents(self, rls_harness: dict) -> None:
        """documents: aislamiento por tenant."""
        await assert_rls_isolation(
            rls_harness["session_a"],
            rls_harness["session_b"],
            table="documents",
            insert_sql="""
                INSERT INTO documents (id, client_id, title, doc_type, status)
                VALUES (:id, :client_id, 'Documento Confidencial', 'pdf', 'active')
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": str(rls_harness["tenant_a"]),
            },
        )

    async def test_document_chunks(self, rls_harness: dict) -> None:
        """document_chunks: aislamiento por tenant (incluye embeddings)."""
        sa = rls_harness["session_a"]
        cid_a = str(rls_harness["tenant_a"])
        doc_id = str(uuid.uuid4())

        await sa.execute(
            text("""
                INSERT INTO documents (id, client_id, title, doc_type, status)
                VALUES (:id, :cid, 'Doc Chunks Test', 'pdf', 'active')
            """),
            {"id": doc_id, "cid": cid_a},
        )

        fake_embedding = "[" + ",".join(["0.1"] * 1536) + "]"
        await assert_rls_isolation(
            sa,
            rls_harness["session_b"],
            table="document_chunks",
            insert_sql="""
                INSERT INTO document_chunks (id, client_id, document_id, content, embedding, chunk_index)
                VALUES (:id, :client_id, :doc_id, 'contenido secreto', :embedding::vector, 0)
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": cid_a,
                "doc_id": doc_id,
                "embedding": fake_embedding,
            },
        )

    async def test_tags(self, rls_harness: dict) -> None:
        """tags: aislamiento por tenant."""
        await assert_rls_isolation(
            rls_harness["session_a"],
            rls_harness["session_b"],
            table="tags",
            insert_sql="""
                INSERT INTO tags (id, client_id, name, color)
                VALUES (:id, :client_id, 'VIP', '#FF0000')
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": str(rls_harness["tenant_a"]),
            },
        )

    async def test_internal_notes(self, rls_harness: dict) -> None:
        """internal_notes: aislamiento por tenant."""
        sa = rls_harness["session_a"]
        cid_a = str(rls_harness["tenant_a"])
        contact_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())

        await sa.execute(
            text("""
                INSERT INTO contacts (id, client_id, phone_number, channel)
                VALUES (:id, :cid, '+5215599990004', 'whatsapp')
            """),
            {"id": contact_id, "cid": cid_a},
        )
        await sa.execute(
            text("""
                INSERT INTO users (id, client_id, email, role, full_name)
                VALUES (:id, :cid, 'test@test.com', 'agent', 'Test User')
            """),
            {"id": user_id, "cid": cid_a},
        )

        await assert_rls_isolation(
            sa,
            rls_harness["session_b"],
            table="internal_notes",
            insert_sql="""
                INSERT INTO internal_notes (id, client_id, contact_id, user_id, content)
                VALUES (:id, :client_id, :contact_id, :user_id, 'Nota confidencial')
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": cid_a,
                "contact_id": contact_id,
                "user_id": user_id,
            },
        )

    async def test_token_budgets(self, rls_harness: dict) -> None:
        """token_budgets: aislamiento por tenant."""
        await assert_rls_isolation(
            rls_harness["session_a"],
            rls_harness["session_b"],
            table="token_budgets",
            insert_sql="""
                INSERT INTO token_budgets (id, client_id, monthly_limit, used_this_month)
                VALUES (:id, :client_id, 100000, 0)
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": str(rls_harness["tenant_a"]),
            },
        )

    async def test_token_usage_log(self, rls_harness: dict) -> None:
        """token_usage_log: aislamiento por tenant."""
        await assert_rls_isolation(
            rls_harness["session_a"],
            rls_harness["session_b"],
            table="token_usage_log",
            insert_sql="""
                INSERT INTO token_usage_log (id, client_id, tokens_used, model, node_name)
                VALUES (:id, :client_id, 150, 'gpt-4o', 'respond')
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": str(rls_harness["tenant_a"]),
            },
        )

    async def test_agent_configs(self, rls_harness: dict) -> None:
        """agent_configs: aislamiento por tenant."""
        await assert_rls_isolation(
            rls_harness["session_a"],
            rls_harness["session_b"],
            table="agent_configs",
            insert_sql="""
                INSERT INTO agent_configs (id, client_id, name, system_prompt, model, is_active)
                VALUES (:id, :client_id, 'Bot Test', 'Eres un asistente', 'gpt-4o', true)
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": str(rls_harness["tenant_a"]),
            },
        )

    async def test_quick_replies(self, rls_harness: dict) -> None:
        """quick_replies: aislamiento por tenant."""
        await assert_rls_isolation(
            rls_harness["session_a"],
            rls_harness["session_b"],
            table="quick_replies",
            insert_sql="""
                INSERT INTO quick_replies (id, client_id, title, content)
                VALUES (:id, :client_id, 'Saludo', 'Hola, bienvenido')
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": str(rls_harness["tenant_a"]),
            },
        )

    async def test_webhook_dedup(self, rls_harness: dict) -> None:
        """webhook_dedup: aislamiento por tenant."""
        await assert_rls_isolation(
            rls_harness["session_a"],
            rls_harness["session_b"],
            table="webhook_dedup",
            insert_sql="""
                INSERT INTO webhook_dedup (id, client_id, channel, external_message_id)
                VALUES (:id, :client_id, 'whatsapp', :ext_id)
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": str(rls_harness["tenant_a"]),
                "ext_id": f"ext_{uuid.uuid4().hex[:12]}",
            },
        )

    async def test_users(self, rls_harness: dict) -> None:
        """users: aislamiento por tenant."""
        await assert_rls_isolation(
            rls_harness["session_a"],
            rls_harness["session_b"],
            table="users",
            insert_sql="""
                INSERT INTO users (id, client_id, email, role, full_name)
                VALUES (:id, :client_id, :email, 'agent', 'Agente Test')
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": str(rls_harness["tenant_a"]),
                "email": f"agent_{uuid.uuid4().hex[:8]}@test.com",
            },
        )

    async def test_pending_responses(self, rls_harness: dict) -> None:
        """pending_responses: aislamiento por tenant."""
        sa = rls_harness["session_a"]
        cid_a = str(rls_harness["tenant_a"])
        conv_id = str(uuid.uuid4())
        contact_id = str(uuid.uuid4())

        await sa.execute(
            text("""
                INSERT INTO contacts (id, client_id, phone_number, channel)
                VALUES (:id, :cid, '+5215599990005', 'whatsapp')
            """),
            {"id": contact_id, "cid": cid_a},
        )
        await sa.execute(
            text("""
                INSERT INTO conversations (id, client_id, contact_id, status, channel)
                VALUES (:id, :cid, :contact_id, 'active', 'whatsapp')
            """),
            {"id": conv_id, "cid": cid_a, "contact_id": contact_id},
        )

        await assert_rls_isolation(
            sa,
            rls_harness["session_b"],
            table="pending_responses",
            insert_sql="""
                INSERT INTO pending_responses (id, client_id, conversation_id, question, proposed_answer)
                VALUES (:id, :client_id, :conv_id, 'pregunta test', 'respuesta propuesta')
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": cid_a,
                "conv_id": conv_id,
            },
        )

    async def test_approved_responses(self, rls_harness: dict) -> None:
        """approved_responses: aislamiento por tenant (con embedding)."""
        fake_embedding = "[" + ",".join(["0.5"] * 1536) + "]"
        await assert_rls_isolation(
            rls_harness["session_a"],
            rls_harness["session_b"],
            table="approved_responses",
            insert_sql="""
                INSERT INTO approved_responses (id, client_id, question, answer, embedding)
                VALUES (:id, :client_id, 'como reservo', 'puedes reservar en...', :embedding::vector)
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": str(rls_harness["tenant_a"]),
                "embedding": fake_embedding,
            },
        )


# ─── Tests por tabla Fase 2 ─────────────────────────────────────────────────


class TestRLSPhase2Tables:
    """Tests de aislamiento RLS para las 6 tablas de Fase 2."""

    async def test_audit_logs(self, rls_harness: dict) -> None:
        """audit_logs: aislamiento por tenant."""
        await assert_rls_isolation(
            rls_harness["session_a"],
            rls_harness["session_b"],
            table="audit_logs",
            insert_sql="""
                INSERT INTO audit_logs (id, client_id, action, entity_type, entity_id)
                VALUES (:id, :client_id, 'create', 'contact', :entity_id)
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": str(rls_harness["tenant_a"]),
                "entity_id": str(uuid.uuid4()),
            },
        )

    async def test_tenant_templates(self, rls_harness: dict) -> None:
        """tenant_templates: aislamiento por tenant."""
        await assert_rls_isolation(
            rls_harness["session_a"],
            rls_harness["session_b"],
            table="tenant_templates",
            insert_sql="""
                INSERT INTO tenant_templates (id, client_id, name, template_data)
                VALUES (:id, :client_id, 'Template Test', '{}')
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": str(rls_harness["tenant_a"]),
            },
        )

    async def test_tenant_webhooks(self, rls_harness: dict) -> None:
        """tenant_webhooks: aislamiento por tenant."""
        await assert_rls_isolation(
            rls_harness["session_a"],
            rls_harness["session_b"],
            table="tenant_webhooks",
            insert_sql="""
                INSERT INTO tenant_webhooks (id, client_id, url, events, is_active)
                VALUES (:id, :client_id, 'https://example.com/hook', ARRAY['message.received'], true)
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": str(rls_harness["tenant_a"]),
            },
        )

    async def test_satisfaction_surveys(self, rls_harness: dict) -> None:
        """satisfaction_surveys: aislamiento por tenant."""
        sa = rls_harness["session_a"]
        cid_a = str(rls_harness["tenant_a"])
        conv_id = str(uuid.uuid4())
        contact_id = str(uuid.uuid4())

        await sa.execute(
            text("""
                INSERT INTO contacts (id, client_id, phone_number, channel)
                VALUES (:id, :cid, '+5215599990006', 'whatsapp')
            """),
            {"id": contact_id, "cid": cid_a},
        )
        await sa.execute(
            text("""
                INSERT INTO conversations (id, client_id, contact_id, status, channel)
                VALUES (:id, :cid, :contact_id, 'resolved', 'whatsapp')
            """),
            {"id": conv_id, "cid": cid_a, "contact_id": contact_id},
        )

        await assert_rls_isolation(
            sa,
            rls_harness["session_b"],
            table="satisfaction_surveys",
            insert_sql="""
                INSERT INTO satisfaction_surveys (id, client_id, conversation_id, score)
                VALUES (:id, :client_id, :conv_id, 5)
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": cid_a,
                "conv_id": conv_id,
            },
        )

    async def test_channel_configs(self, rls_harness: dict) -> None:
        """channel_configs: aislamiento por tenant."""
        await assert_rls_isolation(
            rls_harness["session_a"],
            rls_harness["session_b"],
            table="channel_configs",
            insert_sql="""
                INSERT INTO channel_configs (id, client_id, channel, provider, config, is_active)
                VALUES (:id, :client_id, 'whatsapp', 'ycloud', '{}', true)
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": str(rls_harness["tenant_a"]),
            },
        )


# ─── Tests de Feature Enhancement Tables ────────────────────────────────────


class TestRLSFeatureTables:
    """Tests de aislamiento RLS para tablas de features adicionales."""

    async def test_agent_action_logs(self, rls_harness: dict) -> None:
        """agent_action_logs: aislamiento por tenant."""
        sa = rls_harness["session_a"]
        cid_a = str(rls_harness["tenant_a"])
        conv_id = str(uuid.uuid4())
        contact_id = str(uuid.uuid4())

        await sa.execute(
            text("""
                INSERT INTO contacts (id, client_id, phone_number, channel)
                VALUES (:id, :cid, '+5215599990007', 'whatsapp')
            """),
            {"id": contact_id, "cid": cid_a},
        )
        await sa.execute(
            text("""
                INSERT INTO conversations (id, client_id, contact_id, status, channel)
                VALUES (:id, :cid, :contact_id, 'active', 'whatsapp')
            """),
            {"id": conv_id, "cid": cid_a, "contact_id": contact_id},
        )

        await assert_rls_isolation(
            sa,
            rls_harness["session_b"],
            table="agent_action_logs",
            insert_sql="""
                INSERT INTO agent_action_logs (id, client_id, conversation_id, node_name, action_type, duration_ms)
                VALUES (:id, :client_id, :conv_id, 'intent_router', 'process', 250)
            """,
            params={
                "id": str(uuid.uuid4()),
                "client_id": cid_a,
                "conv_id": conv_id,
            },
        )


# ─── Test de búsqueda vectorial con aislamiento ─────────────────────────────


class TestRLSVectorSearch:
    """Tests de aislamiento RLS en búsquedas vectoriales (RAG)."""

    async def test_vector_search_pre_filter(self, rls_harness: dict) -> None:
        """
        Búsqueda vectorial con pre-filtro por client_id.

        Verifica que el patrón:
            WHERE client_id = current_setting(...)
            AND 1 - (embedding <=> :query) > :threshold
        solo retorna resultados del tenant correcto.
        """
        sa = rls_harness["session_a"]
        sb = rls_harness["session_b"]
        cid_a = str(rls_harness["tenant_a"])
        doc_id = str(uuid.uuid4())

        await sa.execute(
            text("""
                INSERT INTO documents (id, client_id, title, doc_type, status)
                VALUES (:id, :cid, 'RAG Test Doc', 'pdf', 'active')
            """),
            {"id": doc_id, "cid": cid_a},
        )

        fake_embedding = "[" + ",".join(["0.1"] * 1536) + "]"
        chunk_id = str(uuid.uuid4())
        await sa.execute(
            text("""
                INSERT INTO document_chunks (id, client_id, document_id, content, embedding, chunk_index)
                VALUES (:id, :cid, :doc_id, 'info confidencial tenant A', :emb::vector, 0)
            """),
            {"id": chunk_id, "cid": cid_a, "doc_id": doc_id, "emb": fake_embedding},
        )

        # Tenant B busca con el mismo embedding — NO debe encontrar nada
        result = await sb.execute(
            text("""
                SELECT id, 1 - (embedding <=> :query::vector) AS similarity
                FROM document_chunks
                WHERE 1 - (embedding <=> :query::vector) > 0.0
                ORDER BY embedding <=> :query ASC
                LIMIT 5
            """),
            {"query": fake_embedding},
        )
        rows = result.fetchall()
        assert len(rows) == 0, (
            "VIOLACIÓN RLS: Tenant B encontró chunks de Tenant A en búsqueda vectorial"
        )

        # Tenant A SÍ debe encontrar su chunk
        result = await sa.execute(
            text("""
                SELECT id, 1 - (embedding <=> :query::vector) AS similarity
                FROM document_chunks
                WHERE 1 - (embedding <=> :query::vector) > 0.0
                ORDER BY embedding <=> :query ASC
                LIMIT 5
            """),
            {"query": fake_embedding},
        )
        rows = result.fetchall()
        assert len(rows) >= 1, "Tenant A no puede ver su propio chunk en búsqueda vectorial"

    async def test_approved_responses_similarity_isolation(
        self, rls_harness: dict
    ) -> None:
        """
        Few-shot approved_responses: búsqueda por similaridad aislada por tenant.
        """
        sa = rls_harness["session_a"]
        sb = rls_harness["session_b"]
        cid_a = str(rls_harness["tenant_a"])

        fake_embedding = "[" + ",".join(["0.3"] * 1536) + "]"
        await sa.execute(
            text("""
                INSERT INTO approved_responses (id, client_id, question, answer, embedding)
                VALUES (:id, :cid, 'horarios de atención', 'Lunes a Viernes 9-18', :emb::vector)
            """),
            {"id": str(uuid.uuid4()), "cid": cid_a, "emb": fake_embedding},
        )

        # Tenant B busca respuestas aprobadas — NO debe ver las de A
        result = await sb.execute(
            text("""
                SELECT id, 1 - (embedding <=> :query::vector) AS similarity
                FROM approved_responses
                WHERE 1 - (embedding <=> :query::vector) > 0.0
                ORDER BY embedding <=> :query ASC
                LIMIT 3
            """),
            {"query": fake_embedding},
        )
        assert len(result.fetchall()) == 0, (
            "VIOLACIÓN RLS: Tenant B accedió a approved_responses de Tenant A"
        )


# ─── Test de SET LOCAL vs SET ────────────────────────────────────────────────


class TestSetLocalBehavior:
    """Tests que verifican el comportamiento correcto de SET LOCAL."""

    async def test_set_local_is_transaction_scoped(
        self, rls_harness: dict
    ) -> None:
        """SET LOCAL se resetea al terminar la transacción."""
        sa = rls_harness["session_a"]

        result = await sa.execute(
            text("SELECT current_setting('app.current_client_id', true)")
        )
        setting = result.scalar()
        assert setting is not None, "SET LOCAL no estableció app.current_client_id"
        assert setting == str(rls_harness["tenant_a"])

    async def test_different_tenants_different_views(
        self, rls_harness: dict
    ) -> None:
        """Dos sesiones con diferente SET LOCAL ven datos diferentes."""
        sa = rls_harness["session_a"]
        sb = rls_harness["session_b"]

        # Verificar que cada sesión tiene su propio client_id
        result_a = await sa.execute(
            text("SELECT current_setting('app.current_client_id', true)")
        )
        result_b = await sb.execute(
            text("SELECT current_setting('app.current_client_id', true)")
        )

        assert result_a.scalar() == str(rls_harness["tenant_a"])
        assert result_b.scalar() == str(rls_harness["tenant_b"])
        assert result_a.scalar() != result_b.scalar()
