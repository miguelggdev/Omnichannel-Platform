-- =============================================================================
-- init.sql — DDL Completo: Plataforma SaaS Omnicanal Multi-Tenant
-- =============================================================================
-- PostgreSQL 15+ | pgvector 0.7+ | pgcrypto | uuid-ossp
-- Ejecutable directamente: psql -f init.sql
-- Idempotente: se puede ejecutar múltiples veces sin error
-- =============================================================================
-- Sprint 1 — Schema DDL & Arquitectura
-- =============================================================================

BEGIN;

-- ═══════════════════════════════════════════════════════════════════════════════
-- 1. EXTENSIONES
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "vector";

-- ═══════════════════════════════════════════════════════════════════════════════
-- 2. ROLES
-- ═══════════════════════════════════════════════════════════════════════════════

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user NOLOGIN;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_admin') THEN
        CREATE ROLE app_admin NOLOGIN;
    END IF;
END
$$;

-- ═══════════════════════════════════════════════════════════════════════════════
-- 3. TIPOS ENUM
-- ═══════════════════════════════════════════════════════════════════════════════

DO $$ BEGIN CREATE TYPE plan_type AS ENUM (
    'free', 'starter', 'professional', 'enterprise'
); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TYPE user_role AS ENUM (
    'super_admin', 'admin', 'supervisor', 'agent'
); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TYPE channel_type AS ENUM (
    'whatsapp', 'telegram', 'instagram', 'webchat', 'email', 'phone', 'facebook'
); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TYPE conversation_status AS ENUM (
    'new', 'bot_active', 'human_active', 'waiting_human',
    'waiting_client', 'resolved', 'archived'
); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TYPE message_direction AS ENUM (
    'inbound', 'outbound'
); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TYPE message_type AS ENUM (
    'text', 'image', 'audio', 'video', 'document',
    'location', 'template', 'interactive'
); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TYPE document_status AS ENUM (
    'pending', 'processing', 'completed', 'failed'
); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TYPE agent_type AS ENUM (
    'intent_router', 'rag', 'scheduling', 'financial',
    'marketing', 'clinical', 'vision'
); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TYPE pending_response_status AS ENUM (
    'pending', 'approved', 'rejected'
); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ═══════════════════════════════════════════════════════════════════════════════
-- 4. FUNCIONES HELPER
-- ═══════════════════════════════════════════════════════════════════════════════

-- Trigger para actualizar updated_at automáticamente
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Funciones de cifrado para identificadores de contacto
CREATE OR REPLACE FUNCTION encrypt_identifier(val TEXT, key TEXT)
RETURNS BYTEA AS $$
    SELECT pgp_sym_encrypt(val, key);
$$ LANGUAGE SQL IMMUTABLE;

CREATE OR REPLACE FUNCTION decrypt_identifier(val BYTEA, key TEXT)
RETURNS TEXT AS $$
    SELECT pgp_sym_decrypt(val, key);
$$ LANGUAGE SQL IMMUTABLE;


-- ═══════════════════════════════════════════════════════════════════════════════
-- 5. TABLAS MVP (18)
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─── Tabla 1: clients ───────────────────────────────────────────────────────
-- Tenants (tabla raíz, NO tiene client_id)
CREATE TABLE IF NOT EXISTS clients (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(255) NOT NULL,
    slug        VARCHAR(100) UNIQUE NOT NULL,
    plan        plan_type NOT NULL DEFAULT 'free',
    settings    JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Tabla 2: users ─────────────────────────────────────────────────────────
-- Usuarios humanos del sistema (agentes, admins, supervisores)
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id       UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    email           VARCHAR(255) NOT NULL,
    password_hash   VARCHAR(255) NOT NULL,
    full_name       VARCHAR(255) NOT NULL,
    role            user_role NOT NULL DEFAULT 'agent',
    is_active       BOOLEAN NOT NULL DEFAULT true,
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_users_client_email UNIQUE (client_id, email)
);

-- ─── Tabla 3: contacts ──────────────────────────────────────────────────────
-- Contactos finales (clientes de los tenants)
CREATE TABLE IF NOT EXISTS contacts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id       UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    first_name      VARCHAR(255),
    last_name       VARCHAR(255),
    display_name    VARCHAR(255),
    merged_into_id  UUID REFERENCES contacts(id) ON DELETE SET NULL,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Tabla 4: contact_identifiers ───────────────────────────────────────────
-- Identificadores multi-canal (cifrados con pgcrypto)
CREATE TABLE IF NOT EXISTS contact_identifiers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id           UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    contact_id          UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    channel             channel_type NOT NULL,
    identifier_value    BYTEA NOT NULL,  -- cifrado con pgp_sym_encrypt
    is_primary          BOOLEAN NOT NULL DEFAULT false,
    verified_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_contact_identifiers UNIQUE (client_id, channel, identifier_value)
);

-- ─── Tabla 5: tags ──────────────────────────────────────────────────────────
-- Etiquetas por tenant
CREATE TABLE IF NOT EXISTS tags (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name        VARCHAR(100) NOT NULL,
    color       VARCHAR(7),  -- hex color, e.g. '#FF5733'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_tags_client_name UNIQUE (client_id, name)
);

-- ─── Tabla 6: contact_tags ──────────────────────────────────────────────────
-- Relación N:M contactos-etiquetas
CREATE TABLE IF NOT EXISTS contact_tags (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    contact_id  UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    tag_id      UUID NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_contact_tags UNIQUE (client_id, contact_id, tag_id)
);

-- ─── Tabla 7: internal_notes ────────────────────────────────────────────────
-- Notas internas sobre contactos
CREATE TABLE IF NOT EXISTS internal_notes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    contact_id  UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Tabla 8: conversations ─────────────────────────────────────────────────
-- Conversaciones con 7 estados
CREATE TABLE IF NOT EXISTS conversations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id           UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    contact_id          UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    channel             channel_type NOT NULL,
    status              conversation_status NOT NULL DEFAULT 'new',
    assigned_user_id    UUID REFERENCES users(id) ON DELETE SET NULL,
    subject             VARCHAR(500),
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at         TIMESTAMPTZ,
    last_message_at     TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Tabla 9: messages ──────────────────────────────────────────────────────
-- Mensajes individuales
CREATE TABLE IF NOT EXISTS messages (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id               UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    conversation_id         UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    contact_id              UUID REFERENCES contacts(id) ON DELETE SET NULL,
    user_id                 UUID REFERENCES users(id) ON DELETE SET NULL,
    direction               message_direction NOT NULL,
    message_type            message_type NOT NULL DEFAULT 'text',
    content                 TEXT,
    media_url               TEXT,
    external_message_id     VARCHAR(255),
    provider_status         VARCHAR(50),
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Tabla 10: documents ────────────────────────────────────────────────────
-- Base de conocimiento
CREATE TABLE IF NOT EXISTS documents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id       UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    title           VARCHAR(500) NOT NULL,
    file_path       TEXT NOT NULL,
    file_type       VARCHAR(50) NOT NULL,
    file_size_bytes BIGINT,
    status          document_status NOT NULL DEFAULT 'pending',
    chunk_count     INT NOT NULL DEFAULT 0,
    uploaded_by     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Tabla 11: document_chunks ──────────────────────────────────────────────
-- Chunks con embeddings vectoriales
CREATE TABLE IF NOT EXISTS document_chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id       UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index     INT NOT NULL,
    content         TEXT NOT NULL,
    embedding       vector(1536) NOT NULL,
    token_count     INT NOT NULL,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Tabla 12: token_budgets ────────────────────────────────────────────────
-- Presupuesto mensual de tokens por tenant
CREATE TABLE IF NOT EXISTS token_budgets (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id           UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,
    max_tokens          BIGINT NOT NULL,
    used_tokens         BIGINT NOT NULL DEFAULT 0,
    alert_threshold_pct INT NOT NULL DEFAULT 80,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_token_budgets_client_period UNIQUE (client_id, period_start)
);

-- ─── Tabla 13: token_usage_log ──────────────────────────────────────────────
-- Log granular de uso de tokens
CREATE TABLE IF NOT EXISTS token_usage_log (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id           UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    conversation_id     UUID REFERENCES conversations(id) ON DELETE SET NULL,
    model_used          VARCHAR(100) NOT NULL,
    prompt_tokens       INT NOT NULL,
    completion_tokens   INT NOT NULL,
    total_tokens        INT NOT NULL,
    cost_usd            NUMERIC(10,6) NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Tabla 14: webhook_dedup ────────────────────────────────────────────────
-- Deduplicación de webhooks entrantes
CREATE TABLE IF NOT EXISTS webhook_dedup (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id               UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    channel                 channel_type NOT NULL,
    external_message_id     VARCHAR(255) NOT NULL,
    received_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed               BOOLEAN NOT NULL DEFAULT false,

    -- external_message_id es globalmente único por canal/proveedor
    CONSTRAINT uq_webhook_dedup UNIQUE (channel, external_message_id)
);

-- ─── Tabla 15: agent_configs ────────────────────────────────────────────────
-- Configuración de agentes IA por tenant
CREATE TABLE IF NOT EXISTS agent_configs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id       UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    agent_type      agent_type NOT NULL,
    is_enabled      BOOLEAN NOT NULL DEFAULT false,
    model_name      VARCHAR(100) DEFAULT 'gpt-4o-mini',
    temperature     NUMERIC(3,2) DEFAULT 0.7,
    max_tokens      INT DEFAULT 1000,
    system_prompt   TEXT,
    settings        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_agent_configs_client_type UNIQUE (client_id, agent_type)
);

-- ─── Tabla 16: quick_replies ────────────────────────────────────────────────
-- Respuestas rápidas
CREATE TABLE IF NOT EXISTS quick_replies (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    shortcut    VARCHAR(50) NOT NULL,
    title       VARCHAR(255) NOT NULL,
    content     TEXT NOT NULL,
    variables   JSONB NOT NULL DEFAULT '[]'::jsonb,
    category    VARCHAR(100),
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_quick_replies_client_shortcut UNIQUE (client_id, shortcut)
);

-- ─── Tabla 17: pending_responses ────────────────────────────────────────────
-- Respuestas pendientes de aprobación (training mode)
CREATE TABLE IF NOT EXISTS pending_responses (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id           UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    conversation_id     UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    question            TEXT NOT NULL,
    suggested_answer    TEXT NOT NULL,
    status              pending_response_status NOT NULL DEFAULT 'pending',
    reviewed_by         UUID REFERENCES users(id) ON DELETE SET NULL,
    reviewed_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Tabla 18: approved_responses ───────────────────────────────────────────
-- Few-shot dinámicos aprobados
CREATE TABLE IF NOT EXISTS approved_responses (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id           UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    question            TEXT NOT NULL,
    answer              TEXT NOT NULL,
    embedding           vector(1536) NOT NULL,
    source_pending_id   UUID REFERENCES pending_responses(id) ON DELETE SET NULL,
    approved_by         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ═══════════════════════════════════════════════════════════════════════════════
-- 6. ROW LEVEL SECURITY (RLS)
-- ═══════════════════════════════════════════════════════════════════════════════
-- REGLA ABSOLUTA: RLS habilitado con FORCE en CADA tabla.
-- Política: client_id = current_setting('app.current_client_id')::uuid
-- SIEMPRE SET LOCAL, NUNCA SET (pgBouncer transaction mode)

-- clients: usa id en lugar de client_id (es la tabla raíz)
ALTER TABLE clients ENABLE ROW LEVEL SECURITY;
ALTER TABLE clients FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON clients;
CREATE POLICY tenant_isolation ON clients
    FOR ALL
    USING (id = current_setting('app.current_client_id')::uuid)
    WITH CHECK (id = current_setting('app.current_client_id')::uuid);

-- Macro para las demás tablas (todas usan client_id)
DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOR tbl IN
        SELECT unnest(ARRAY[
            'users', 'contacts', 'contact_identifiers', 'tags',
            'contact_tags', 'internal_notes', 'conversations', 'messages',
            'documents', 'document_chunks', 'token_budgets', 'token_usage_log',
            'webhook_dedup', 'agent_configs', 'quick_replies',
            'pending_responses', 'approved_responses'
        ])
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', tbl);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', tbl);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I
                FOR ALL
                USING (client_id = current_setting(''app.current_client_id'')::uuid)
                WITH CHECK (client_id = current_setting(''app.current_client_id'')::uuid)',
            tbl
        );
    END LOOP;
END
$$;


-- ═══════════════════════════════════════════════════════════════════════════════
-- 7. TRIGGERS — updated_at automático
-- ═══════════════════════════════════════════════════════════════════════════════

DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOR tbl IN
        SELECT unnest(ARRAY[
            'clients', 'users', 'contacts', 'conversations',
            'documents', 'token_budgets', 'agent_configs', 'quick_replies'
        ])
    LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS set_updated_at ON %I;
             CREATE TRIGGER set_updated_at
                BEFORE UPDATE ON %I
                FOR EACH ROW
                EXECUTE FUNCTION update_updated_at_column()',
            tbl, tbl
        );
    END LOOP;
END
$$;


-- ═══════════════════════════════════════════════════════════════════════════════
-- 8. ÍNDICES
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─── B-tree: Foreign Keys y columnas de filtrado frecuente ──────────────────

-- users
CREATE INDEX IF NOT EXISTS idx_users_client_id ON users(client_id);

-- contacts
CREATE INDEX IF NOT EXISTS idx_contacts_client_id ON contacts(client_id);
CREATE INDEX IF NOT EXISTS idx_contacts_merged_into ON contacts(merged_into_id) WHERE merged_into_id IS NOT NULL;

-- contact_identifiers
CREATE INDEX IF NOT EXISTS idx_contact_identifiers_client_id ON contact_identifiers(client_id);
CREATE INDEX IF NOT EXISTS idx_contact_identifiers_contact_id ON contact_identifiers(contact_id);

-- tags
CREATE INDEX IF NOT EXISTS idx_tags_client_id ON tags(client_id);

-- contact_tags
CREATE INDEX IF NOT EXISTS idx_contact_tags_client_id ON contact_tags(client_id);
CREATE INDEX IF NOT EXISTS idx_contact_tags_contact_id ON contact_tags(contact_id);
CREATE INDEX IF NOT EXISTS idx_contact_tags_tag_id ON contact_tags(tag_id);

-- internal_notes
CREATE INDEX IF NOT EXISTS idx_internal_notes_client_id ON internal_notes(client_id);
CREATE INDEX IF NOT EXISTS idx_internal_notes_contact_id ON internal_notes(contact_id);

-- conversations
CREATE INDEX IF NOT EXISTS idx_conversations_client_id ON conversations(client_id);
CREATE INDEX IF NOT EXISTS idx_conversations_contact_id ON conversations(contact_id);
CREATE INDEX IF NOT EXISTS idx_conversations_assigned_user ON conversations(assigned_user_id) WHERE assigned_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_conversations_status ON conversations(client_id, status);
CREATE INDEX IF NOT EXISTS idx_conversations_last_message ON conversations(client_id, last_message_at DESC NULLS LAST);

-- messages
CREATE INDEX IF NOT EXISTS idx_messages_client_id ON messages(client_id);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_external_id ON messages(external_message_id) WHERE external_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(conversation_id, created_at);

-- documents
CREATE INDEX IF NOT EXISTS idx_documents_client_id ON documents(client_id);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(client_id, status);

-- document_chunks
CREATE INDEX IF NOT EXISTS idx_document_chunks_client_id ON document_chunks(client_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_document_id ON document_chunks(document_id);

-- token_budgets
CREATE INDEX IF NOT EXISTS idx_token_budgets_client_id ON token_budgets(client_id);

-- token_usage_log
CREATE INDEX IF NOT EXISTS idx_token_usage_log_client_id ON token_usage_log(client_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_log_created ON token_usage_log(client_id, created_at);

-- webhook_dedup
CREATE INDEX IF NOT EXISTS idx_webhook_dedup_client_id ON webhook_dedup(client_id);
CREATE INDEX IF NOT EXISTS idx_webhook_dedup_channel_ext ON webhook_dedup(channel, external_message_id);
CREATE INDEX IF NOT EXISTS idx_webhook_dedup_received ON webhook_dedup(received_at);

-- agent_configs
CREATE INDEX IF NOT EXISTS idx_agent_configs_client_id ON agent_configs(client_id);

-- pending_responses
CREATE INDEX IF NOT EXISTS idx_pending_responses_client_id ON pending_responses(client_id);
CREATE INDEX IF NOT EXISTS idx_pending_responses_status ON pending_responses(client_id, status);

-- approved_responses
CREATE INDEX IF NOT EXISTS idx_approved_responses_client_id ON approved_responses(client_id);

-- ─── GIN: Columnas JSONB ────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_clients_settings_gin ON clients USING gin(settings);
CREATE INDEX IF NOT EXISTS idx_contacts_metadata_gin ON contacts USING gin(metadata);
CREATE INDEX IF NOT EXISTS idx_conversations_metadata_gin ON conversations USING gin(metadata);
CREATE INDEX IF NOT EXISTS idx_messages_metadata_gin ON messages USING gin(metadata);
CREATE INDEX IF NOT EXISTS idx_documents_metadata_gin ON documents USING gin(metadata);
CREATE INDEX IF NOT EXISTS idx_document_chunks_metadata_gin ON document_chunks USING gin(metadata);
CREATE INDEX IF NOT EXISTS idx_agent_configs_settings_gin ON agent_configs USING gin(settings);
CREATE INDEX IF NOT EXISTS idx_quick_replies_variables_gin ON quick_replies USING gin(variables);

-- ─── HNSW: Columnas vector (embeddings) ─────────────────────────────────────
-- m = 16, ef_construction = 200 — balance entre velocidad y precisión
-- Requiere pgvector 0.5+ (usamos 0.7+)

CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding
    ON document_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX IF NOT EXISTS idx_approved_responses_embedding
    ON approved_responses USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);


-- ═══════════════════════════════════════════════════════════════════════════════
-- 9. PERMISOS
-- ═══════════════════════════════════════════════════════════════════════════════

-- app_user: permisos básicos CRUD
GRANT USAGE ON SCHEMA public TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO app_user;

-- app_admin: permisos extendidos
GRANT USAGE ON SCHEMA public TO app_admin;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO app_admin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO app_admin;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO app_admin;

-- Permisos para tablas futuras
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON TABLES TO app_admin;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE ON SEQUENCES TO app_user;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON SEQUENCES TO app_admin;


COMMIT;

-- ═══════════════════════════════════════════════════════════════════════════════
-- FIN DEL DDL MVP
-- ═══════════════════════════════════════════════════════════════════════════════
