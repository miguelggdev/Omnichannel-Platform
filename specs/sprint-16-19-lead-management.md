# Sprint 16-19: Módulo de Lead Management con IA

> Referencia de diseño: BuilderX/AI CRM (@diego.vetencourt)
> Pipeline: CAPTURA → ENRIQUECE → CALIFICA → ASIGNA → FOLLOW-UP → AGENDA → MIDE → CIERRA
> ADRs: 021-025 | Tablas: #27-36

---

## Visión General

Módulo de gestión de leads con IA integrado en la plataforma omnicanal multi-tenant.
Cada tenant puede activar el módulo (`lead_management_enabled`), configurar su propio
pipeline, ICP, scoring y secuencias de follow-up. La IA genera mensajes personalizados
usando RAG (knowledge base del negocio) + few-shot (respuestas aprobadas por humanos).

### Ventajas sobre BuilderX

| Aspecto | BuilderX | Nuestra plataforma |
|---|---|---|
| Multi-tenant | No visible | RLS nativo, cada tenant su pipeline |
| Canales de outreach | Básico | WhatsApp + Instagram + Facebook + Telegram + Email + Webchat |
| Personalización | Template genérico | RAG + few-shot de respuestas aprobadas |
| Voz IA | No | Vapi/Bland.ai + agenda humana |
| Scoring | FIT Score fijo | FIT + Behavioral + AI Score triple |
| Pipeline | Etapas fijas | Configurable por tenant |
| Training mode | No | Humanos aprueban pitch → few-shot |
| Theming | Dark+azul fijo | Dark/orange, Dark/blue, Light + toggle + accent por tenant |
| Presupuesto IA | No visible | TokenBudgetGuard con degradación |
| Idiomas | Solo español | i18n (6 idiomas) |

---

## Sprint 16: Lead Capture & Pipeline Foundation

### Objetivo
Infraestructura base del módulo de leads + captura multi-fuente.

### Tablas nuevas
- `lead_pipeline_stages` (#27) — Etapas configurables del pipeline por tenant
- `leads` (#28) — Entidad principal de lead
- `lead_sources` (#29) — Fuentes de captura configuradas por tenant

### Alteraciones en tablas existentes
```sql
ALTER TABLE clients ADD COLUMN lead_management_enabled BOOLEAN DEFAULT false;
ALTER TABLE clients ADD COLUMN icp_config JSONB DEFAULT '{}';
ALTER TABLE clients ADD COLUMN lead_scoring_weights JSONB DEFAULT '{"fit": 40, "behavioral": 30, "ai": 30}';
ALTER TABLE clients ADD COLUMN theme_config JSONB DEFAULT '{"mode": "dark", "accent": "#FF6B00", "variant": "default"}';
ALTER TABLE contacts ADD COLUMN lead_id UUID REFERENCES leads(id);
ALTER TABLE contacts ADD COLUMN is_lead BOOLEAN DEFAULT false;
```

### Endpoints API
```
POST   /api/v1/leads                    # Crear lead
GET    /api/v1/leads                    # Listar (filtros, paginación, sorting)
GET    /api/v1/leads/{id}               # Detalle
PUT    /api/v1/leads/{id}               # Actualizar
DELETE /api/v1/leads/{id}               # Soft delete
PATCH  /api/v1/leads/{id}/stage         # Mover de etapa
POST   /api/v1/leads/import             # Import CSV/Excel
POST   /api/v1/capture/{source_token}   # Captura pública (web forms, rate limited)
GET    /api/v1/leads/kanban             # Vista Kanban por stages
CRUD   /api/v1/lead-pipeline-stages     # Etapas del pipeline
CRUD   /api/v1/lead-sources             # Fuentes de leads
```

### Tareas
| Tarea | Estimación |
|---|---|
| DDL: tablas #27-29 + RLS + índices | 2 días |
| Alembic migrations + alteraciones clients/contacts | 1 día |
| API CRUD leads con filtros, paginación, sorting | 3 días |
| API pipeline stages + stages default en onboarding | 1 día |
| API lead sources + webhook receiver genérico | 2 días |
| Web form widget: POST /capture/{source_token} con rate limiting | 1 día |
| LinkedIn: import CSV + webhook para Phantombuster | 2 días |
| Lead ↔ Contact sync bidireccional | 1 día |
| Pipeline Kanban API (leads agrupados por stage) | 1 día |
| Tests unit + integration con RLS + migration checks | 2 días |

**Total: ~16 días (2 devs = 8 días calendario)**

---

## Sprint 17: Enrichment & Qualification

### Objetivo
Enriquecimiento automático + scoring inteligente + ICP matching.

### Tablas nuevas
- `lead_activities` (#30) — Log de actividades por lead
- `lead_scores` (#31) — Historial de scoring (FIT, behavioral, AI)

### Endpoints API
```
POST   /api/v1/leads/{id}/enrich        # Trigger enrichment manual
GET    /api/v1/leads/{id}/score         # Ver scores detallados
POST   /api/v1/leads/{id}/score/recalc  # Recalcular scores
PUT    /api/v1/clients/icp              # Configurar ICP del tenant
GET    /api/v1/leads/{id}/activities    # Timeline de actividades
POST   /api/v1/leads/{id}/assign       # Asignar a agente
```

### Tareas
| Tarea | Estimación |
|---|---|
| DDL: tablas #30-31 + RLS | 1 día |
| Enrichment engine: empresa por dominio, scraping web, LinkedIn | 3 días |
| Integración Clearbit/Apollo/Hunter.io (configurable por tenant) | 2 días |
| Enrichment cache con TTL | 1 día |
| FIT Score calculator (industry + company_size + job_title + region) | 2 días |
| Behavioral scoring (respuestas, aperturas, clicks, tiempo) | 2 días |
| AI Score: Claude/GPT analiza conversaciones + reasoning | 2 días |
| ICP matching contra clients.icp_config | 1 día |
| Auto-qualification rules (score > X → mover a etapa Y) | 2 días |
| Lead assignment (round-robin, por territorio, por carga) | 1 día |
| Activity logging en lead_activities | 1 día |
| Celery: cola `lead-enrichment` para async | 1 día |
| Tests unit + integration + scoring accuracy | 2 días |

**Total: ~21 días (2 devs = ~11 días calendario)**

---

## Sprint 18: AI Follow-up & Sequences

### Objetivo
Secuencias automatizadas multi-canal con IA + follow-up inteligente.

### Tablas nuevas
- `lead_sequences` (#32) — Secuencias de follow-up automatizadas
- `lead_sequence_steps` (#33) — Pasos individuales de cada secuencia
- `lead_sequence_enrollments` (#34) — Leads inscritos en secuencias activas

### Endpoints API
```
CRUD   /api/v1/lead-sequences           # Secuencias de follow-up
GET    /api/v1/lead-sequences/{id}/steps # Pasos de una secuencia
POST   /api/v1/leads/{id}/enroll/{seq}  # Inscribir lead en secuencia
DELETE /api/v1/leads/{id}/enroll/{seq}  # Sacar lead de secuencia
GET    /api/v1/leads/follow-up/kanban   # Kanban de follow-up
POST   /api/v1/leads/{id}/message       # Enviar mensaje manual a lead
```

### Tareas
| Tarea | Estimación |
|---|---|
| DDL: tablas #32-34 + RLS | 1 día |
| Sequence builder API (CRUD con steps: message, wait, condition, task) | 3 días |
| Sequence engine: Celery Beat → ejecutar paso → avanzar | 3 días |
| AI message generation: LangGraph con RAG + few-shot del tenant | 3 días |
| Smart timing: mejor hora basada en historial de respuestas | 2 días |
| Multi-canal dispatch (WhatsApp > Email > Instagram > SMS) | 2 días |
| Response detection: positiva, negativa, pregunta, OOO, bounced | 2 días |
| Auto-actions on response (positiva→agendar, negativa→pausar) | 1 día |
| Kanban Follow-up API | 1 día |
| Templates de secuencia pre-hechas | 1 día |
| Tests unit + integration + mock canales | 2 días |

**Total: ~21 días (2 devs = ~11 días calendario)**

---

## Sprint 19: Scheduling + Voice + Deals + Analytics

### Objetivo
Cierre del ciclo — agendamiento, llamadas (IA + humanas), deals y medición.

### Tablas nuevas
- `deals` (#35) — Oportunidades de venta con valor y probabilidad
- `scheduled_calls` (#36) — Llamadas agendadas (IA o humanas)

### Endpoints API
```
CRUD   /api/v1/deals                    # CRUD de deals
PATCH  /api/v1/deals/{id}/stage         # Mover deal de etapa
GET    /api/v1/deals/pipeline           # Pipeline visual
CRUD   /api/v1/scheduled-calls          # CRUD de llamadas
POST   /api/v1/scheduled-calls/{id}/confirm   # Confirmar llamada
POST   /api/v1/scheduled-calls/{id}/initiate  # Iniciar llamada IA
GET    /api/v1/analytics/leads          # Métricas de leads
GET    /api/v1/analytics/pipeline       # Pipeline health
GET    /api/v1/analytics/revenue        # Revenue tracking
GET    /api/v1/analytics/channels       # Performance por canal
```

### Tareas
| Tarea | Estimación |
|---|---|
| DDL: tablas #35-36 + RLS | 1 día |
| Scheduling API + integración Google Calendar | 2 días |
| Timezone-aware booking (slots por timezone del lead) | 2 días |
| Confirmaciones automáticas WhatsApp/Email 24h + 1h antes | 1 día |
| Vapi.ai integration (VoiceCallProvider ABC) | 3 días |
| Bland.ai integration (fallback provider) | 2 días |
| Human call scheduling + notificación al agente | 1 día |
| Deal pipeline API + revenue tracking | 2 días |
| Deal ↔ Lead sync automático | 1 día |
| Analytics API (pipeline health, conversion, revenue, canales) | 3 días |
| Analytics aggregation: Celery task diario pre-cálculo | 2 días |
| Telegram alerts: deal cerrado > umbral | 1 día |
| Tests + QA final del flujo Lead→Deal→Close | 3 días |

**Total: ~24 días (2 devs = 12 días calendario)**

---

## DDL Completo

### Enums nuevos
```sql
CREATE TYPE lead_stage_type AS ENUM (
  'new', 'enriched', 'qualified', 'assigned',
  'follow_up', 'meeting_scheduled', 'proposal',
  'negotiation', 'won', 'lost', 'disqualified'
);

CREATE TYPE lead_source_type AS ENUM (
  'web_form', 'linkedin', 'facebook_ad', 'google_ad',
  'instagram', 'referral', 'manual', 'api', 'whatsapp', 'import'
);

CREATE TYPE deal_stage AS ENUM (
  'new_contact', 'qualified', 'proposal', 'negotiation',
  'closed_won', 'closed_lost'
);

CREATE TYPE call_type AS ENUM ('ai_voice', 'human', 'hybrid');
CREATE TYPE call_status AS ENUM (
  'pending', 'confirmed', 'in_progress', 'completed',
  'no_show', 'cancelled', 'rescheduled'
);
```

### Tabla #27: lead_pipeline_stages
```sql
CREATE TABLE lead_pipeline_stages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL REFERENCES clients(id),
  name VARCHAR(100) NOT NULL,
  slug VARCHAR(50) NOT NULL,
  position INTEGER NOT NULL,
  color VARCHAR(7),
  auto_actions JSONB DEFAULT '{}',
  is_terminal BOOLEAN DEFAULT false,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(client_id, slug),
  UNIQUE(client_id, position)
);
ALTER TABLE lead_pipeline_stages ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON lead_pipeline_stages
  USING (client_id = current_setting('app.current_client_id')::uuid);
```

### Tabla #28: leads
```sql
CREATE TABLE leads (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL REFERENCES clients(id),
  contact_id UUID REFERENCES contacts(id),
  source_id UUID REFERENCES lead_sources(id),
  assigned_user_id UUID REFERENCES users(id),
  pipeline_stage_id UUID REFERENCES lead_pipeline_stages(id),
  first_name VARCHAR(100),
  last_name VARCHAR(100),
  email VARCHAR(255),
  phone VARCHAR(50),
  linkedin_url VARCHAR(500),
  company_name VARCHAR(200),
  company_domain VARCHAR(255),
  company_size VARCHAR(50),
  industry VARCHAR(100),
  job_title VARCHAR(150),
  fit_score INTEGER DEFAULT 0 CHECK (fit_score BETWEEN 0 AND 100),
  behavioral_score INTEGER DEFAULT 0 CHECK (behavioral_score BETWEEN 0 AND 100),
  ai_score INTEGER DEFAULT 0 CHECK (ai_score BETWEEN 0 AND 100),
  total_score INTEGER GENERATED ALWAYS AS (
    (fit_score * 40 + behavioral_score * 30 + ai_score * 30) / 100
  ) STORED,
  status VARCHAR(20) DEFAULT 'active',
  temperature VARCHAR(10) DEFAULT 'cold',
  estimated_value NUMERIC(12,2),
  currency VARCHAR(3) DEFAULT 'USD',
  last_activity_at TIMESTAMPTZ,
  next_follow_up_at TIMESTAMPTZ,
  converted_at TIMESTAMPTZ,
  disqualified_at TIMESTAMPTZ,
  disqualified_reason TEXT,
  enrichment_data JSONB DEFAULT '{}',
  enriched_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_leads_client_stage ON leads(client_id, pipeline_stage_id);
CREATE INDEX idx_leads_client_score ON leads(client_id, total_score DESC);
CREATE INDEX idx_leads_client_next_followup ON leads(client_id, next_follow_up_at)
  WHERE status = 'active';
CREATE INDEX idx_leads_assigned ON leads(assigned_user_id, client_id)
  WHERE status = 'active';
ALTER TABLE leads ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON leads
  USING (client_id = current_setting('app.current_client_id')::uuid);
```

### Tabla #29: lead_sources
```sql
CREATE TABLE lead_sources (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL REFERENCES clients(id),
  name VARCHAR(100) NOT NULL,
  source_type lead_source_type NOT NULL,
  config JSONB DEFAULT '{}',
  utm_tracking JSONB DEFAULT '{}',
  is_active BOOLEAN DEFAULT true,
  leads_count INTEGER DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE lead_sources ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON lead_sources
  USING (client_id = current_setting('app.current_client_id')::uuid);
```

### Tabla #30: lead_activities
```sql
CREATE TABLE lead_activities (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  client_id UUID NOT NULL REFERENCES clients(id),
  user_id UUID REFERENCES users(id),
  activity_type VARCHAR(50) NOT NULL,
  description TEXT,
  metadata JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_lead_activities_lead ON lead_activities(lead_id, created_at DESC);
ALTER TABLE lead_activities ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON lead_activities
  USING (client_id = current_setting('app.current_client_id')::uuid);
```

### Tabla #31: lead_scores
```sql
CREATE TABLE lead_scores (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  client_id UUID NOT NULL REFERENCES clients(id),
  score_type VARCHAR(20) NOT NULL,
  score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
  factors JSONB NOT NULL,
  scored_at TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE lead_scores ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON lead_scores
  USING (client_id = current_setting('app.current_client_id')::uuid);
```

### Tabla #32: lead_sequences
```sql
CREATE TABLE lead_sequences (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL REFERENCES clients(id),
  name VARCHAR(200) NOT NULL,
  description TEXT,
  trigger_conditions JSONB NOT NULL,
  channel_priority JSONB DEFAULT '["whatsapp", "email", "instagram"]',
  is_active BOOLEAN DEFAULT true,
  enrolled_count INTEGER DEFAULT 0,
  completed_count INTEGER DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE lead_sequences ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON lead_sequences
  USING (client_id = current_setting('app.current_client_id')::uuid);
```

### Tabla #33: lead_sequence_steps
```sql
CREATE TABLE lead_sequence_steps (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  sequence_id UUID NOT NULL REFERENCES lead_sequences(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  step_type VARCHAR(30) NOT NULL,
  config JSONB NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(sequence_id, position)
);
```

### Tabla #34: lead_sequence_enrollments
```sql
CREATE TABLE lead_sequence_enrollments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  sequence_id UUID NOT NULL REFERENCES lead_sequences(id),
  client_id UUID NOT NULL REFERENCES clients(id),
  current_step INTEGER DEFAULT 1,
  status VARCHAR(20) DEFAULT 'active',
  enrolled_at TIMESTAMPTZ DEFAULT now(),
  next_step_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  exit_reason TEXT,
  UNIQUE(lead_id, sequence_id)
);
CREATE INDEX idx_enrollments_next_step ON lead_sequence_enrollments(next_step_at)
  WHERE status = 'active';
ALTER TABLE lead_sequence_enrollments ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON lead_sequence_enrollments
  USING (client_id = current_setting('app.current_client_id')::uuid);
```

### Tabla #35: deals
```sql
CREATE TABLE deals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL REFERENCES clients(id),
  lead_id UUID NOT NULL REFERENCES leads(id),
  assigned_user_id UUID REFERENCES users(id),
  title VARCHAR(300) NOT NULL,
  value NUMERIC(12,2) NOT NULL DEFAULT 0,
  currency VARCHAR(3) DEFAULT 'USD',
  stage deal_stage DEFAULT 'new_contact',
  probability INTEGER DEFAULT 0 CHECK (probability BETWEEN 0 AND 100),
  expected_close_date DATE,
  actual_close_date DATE,
  won_at TIMESTAMPTZ,
  lost_at TIMESTAMPTZ,
  lost_reason TEXT,
  notes TEXT,
  metadata JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_deals_client_stage ON deals(client_id, stage);
CREATE INDEX idx_deals_client_value ON deals(client_id, value DESC)
  WHERE stage NOT IN ('closed_lost');
ALTER TABLE deals ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON deals
  USING (client_id = current_setting('app.current_client_id')::uuid);
```

### Tabla #36: scheduled_calls
```sql
CREATE TABLE scheduled_calls (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL REFERENCES clients(id),
  lead_id UUID NOT NULL REFERENCES leads(id),
  deal_id UUID REFERENCES deals(id),
  assigned_user_id UUID REFERENCES users(id),
  call_type call_type NOT NULL DEFAULT 'human',
  status call_status DEFAULT 'pending',
  scheduled_at TIMESTAMPTZ NOT NULL,
  duration_minutes INTEGER DEFAULT 30,
  timezone VARCHAR(50) DEFAULT 'America/Bogota',
  ai_voice_provider VARCHAR(30),
  ai_voice_config JSONB,
  recording_url TEXT,
  transcript TEXT,
  outcome VARCHAR(30),
  notes TEXT,
  reminder_sent_at TIMESTAMPTZ,
  confirmed_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_calls_upcoming ON scheduled_calls(scheduled_at)
  WHERE status IN ('pending', 'confirmed');
ALTER TABLE scheduled_calls ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON scheduled_calls
  USING (client_id = current_setting('app.current_client_id')::uuid);
```

---

## Integraciones Externas Nuevas

| Servicio | Propósito | Sprint | Variables .env |
|---|---|---|---|
| Clearbit / Apollo.io | Enrichment de empresas y contactos | 17 | `CLEARBIT_API_KEY` |
| Hunter.io | Verificación de emails | 17 | `HUNTER_API_KEY` |
| Vapi.ai | Llamadas con voz IA | 19 | `VAPI_API_KEY`, `VAPI_PHONE_NUMBER` |
| Bland.ai | Llamadas voz IA (alternativa) | 19 | `BLAND_AI_API_KEY`, `BLAND_AI_PHONE_NUMBER` |
| Phantombuster / RapidAPI | Scraping LinkedIn | 16-17 | `PHANTOMBUSTER_API_KEY` |
| Facebook/Google Ads API | Captura leads desde campañas | 16 | `FB_ADS_ACCESS_TOKEN`, `GOOGLE_ADS_API_KEY` |

---

## Theming

### Paleta Dark + Orange (principal)
```css
--bg-primary: #0A0E1A;
--bg-secondary: #111827;
--bg-tertiary: #1F2937;
--accent: #FF6B00;
--accent-light: #FF8A3D;
--accent-subtle: rgba(255,107,0,0.15);
--text-primary: #F9FAFB;
--text-secondary: #9CA3AF;
--success: #10B981;
--warning: #F59E0B;
--danger: #EF4444;
```

### Paleta Light
```css
--bg-primary: #FFFFFF;
--bg-secondary: #F9FAFB;
--bg-tertiary: #F3F4F6;
--accent: #EA580C;
--text-primary: #111827;
--text-secondary: #4B5563;
```

### Implementación
- `next-themes` + CSS custom properties + `[data-theme]`
- `clients.theme_config` JSONB: `mode`, `accent`, `variant`
- Super admin configura theme default por tenant
