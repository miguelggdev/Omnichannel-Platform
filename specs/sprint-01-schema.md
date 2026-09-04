# Sprint 1 — Schema DDL & Arquitectura

## Objetivo
Cimientos de la base de datos y la estructura del proyecto. Al finalizar este sprint, se tendrá el DDL completo ejecutable, las politicas RLS configuradas, los indices optimizados y un diagrama de arquitectura del sistema.

## Prerequisitos
- PostgreSQL 15+ con pgvector 0.7+, pgcrypto, uuid-ossp disponibles
- Acceso a un servidor de desarrollo
- Conocimiento basico de SQL, extensiones PostgreSQL y RLS

## Archivos a Crear
- `supabase/init/init.sql` — DDL ejecutable completo (idempotente con IF NOT EXISTS)
- `docs/architecture.mermaid` — Diagrama de arquitectura (flujo webhook → respuesta)
- `tests/test_rls_isolation.sql` — Script de test de aislamiento RLS

## Tareas Detalladas

### 1. Extensiones y Roles

Crear las extensiones necesarias al inicio del script DDL:

```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "vector";
```

Crear roles con permisos minimos:
- `app_user`: SELECT, INSERT, UPDATE, DELETE en schema public (usado por la aplicacion via pgBouncer)
- `app_admin`: permisos adicionales incluyendo TRUNCATE, REFERENCES y acceso a funciones administrativas

Importante: los roles deben crearse con IF NOT EXISTS o con un bloque DO $$ para idempotencia.

### 2. Tipos ENUM

Definir TODOS los enums como tipos PostgreSQL antes de crear las tablas:

```sql
CREATE TYPE plan_type AS ENUM ('free', 'starter', 'professional', 'enterprise');
CREATE TYPE user_role AS ENUM ('super_admin', 'admin', 'supervisor', 'agent');
CREATE TYPE channel_type AS ENUM ('whatsapp', 'telegram', 'instagram', 'webchat', 'email', 'phone', 'facebook');
CREATE TYPE conversation_status AS ENUM ('new', 'bot_active', 'human_active', 'waiting_human', 'waiting_client', 'resolved', 'archived');
CREATE TYPE message_direction AS ENUM ('inbound', 'outbound');
CREATE TYPE message_type AS ENUM ('text', 'image', 'audio', 'video', 'document', 'location', 'template', 'interactive');
CREATE TYPE document_status AS ENUM ('pending', 'processing', 'completed', 'failed');
CREATE TYPE agent_type AS ENUM ('intent_router', 'rag', 'scheduling', 'financial', 'marketing', 'clinical', 'vision');
CREATE TYPE pending_response_status AS ENUM ('pending', 'approved', 'rejected');
```

Usar bloque condicional para idempotencia:
```sql
DO $$ BEGIN
    CREATE TYPE plan_type AS ENUM (...);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
```

### 3. Tablas MVP (18)

Crear las 18 tablas en orden de dependencias (tablas referenciadas primero):

**Tabla 1: `clients`** — Tenants (tabla raiz, NO tiene client_id)
```
- id: UUID PK DEFAULT gen_random_uuid()
- name: VARCHAR(255) NOT NULL
- slug: VARCHAR(100) UNIQUE NOT NULL
- plan: plan_type NOT NULL DEFAULT 'free'
- settings: JSONB DEFAULT '{}'::jsonb
- is_active: BOOLEAN NOT NULL DEFAULT true
- created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
- updated_at: TIMESTAMPTZ NOT NULL DEFAULT now()
```

**Tabla 2: `users`** — Usuarios humanos del sistema
```
- id: UUID PK DEFAULT gen_random_uuid()
- client_id: UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE
- email: VARCHAR(255) NOT NULL
- password_hash: VARCHAR(255) NOT NULL
- full_name: VARCHAR(255) NOT NULL
- role: user_role NOT NULL DEFAULT 'agent'
- is_active: BOOLEAN NOT NULL DEFAULT true
- last_login_at: TIMESTAMPTZ
- created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
- updated_at: TIMESTAMPTZ NOT NULL DEFAULT now()
- UNIQUE(client_id, email)
```

**Tabla 3: `contacts`** — Contactos finales (clientes de los tenants)
```
- id: UUID PK DEFAULT gen_random_uuid()
- client_id: UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE
- first_name: VARCHAR(255)
- last_name: VARCHAR(255)
- display_name: VARCHAR(255)
- merged_into_id: UUID REFERENCES contacts(id) ON DELETE SET NULL (self-ref, nullable)
- metadata: JSONB DEFAULT '{}'::jsonb
- created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
- updated_at: TIMESTAMPTZ NOT NULL DEFAULT now()
```

**Tabla 4: `contact_identifiers`** — Identificadores multi-canal
```
- id: UUID PK DEFAULT gen_random_uuid()
- client_id: UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE
- contact_id: UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE
- channel: channel_type NOT NULL
- identifier_value: BYTEA NOT NULL (cifrado con pgcrypto)
- is_primary: BOOLEAN NOT NULL DEFAULT false
- verified_at: TIMESTAMPTZ
- created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
- UNIQUE(client_id, channel, identifier_value)
```

Nota sobre cifrado: `identifier_value` debe almacenarse cifrado usando `pgp_sym_encrypt(valor, key)` y descifrarse con `pgp_sym_decrypt(identifier_value, key)`. La key de cifrado proviene de la variable de entorno ENCRYPTION_KEY. Considerar crear funciones helper:
```sql
CREATE OR REPLACE FUNCTION encrypt_identifier(val TEXT, key TEXT) RETURNS BYTEA AS $$
    SELECT pgp_sym_encrypt(val, key);
$$ LANGUAGE SQL IMMUTABLE;

CREATE OR REPLACE FUNCTION decrypt_identifier(val BYTEA, key TEXT) RETURNS TEXT AS $$
    SELECT pgp_sym_decrypt(val, key);
$$ LANGUAGE SQL IMMUTABLE;
```

**Tabla 5: `tags`** — Etiquetas por tenant
```
- id: UUID PK DEFAULT gen_random_uuid()
- client_id: UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE
- name: VARCHAR(100) NOT NULL
- color: VARCHAR(7) (hex color, e.g. '#FF5733')
- created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
- UNIQUE(client_id, name)
```

**Tabla 6: `contact_tags`** — Relacion N:M contactos-etiquetas
```
- id: UUID PK DEFAULT gen_random_uuid()
- client_id: UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE
- contact_id: UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE
- tag_id: UUID NOT NULL REFERENCES tags(id) ON DELETE CASCADE
- created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
- UNIQUE(client_id, contact_id, tag_id)
```

**Tabla 7: `internal_notes`** — Notas internas sobre contactos
```
- id: UUID PK DEFAULT gen_random_uuid()
- client_id: UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE
- contact_id: UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE
- user_id: UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE
- content: TEXT NOT NULL
- created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
```

**Tabla 8: `conversations`** — Conversaciones con 7 estados
```
- id: UUID PK DEFAULT gen_random_uuid()
- client_id: UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE
- contact_id: UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE
- channel: channel_type NOT NULL
- status: conversation_status NOT NULL DEFAULT 'new'
- assigned_user_id: UUID REFERENCES users(id) ON DELETE SET NULL
- subject: VARCHAR(500)
- metadata: JSONB DEFAULT '{}'::jsonb
- started_at: TIMESTAMPTZ NOT NULL DEFAULT now()
- resolved_at: TIMESTAMPTZ
- last_message_at: TIMESTAMPTZ
- created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
- updated_at: TIMESTAMPTZ NOT NULL DEFAULT now()
```

**Tabla 9: `messages`** — Mensajes individuales
```
- id: UUID PK DEFAULT gen_random_uuid()
- client_id: UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE
- conversation_id: UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE
- contact_id: UUID REFERENCES contacts(id) ON DELETE SET NULL
- user_id: UUID REFERENCES users(id) ON DELETE SET NULL
- direction: message_direction NOT NULL
- message_type: message_type NOT NULL DEFAULT 'text'
- content: TEXT
- media_url: TEXT
- external_message_id: VARCHAR(255)
- provider_status: VARCHAR(50)
- metadata: JSONB DEFAULT '{}'::jsonb
- created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
```

**Tabla 10: `documents`** — Base de conocimiento
```
- id: UUID PK DEFAULT gen_random_uuid()
- client_id: UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE
- title: VARCHAR(500) NOT NULL
- file_path: TEXT NOT NULL
- file_type: VARCHAR(50) NOT NULL
- file_size_bytes: BIGINT
- status: document_status NOT NULL DEFAULT 'pending'
- chunk_count: INT NOT NULL DEFAULT 0
- uploaded_by: UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE
- metadata: JSONB DEFAULT '{}'::jsonb
- created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
- updated_at: TIMESTAMPTZ NOT NULL DEFAULT now()
```

**Tabla 11: `document_chunks`** — Chunks con embeddings vectoriales
```
- id: UUID PK DEFAULT gen_random_uuid()
- client_id: UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE
- document_id: UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE
- chunk_index: INT NOT NULL
- content: TEXT NOT NULL
- embedding: vector(1536) NOT NULL
- token_count: INT NOT NULL
- metadata: JSONB DEFAULT '{}'::jsonb (incluye page_number, section)
- created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
```

**Tabla 12: `token_budgets`** — Presupuesto mensual de tokens por tenant
```
- id: UUID PK DEFAULT gen_random_uuid()
- client_id: UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE
- period_start: DATE NOT NULL
- period_end: DATE NOT NULL
- max_tokens: BIGINT NOT NULL
- used_tokens: BIGINT NOT NULL DEFAULT 0
- alert_threshold_pct: INT NOT NULL DEFAULT 80
- created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
- updated_at: TIMESTAMPTZ NOT NULL DEFAULT now()
- UNIQUE(client_id, period_start)
```

**Tabla 13: `token_usage_log`** — Log granular de uso de tokens
```
- id: UUID PK DEFAULT gen_random_uuid()
- client_id: UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE
- conversation_id: UUID REFERENCES conversations(id) ON DELETE SET NULL
- model_used: VARCHAR(100) NOT NULL
- prompt_tokens: INT NOT NULL
- completion_tokens: INT NOT NULL
- total_tokens: INT NOT NULL
- cost_usd: NUMERIC(10,6) NOT NULL DEFAULT 0
- created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
```

**Tabla 14: `webhook_dedup`** — Deduplicacion de webhooks
```
- id: UUID PK DEFAULT gen_random_uuid()
- client_id: UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE
- channel: channel_type NOT NULL
- external_message_id: VARCHAR(255) NOT NULL
- received_at: TIMESTAMPTZ NOT NULL DEFAULT now()
- processed: BOOLEAN NOT NULL DEFAULT false
- UNIQUE(channel, external_message_id)
```

Nota: la constraint UNIQUE es sobre (channel, external_message_id) SIN client_id, porque un external_message_id es globalmente unico por canal/proveedor.

**Tabla 15: `agent_configs`** — Configuracion de agentes IA por tenant
```
- id: UUID PK DEFAULT gen_random_uuid()
- client_id: UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE
- agent_type: agent_type NOT NULL
- is_enabled: BOOLEAN NOT NULL DEFAULT false
- model_name: VARCHAR(100) DEFAULT 'gpt-4o-mini'
- temperature: NUMERIC(3,2) DEFAULT 0.7
- max_tokens: INT DEFAULT 1000
- system_prompt: TEXT
- settings: JSONB DEFAULT '{}'::jsonb
- created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
- updated_at: TIMESTAMPTZ NOT NULL DEFAULT now()
- UNIQUE(client_id, agent_type)
```

**Tabla 16: `quick_replies`** — Respuestas rapidas
```
- id: UUID PK DEFAULT gen_random_uuid()
- client_id: UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE
- shortcut: VARCHAR(50) NOT NULL
- title: VARCHAR(255) NOT NULL
- content: TEXT NOT NULL
- variables: JSONB DEFAULT '[]'::jsonb
- category: VARCHAR(100)
- is_active: BOOLEAN NOT NULL DEFAULT true
- created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
- updated_at: TIMESTAMPTZ NOT NULL DEFAULT now()
- UNIQUE(client_id, shortcut)
```

**Tabla 17: `pending_responses`** — Respuestas pendientes de aprobacion (training mode)
```
- id: UUID PK DEFAULT gen_random_uuid()
- client_id: UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE
- conversation_id: UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE
- question: TEXT NOT NULL
- suggested_answer: TEXT NOT NULL
- status: pending_response_status NOT NULL DEFAULT 'pending'
- reviewed_by: UUID REFERENCES users(id) ON DELETE SET NULL
- reviewed_at: TIMESTAMPTZ
- created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
```

**Tabla 18: `approved_responses`** — Few-shot dinamicos aprobados
```
- id: UUID PK DEFAULT gen_random_uuid()
- client_id: UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE
- question: TEXT NOT NULL
- answer: TEXT NOT NULL
- embedding: vector(1536) NOT NULL
- source_pending_id: UUID REFERENCES pending_responses(id) ON DELETE SET NULL
- approved_by: UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE
- created_at: TIMESTAMPTZ NOT NULL DEFAULT now()
```

### 4. Tablas Fase 2 (6) — Comentadas o en schema separado

Incluir al final del DDL como bloques comentados con la estructura completa, listos para descomentar en Fase 2:

19. **`audit_logs`** — Log de auditoria de acciones
```
- id, client_id, user_id, action (VARCHAR), entity_type (VARCHAR), entity_id (UUID),
  old_values (JSONB), new_values (JSONB), ip_address (INET), created_at
```

20. **`tenant_templates`** — Templates de WhatsApp por tenant
```
- id, client_id, template_name, language, category, status, components (JSONB),
  external_template_id, created_at, updated_at
```

21. **`tenant_webhooks`** — Webhooks salientes configurados por tenant
```
- id, client_id, url, events (JSONB array), secret, is_active, created_at, updated_at
```

22. **`outgoing_webhook_logs`** — Log de webhooks salientes
```
- id, client_id, tenant_webhook_id (FK), event_type, payload (JSONB),
  response_status (INT), response_body (TEXT), attempts (INT), created_at
```

23. **`satisfaction_surveys`** — Encuestas CSAT
```
- id, client_id, conversation_id (FK), contact_id (FK), score (INT 1-5),
  comment (TEXT), sent_at, responded_at, created_at
```

24. **`channel_configs`** — Configuracion de canales por tenant
```
- id, client_id, channel, provider_name, credentials (JSONB cifrado),
  is_active, webhook_url, settings (JSONB), created_at, updated_at
- UNIQUE(client_id, channel)
```

### 5. Politicas RLS

Habilitar RLS con FORCE en CADA tabla:

```sql
ALTER TABLE nombre_tabla ENABLE ROW LEVEL SECURITY;
ALTER TABLE nombre_tabla FORCE ROW LEVEL SECURITY;
```

Politica para tablas con `client_id`:
```sql
CREATE POLICY tenant_isolation ON nombre_tabla
    FOR ALL
    USING (client_id = current_setting('app.current_client_id')::uuid)
    WITH CHECK (client_id = current_setting('app.current_client_id')::uuid);
```

Politica especial para `clients` (usa `id` en lugar de `client_id`):
```sql
CREATE POLICY tenant_isolation ON clients
    FOR ALL
    USING (id = current_setting('app.current_client_id')::uuid)
    WITH CHECK (id = current_setting('app.current_client_id')::uuid);
```

Conceder permisos al rol `app_user`:
```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO app_user;
```

### 6. Indices

**B-tree en todas las FK:**
```sql
CREATE INDEX IF NOT EXISTS idx_users_client_id ON users(client_id);
CREATE INDEX IF NOT EXISTS idx_contacts_client_id ON contacts(client_id);
CREATE INDEX IF NOT EXISTS idx_contact_identifiers_client_id ON contact_identifiers(client_id);
CREATE INDEX IF NOT EXISTS idx_contact_identifiers_contact_id ON contact_identifiers(contact_id);
CREATE INDEX IF NOT EXISTS idx_contact_tags_client_id ON contact_tags(client_id);
CREATE INDEX IF NOT EXISTS idx_contact_tags_contact_id ON contact_tags(contact_id);
CREATE INDEX IF NOT EXISTS idx_contact_tags_tag_id ON contact_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_internal_notes_client_id ON internal_notes(client_id);
CREATE INDEX IF NOT EXISTS idx_internal_notes_contact_id ON internal_notes(contact_id);
CREATE INDEX IF NOT EXISTS idx_conversations_client_id ON conversations(client_id);
CREATE INDEX IF NOT EXISTS idx_conversations_contact_id ON conversations(contact_id);
CREATE INDEX IF NOT EXISTS idx_conversations_assigned_user_id ON conversations(assigned_user_id);
CREATE INDEX IF NOT EXISTS idx_conversations_status ON conversations(client_id, status);
CREATE INDEX IF NOT EXISTS idx_conversations_last_message_at ON conversations(client_id, last_message_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_client_id ON messages(client_id);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_external_message_id ON messages(external_message_id);
CREATE INDEX IF NOT EXISTS idx_documents_client_id ON documents(client_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_client_id ON document_chunks(client_id);
CREATE INDEX IF NOT EXISTS idx_document_chunks_document_id ON document_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_token_budgets_client_id ON token_budgets(client_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_log_client_id ON token_usage_log(client_id);
CREATE INDEX IF NOT EXISTS idx_webhook_dedup_channel_ext_id ON webhook_dedup(channel, external_message_id);
CREATE INDEX IF NOT EXISTS idx_agent_configs_client_id ON agent_configs(client_id);
CREATE INDEX IF NOT EXISTS idx_pending_responses_client_id ON pending_responses(client_id);
CREATE INDEX IF NOT EXISTS idx_pending_responses_status ON pending_responses(client_id, status);
CREATE INDEX IF NOT EXISTS idx_approved_responses_client_id ON approved_responses(client_id);
```

**GIN en columnas JSONB:**
```sql
CREATE INDEX IF NOT EXISTS idx_clients_settings ON clients USING gin(settings);
CREATE INDEX IF NOT EXISTS idx_contacts_metadata ON contacts USING gin(metadata);
CREATE INDEX IF NOT EXISTS idx_conversations_metadata ON conversations USING gin(metadata);
CREATE INDEX IF NOT EXISTS idx_messages_metadata ON messages USING gin(metadata);
CREATE INDEX IF NOT EXISTS idx_documents_metadata ON documents USING gin(metadata);
CREATE INDEX IF NOT EXISTS idx_document_chunks_metadata ON document_chunks USING gin(metadata);
CREATE INDEX IF NOT EXISTS idx_agent_configs_settings ON agent_configs USING gin(settings);
CREATE INDEX IF NOT EXISTS idx_quick_replies_variables ON quick_replies USING gin(variables);
```

**HNSW en columnas vector:**
```sql
CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding
    ON document_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX IF NOT EXISTS idx_approved_responses_embedding
    ON approved_responses USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);
```

### 7. Funcion de updated_at automatico

Crear trigger function para actualizar `updated_at` automaticamente:

```sql
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
```

Aplicar a todas las tablas que tengan `updated_at`:
```sql
CREATE TRIGGER set_updated_at BEFORE UPDATE ON clients
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
-- Repetir para: users, contacts, conversations, documents, token_budgets,
-- agent_configs, quick_replies
```

### 8. Diagrama Mermaid de Arquitectura

Crear `docs/architecture.mermaid` con el flujo completo:

```
Canales (WhatsApp/Telegram/Instagram/Webchat)
    → Traefik (API Gateway, TLS, Rate Limiting)
        → FastAPI (Auth, Middleware, API v1)
            → pgBouncer → PostgreSQL 15 + pgvector
            → Redis (Cache + Celery Broker)
            → Celery Workers (5 colas)
                → webhooks (concurrency 4)
                → ai_inference (concurrency 2)
                → documents (concurrency 2)
                → notifications (concurrency 2)
                → bulk (concurrency 1)
            → LangGraph (StateGraph)
                → intent_routing
                → rag_query
                → token_budget_check
                → respond → MessagingProvider (YCloud ABC) → Canal
                → human_handoff
                → training_mode_approval
            → Supabase Storage (documentos)
    → Prometheus + Grafana (monitoreo)
```

### 9. Test de aislamiento RLS

Crear `tests/test_rls_isolation.sql`:

```sql
-- 1. Insertar dos tenants
INSERT INTO clients (id, name, slug, plan) VALUES
    ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'Tenant A', 'tenant-a', 'starter'),
    ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'Tenant B', 'tenant-b', 'starter');

-- 2. Insertar datos para Tenant A
SET LOCAL app.current_client_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';

INSERT INTO contacts (client_id, first_name, last_name, display_name)
VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'Juan', 'Perez', 'Juan Perez');

-- Obtener contact_id para uso posterior
-- INSERT conversation y message para Tenant A...

-- 3. Cambiar a Tenant B
SET LOCAL app.current_client_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';

-- 4. Verificar que Tenant B no ve datos de Tenant A
SELECT count(*) AS should_be_zero FROM contacts;
-- Esperado: 0

SELECT count(*) AS should_be_zero FROM conversations;
-- Esperado: 0

-- 5. Cambiar a Tenant A y verificar que ve sus datos
SET LOCAL app.current_client_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';

SELECT count(*) AS should_be_one FROM contacts;
-- Esperado: 1
```

El script debe ejecutarse dentro de una transaccion (BEGIN; ... ROLLBACK;) para limpieza.

## Criterios de Aceptacion
- [ ] DDL ejecuta sin errores en PostgreSQL 15 + pgvector 0.7+
- [ ] Todos los enums estan definidos como TYPE (no strings hardcoded)
- [ ] Todas las tablas tienen `created_at` con DEFAULT now()
- [ ] Todas las tablas con `updated_at` tienen trigger automatico
- [ ] Test RLS pasa: Tenant B no ve datos de Tenant A
- [ ] Test RLS pasa: Tenant A ve sus propios datos
- [ ] Diagrama Mermaid renderiza correctamente
- [ ] Script DDL es idempotente (se puede ejecutar multiples veces sin error)
- [ ] Indices HNSW creados correctamente en columnas vector
- [ ] Funciones de cifrado para contact_identifiers disponibles
- [ ] Roles app_user y app_admin creados con permisos correctos

## Notas Tecnicas

### BUG-001: Alias de SELECT en WHERE
PostgreSQL no permite usar alias de SELECT en clausulas WHERE. Esto afecta especialmente las queries de busqueda vectorial. En lugar de:
```sql
-- INCORRECTO - NO FUNCIONA
SELECT 1 - (embedding <=> :query) AS similarity
FROM document_chunks
WHERE similarity > 0.75;
```
Usar:
```sql
-- CORRECTO
SELECT 1 - (embedding <=> :query) AS similarity
FROM document_chunks
WHERE 1 - (embedding <=> :query) > 0.75;
```

### Cifrado de contact_identifiers
Las columnas `identifier_value` en `contact_identifiers` deben estar cifradas con pgcrypto usando `pgp_sym_encrypt`/`pgp_sym_decrypt`. La clave de cifrado se pasa desde la aplicacion, nunca se almacena en la base de datos.

### gen_random_uuid() vs uuid_generate_v4()
Usar `gen_random_uuid()` de pgcrypto en lugar de `uuid_generate_v4()` de uuid-ossp. Es mas seguro criptograficamente y no requiere extension adicional si pgcrypto ya esta habilitada.

### Indices HNSW
Los indices HNSW requieren pgvector 0.5+ (usamos 0.7+). Los parametros `m = 16` y `ef_construction = 200` son valores recomendados para un balance entre velocidad y precision.

### SET LOCAL vs SET
SIEMPRE usar `SET LOCAL` para establecer el contexto del tenant. `SET LOCAL` tiene scope de transaccion, lo cual es esencial para compatibilidad con pgBouncer en transaction mode. `SET` (sin LOCAL) persiste por sesion y puede causar fuga de datos entre tenants cuando pgBouncer reutiliza conexiones.

## Dependencias para Sprint 2
- El archivo `supabase/init/init.sql` debe ser completamente idempotente (IF NOT EXISTS en todas las sentencias) porque se montara como volumen en Docker y se ejecutara al inicializar el contenedor de PostgreSQL.
- Los roles `app_user` y `app_admin` creados aqui se referencian en la configuracion de pgBouncer del Sprint 2.
- Las extensiones deben cargarse antes de cualquier tabla que use tipos `uuid` o `vector`.
- El schema de tablas Fase 2 debe estar comentado pero presente para referencia.
