-- =============================================================================
-- phase2_tables.sql — Tablas Fase 2 (Sprints 9-12)
-- =============================================================================
-- Descomentar y ejecutar cuando se inicien los sprints de Fase 2.
-- Requiere que init.sql se haya ejecutado previamente.
-- =============================================================================
-- ESTADO (2026-09-10, issue #6 / BUG-005): LEGACY junto con init.sql — ver la
-- nota de estado al inicio de ese archivo. Estas 6 tablas (audit_logs,
-- tenant_templates, tenant_webhooks, satisfaction_surveys, channel_configs,
-- agent_action_logs) tampoco existen todavía en ninguna migración de Alembic;
-- cuando se creen, deben nacer como migraciones (no ejecutando este archivo).
-- =============================================================================

BEGIN;

-- ═══════════════════════════════════════════════════════════════════════════════
-- TIPOS ENUM adicionales para Fase 2
-- ═══════════════════════════════════════════════════════════════════════════════

DO $$ BEGIN CREATE TYPE audit_action AS ENUM (
    'create', 'update', 'delete', 'login', 'logout',
    'export', 'import', 'assign', 'escalate', 'resolve'
); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TYPE template_status AS ENUM (
    'draft', 'pending_approval', 'approved', 'rejected', 'paused'
); EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN CREATE TYPE template_category AS ENUM (
    'marketing', 'utility', 'authentication'
); EXCEPTION WHEN duplicate_object THEN NULL; END $$;


-- ═══════════════════════════════════════════════════════════════════════════════
-- Tabla 19: audit_logs — Log de auditoría de acciones (Sprint 8+)
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS audit_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id       UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    user_id         UUID REFERENCES users(id) ON DELETE SET NULL,
    action          audit_action NOT NULL,
    entity_type     VARCHAR(100) NOT NULL,
    entity_id       UUID,
    old_values      JSONB,
    new_values      JSONB,
    ip_address      INET,
    user_agent      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ═══════════════════════════════════════════════════════════════════════════════
-- Tabla 20: tenant_templates — Templates de WhatsApp por tenant (Sprint 10)
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS tenant_templates (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id               UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    template_name           VARCHAR(255) NOT NULL,
    language                VARCHAR(10) NOT NULL DEFAULT 'es',
    category                template_category NOT NULL DEFAULT 'utility',
    status                  template_status NOT NULL DEFAULT 'draft',
    components              JSONB NOT NULL DEFAULT '[]'::jsonb,
    external_template_id    VARCHAR(255),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_tenant_templates UNIQUE (client_id, template_name, language)
);

-- ═══════════════════════════════════════════════════════════════════════════════
-- Tabla 21: tenant_webhooks — Webhooks salientes configurados (Sprint 11)
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS tenant_webhooks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    events      JSONB NOT NULL DEFAULT '[]'::jsonb,  -- ["message.received", "conversation.resolved"]
    secret      VARCHAR(255) NOT NULL,                -- HMAC-SHA256 signing secret
    is_active   BOOLEAN NOT NULL DEFAULT true,
    max_retries INT NOT NULL DEFAULT 3,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ═══════════════════════════════════════════════════════════════════════════════
-- Tabla 22: outgoing_webhook_logs — Log de webhooks salientes (Sprint 11)
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS outgoing_webhook_logs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id           UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    tenant_webhook_id   UUID NOT NULL REFERENCES tenant_webhooks(id) ON DELETE CASCADE,
    event_type          VARCHAR(100) NOT NULL,
    payload             JSONB NOT NULL,
    response_status     INT,
    response_body       TEXT,
    attempts            INT NOT NULL DEFAULT 0,
    next_retry_at       TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ═══════════════════════════════════════════════════════════════════════════════
-- Tabla 23: satisfaction_surveys — Encuestas CSAT (Sprint 11)
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS satisfaction_surveys (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id           UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    conversation_id     UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    contact_id          UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    score               INT CHECK (score BETWEEN 1 AND 5),
    comment             TEXT,
    sent_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    responded_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ═══════════════════════════════════════════════════════════════════════════════
-- Tabla 24: channel_configs — Configuración de canales por tenant (Sprint 9)
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS channel_configs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id       UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    channel         channel_type NOT NULL,
    provider_name   VARCHAR(100) NOT NULL,
    credentials     BYTEA NOT NULL,          -- cifrado con pgp_sym_encrypt
    is_active       BOOLEAN NOT NULL DEFAULT true,
    webhook_url     TEXT,
    settings        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_channel_configs UNIQUE (client_id, channel)
);


-- ═══════════════════════════════════════════════════════════════════════════════
-- RLS para tablas Fase 2
-- ═══════════════════════════════════════════════════════════════════════════════

DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOR tbl IN
        SELECT unnest(ARRAY[
            'audit_logs', 'tenant_templates', 'tenant_webhooks',
            'outgoing_webhook_logs', 'satisfaction_surveys', 'channel_configs'
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
-- Triggers updated_at para tablas Fase 2
-- ═══════════════════════════════════════════════════════════════════════════════

DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOR tbl IN
        SELECT unnest(ARRAY[
            'tenant_templates', 'tenant_webhooks', 'channel_configs'
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
-- Índices Fase 2
-- ═══════════════════════════════════════════════════════════════════════════════

-- audit_logs
CREATE INDEX IF NOT EXISTS idx_audit_logs_client_id ON audit_logs(client_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id ON audit_logs(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_logs_entity ON audit_logs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(client_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_old_values_gin ON audit_logs USING gin(old_values);
CREATE INDEX IF NOT EXISTS idx_audit_logs_new_values_gin ON audit_logs USING gin(new_values);

-- tenant_templates
CREATE INDEX IF NOT EXISTS idx_tenant_templates_client_id ON tenant_templates(client_id);
CREATE INDEX IF NOT EXISTS idx_tenant_templates_components_gin ON tenant_templates USING gin(components);

-- tenant_webhooks
CREATE INDEX IF NOT EXISTS idx_tenant_webhooks_client_id ON tenant_webhooks(client_id);
CREATE INDEX IF NOT EXISTS idx_tenant_webhooks_events_gin ON tenant_webhooks USING gin(events);

-- outgoing_webhook_logs
CREATE INDEX IF NOT EXISTS idx_outgoing_webhook_logs_client_id ON outgoing_webhook_logs(client_id);
CREATE INDEX IF NOT EXISTS idx_outgoing_webhook_logs_webhook ON outgoing_webhook_logs(tenant_webhook_id);
CREATE INDEX IF NOT EXISTS idx_outgoing_webhook_logs_retry ON outgoing_webhook_logs(next_retry_at)
    WHERE next_retry_at IS NOT NULL AND attempts < 3;

-- satisfaction_surveys
CREATE INDEX IF NOT EXISTS idx_satisfaction_surveys_client_id ON satisfaction_surveys(client_id);
CREATE INDEX IF NOT EXISTS idx_satisfaction_surveys_conversation ON satisfaction_surveys(conversation_id);
CREATE INDEX IF NOT EXISTS idx_satisfaction_surveys_contact ON satisfaction_surveys(contact_id);

-- channel_configs
CREATE INDEX IF NOT EXISTS idx_channel_configs_client_id ON channel_configs(client_id);
CREATE INDEX IF NOT EXISTS idx_channel_configs_settings_gin ON channel_configs USING gin(settings);

-- Permisos
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO app_user;

COMMIT;

-- ═══════════════════════════════════════════════════════════════════════════════
-- FIN DEL DDL FASE 2
-- ═══════════════════════════════════════════════════════════════════════════════
