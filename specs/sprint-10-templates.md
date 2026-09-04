# Sprint 10 — Templates, Clonación & Sentimiento (Fase 2)

## Objetivo

Implementar un sistema de templates que permita clonar la configuración de un tenant existente para crear nuevos tenants rápidamente, e integrar análisis de sentimiento en el grafo de LangGraph para escalar conversaciones con contactos negativos automáticamente.

## Prerequisitos

- Sprint 9 completado (todos los canales operativos).
- Tablas `tenant_templates` definidas en el esquema de Fase 2 (Sprint 1). Si no existen, crear migración.
- LangGraph grafo funcional con intent routing y nodos RAG, scheduling y handoff.
- Supabase Storage configurado para almacenar documentos por tenant (bucket por `client_id`).
- GPT-4o-mini disponible via API para análisis de sentimiento.
- Celery con cola `bulk` configurada para operaciones pesadas.

## Archivos a Crear/Modificar

| Archivo | Acción | Descripción |
|---|---|---|
| `app/api/v1/admin.py` | Modificar | Agregar endpoints CRUD de templates |
| `app/services/tenant_cloner.py` | Crear | Servicio de clonación de tenants |
| `app/agents/nodes/sentiment.py` | Crear | Nodo de análisis de sentimiento para LangGraph |
| `app/tasks/tenant_operations.py` | Crear | Tasks Celery para clonación async |
| `app/agents/graph.py` | Modificar | Agregar nodo sentiment_analysis al grafo |
| `app/models/tenant_template.py` | Crear | Modelo SQLAlchemy para tenant_templates |
| `app/schemas/template.py` | Crear | Schemas Pydantic para templates |
| `app/schemas/sentiment.py` | Crear | Schema para resultado de sentimiento |
| `migrations/versions/xxx_tenant_templates.py` | Crear | Migración para tabla tenant_templates |
| `tests/unit/test_tenant_cloner.py` | Crear | Tests unitarios del servicio de clonación |
| `tests/unit/test_sentiment_analysis.py` | Crear | Tests del nodo de sentimiento |
| `tests/integration/test_template_flow.py` | Crear | Test de flujo completo de clonación |

## Tareas Detalladas

### 1. Modelo tenant_templates — `app/models/tenant_template.py`

**1.1 Tabla tenant_templates**

```python
# app/models/tenant_template.py
from sqlalchemy import Column, String, Text, DateTime, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.models.base import Base, TimestampMixin
import uuid
from enum import Enum

class InstantiationStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

class TenantTemplate(Base, TimestampMixin):
    __tablename__ = "tenant_templates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    source_client_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    config = Column(JSONB, nullable=False)  # Snapshot completo de la configuración
    created_by = Column(UUID(as_uuid=True), nullable=False)
    is_public = Column(Boolean, default=False)  # ¿Visible para otros super_admins?
    version = Column(String(20), default="1.0")

    # Estado de instanciaciones en curso
    # (Se trackea por instanciación, no en el template)
```

**1.2 Migración**

```sql
CREATE TABLE tenant_templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    description TEXT,
    source_client_id UUID NOT NULL REFERENCES clients(id),
    config JSONB NOT NULL,
    created_by UUID NOT NULL REFERENCES users(id),
    is_public BOOLEAN DEFAULT FALSE,
    version VARCHAR(20) DEFAULT '1.0',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Tabla auxiliar para trackear instanciaciones
CREATE TABLE template_instantiations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    template_id UUID NOT NULL REFERENCES tenant_templates(id),
    target_client_id UUID REFERENCES clients(id),  -- NULL hasta que se crea el client
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending, processing, completed, failed
    error_message TEXT,
    progress JSONB DEFAULT '{}',  -- {"documents_processed": 5, "documents_total": 10}
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_template_instantiations_status ON template_instantiations(status);
```

### 2. CRUD de Templates — Endpoints

**2.1 POST /api/v1/admin/templates — Crear template (snapshot)**

```python
@router.post("/admin/templates", status_code=201)
async def create_template(
    data: TemplateCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["super_admin"])),
):
    """
    Crear template desde un tenant activo.
    Toma un snapshot de toda la configuración del tenant.
    """
    source_client = await db.get(Client, data.source_client_id)
    if not source_client:
        raise HTTPException(404, "Tenant origen no encontrado")

    # Generar snapshot
    config_snapshot = await tenant_cloner.create_snapshot(db, data.source_client_id)

    template = TenantTemplate(
        name=data.name,
        description=data.description,
        source_client_id=data.source_client_id,
        config=config_snapshot,
        created_by=current_user.id,
        is_public=data.is_public,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)

    return template
```

**2.2 GET /api/v1/admin/templates — Listar templates**

```python
@router.get("/admin/templates")
async def list_templates(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "super_admin"])),
):
    """Listar templates disponibles."""
    query = select(TenantTemplate)
    if current_user.role != "super_admin":
        # Admin solo ve templates públicos o del propio tenant
        query = query.where(
            or_(
                TenantTemplate.is_public == True,
                TenantTemplate.source_client_id == current_user.client_id,
            )
        )
    query = query.order_by(TenantTemplate.created_at.desc())
    result = await db.execute(query)
    return result.scalars().all()
```

**2.3 POST /api/v1/admin/templates/{id}/instantiate — Crear tenant desde template**

```python
@router.post("/admin/templates/{template_id}/instantiate", status_code=202)
async def instantiate_template(
    template_id: UUID,
    data: TemplateInstantiate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["super_admin"])),
):
    """
    Crear nuevo tenant desde template.
    La operación es async (Celery) porque regenerar embeddings es costoso.
    Retorna 202 Accepted con el ID de instanciación para tracking.
    """
    template = await db.get(TenantTemplate, template_id)
    if not template:
        raise HTTPException(404, "Template no encontrado")

    # Crear registro de instanciación
    instantiation = TemplateInstantiation(
        template_id=template_id,
        status="pending",
    )
    db.add(instantiation)
    await db.commit()

    # Lanzar task async
    clone_tenant_from_template.delay(
        template_id=str(template_id),
        instantiation_id=str(instantiation.id),
        new_tenant_name=data.tenant_name,
        admin_email=data.admin_email,
        custom_overrides=data.overrides,  # Opcional: sobrescribir configs específicas
    )

    return {
        "instantiation_id": str(instantiation.id),
        "status": "pending",
        "message": "Clonación iniciada. Consultar progreso en GET /admin/templates/instantiations/{id}",
    }
```

**2.4 GET /api/v1/admin/templates/instantiations/{id} — Estado de instanciación**

```python
@router.get("/admin/templates/instantiations/{instantiation_id}")
async def get_instantiation_status(
    instantiation_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["super_admin"])),
):
    """Consultar estado de una instanciación en curso."""
    inst = await db.get(TemplateInstantiation, instantiation_id)
    if not inst:
        raise HTTPException(404, "Instanciación no encontrada")
    return inst
```

### 3. Servicio de Clonación — `app/services/tenant_cloner.py`

**3.1 Snapshot (serialización)**

```python
# app/services/tenant_cloner.py
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

class TenantCloner:
    """Servicio para crear snapshots y clonar tenants."""

    async def create_snapshot(self, db: AsyncSession, client_id: UUID) -> dict:
        """
        Crear snapshot JSON de toda la configuración clonable de un tenant.
        NO incluye: datos de contactos/conversaciones, embeddings, audit_logs.
        SÍ incluye: agent_configs, quick_replies, documents (metadata), tags, channel_configs.
        """
        snapshot = {
            "version": "1.0",
            "snapshot_date": datetime.utcnow().isoformat(),
            "source_client_id": str(client_id),
        }

        # 1. Agent configs
        agent_configs = await db.execute(
            select(AgentConfig).where(AgentConfig.client_id == client_id)
        )
        snapshot["agent_configs"] = [
            {
                "agent_type": ac.agent_type,
                "model": ac.model,
                "system_prompt": ac.system_prompt,
                "temperature": ac.temperature,
                "max_tokens": ac.max_tokens,
                "tools_enabled": ac.tools_enabled,
                "settings": ac.settings,
            }
            for ac in agent_configs.scalars()
        ]

        # 2. Quick replies
        quick_replies = await db.execute(
            select(QuickReply).where(QuickReply.client_id == client_id)
        )
        snapshot["quick_replies"] = [
            {
                "shortcut": qr.shortcut,
                "title": qr.title,
                "content": qr.content,
                "category": qr.category,
            }
            for qr in quick_replies.scalars()
        ]

        # 3. Documents (metadata, NO embeddings)
        documents = await db.execute(
            select(Document).where(Document.client_id == client_id)
        )
        snapshot["documents"] = [
            {
                "title": doc.title,
                "file_name": doc.file_name,
                "file_path": doc.file_path,  # Ruta en Supabase Storage
                "content_type": doc.content_type,
                "chunk_config": doc.chunk_config,
                "metadata": doc.metadata,
            }
            for doc in documents.scalars()
        ]

        # 4. Tags
        tags = await db.execute(
            select(Tag).where(Tag.client_id == client_id)
        )
        snapshot["tags"] = [
            {"name": tag.name, "color": tag.color, "description": tag.description}
            for tag in tags.scalars()
        ]

        # 5. Channel configs (sin secrets)
        channel_configs = await db.execute(
            select(ChannelConfig).where(ChannelConfig.client_id == client_id)
        )
        snapshot["channel_configs"] = [
            {
                "channel": cc.channel,
                "is_active": cc.is_active,
                # provider_config se copia SIN secrets (tokens, passwords)
                "provider_config_template": {
                    k: v for k, v in cc.provider_config.items()
                    if k not in ("access_token", "api_key", "password", "secret", "bot_token")
                },
            }
            for cc in channel_configs.scalars()
        ]

        # 6. Token budget config
        client = await db.get(Client, client_id)
        snapshot["client_settings"] = {
            "token_budget_monthly": client.token_budget_monthly,
            "settings": client.settings,
        }

        return snapshot
```

**3.2 Instanciación (deserialización + creación)**

```python
    async def instantiate(
        self,
        db: AsyncSession,
        template: TenantTemplate,
        new_tenant_name: str,
        admin_email: str,
        overrides: dict = None,
    ) -> UUID:
        """
        Crear nuevo tenant desde un template.
        Retorna el client_id del nuevo tenant.
        """
        config = template.config

        # 1. Crear nuevo client
        new_client = Client(
            name=new_tenant_name,
            token_budget_monthly=config["client_settings"]["token_budget_monthly"],
            settings=config["client_settings"]["settings"],
            is_active=True,
        )
        db.add(new_client)
        await db.flush()  # Obtener ID antes de commit

        new_client_id = new_client.id

        # 2. Crear usuario admin
        admin_user = User(
            client_id=new_client_id,
            email=admin_email,
            role="admin",
            # Password temporal o invitación por email
        )
        db.add(admin_user)

        # 3. Copiar agent configs
        for ac_config in config["agent_configs"]:
            if overrides and "agent_configs" in overrides:
                # Aplicar overrides si existen
                ac_config = {**ac_config, **overrides.get("agent_configs", {}).get(ac_config["agent_type"], {})}

            agent_config = AgentConfig(
                client_id=new_client_id,
                **ac_config,
            )
            db.add(agent_config)

        # 4. Copiar quick replies
        for qr_data in config["quick_replies"]:
            quick_reply = QuickReply(
                client_id=new_client_id,
                **qr_data,
            )
            db.add(quick_reply)

        # 5. Copiar tags
        for tag_data in config["tags"]:
            tag = Tag(
                client_id=new_client_id,
                **tag_data,
            )
            db.add(tag)

        # 6. Copiar channel configs (SIN secrets — se deben configurar manualmente)
        for cc_data in config["channel_configs"]:
            channel_config = ChannelConfig(
                client_id=new_client_id,
                channel=cc_data["channel"],
                is_active=False,  # Inactivo hasta configurar secrets
                provider_config=cc_data["provider_config_template"],
            )
            db.add(channel_config)

        await db.commit()
        return new_client_id

    async def clone_documents(self, db: AsyncSession, template: TenantTemplate, new_client_id: UUID):
        """
        Copiar documentos del template al nuevo tenant.
        Los archivos se copian en Supabase Storage y los embeddings se REGENERAN.
        """
        config = template.config
        source_client_id = config["source_client_id"]

        for doc_data in config["documents"]:
            # 1. Copiar archivo en Supabase Storage
            source_path = doc_data["file_path"]
            new_path = source_path.replace(source_client_id, str(new_client_id))

            await supabase_storage.copy_file(
                source_bucket=f"documents-{source_client_id}",
                source_path=source_path,
                dest_bucket=f"documents-{new_client_id}",
                dest_path=new_path,
            )

            # 2. Crear registro de documento
            document = Document(
                client_id=new_client_id,
                title=doc_data["title"],
                file_name=doc_data["file_name"],
                file_path=new_path,
                content_type=doc_data["content_type"],
                chunk_config=doc_data["chunk_config"],
                metadata=doc_data["metadata"],
                indexing_status="pending",  # Se indexará en el siguiente paso
            )
            db.add(document)

        await db.commit()

        # 3. Regenerar embeddings (NO copiar — son modelo-dependientes)
        from app.tasks.document_processing import reindex_all_documents
        reindex_all_documents.delay(str(new_client_id))
```

### 4. Task Celery — `app/tasks/tenant_operations.py`

```python
# app/tasks/tenant_operations.py
from app.core.celery_app import celery_app
from app.services.tenant_cloner import TenantCloner

@celery_app.task(queue="bulk", bind=True, max_retries=1)
async def clone_tenant_from_template(
    self,
    template_id: str,
    instantiation_id: str,
    new_tenant_name: str,
    admin_email: str,
    custom_overrides: dict = None,
):
    """
    Task async para clonar un tenant desde template.
    Se ejecuta en la cola 'bulk' porque regenerar embeddings es costoso.
    """
    cloner = TenantCloner()

    async with get_db_session() as db:
        try:
            # Actualizar estado
            instantiation = await db.get(TemplateInstantiation, instantiation_id)
            instantiation.status = "processing"
            instantiation.started_at = datetime.utcnow()
            await db.commit()

            # Obtener template
            template = await db.get(TenantTemplate, template_id)
            if not template:
                raise ValueError(f"Template {template_id} no encontrado")

            # 1. Crear tenant con configs
            new_client_id = await cloner.instantiate(
                db, template, new_tenant_name, admin_email, custom_overrides
            )

            # Actualizar instanciación con client_id
            instantiation.target_client_id = new_client_id
            instantiation.progress = {"step": "configs_copied", "documents_processed": 0}
            await db.commit()

            # 2. Copiar documentos y regenerar embeddings
            await cloner.clone_documents(db, template, new_client_id)

            # 3. Actualizar progreso (los embeddings se regeneran async)
            instantiation.progress = {"step": "documents_queued"}
            await db.commit()

            # 4. Esperar a que se completen los embeddings
            # (Esto se puede hacer con un callback o polling)
            # Por simplicidad, marcar como completado cuando los docs están encolados
            instantiation.status = "completed"
            instantiation.completed_at = datetime.utcnow()
            await db.commit()

        except Exception as exc:
            instantiation.status = "failed"
            instantiation.error_message = str(exc)
            await db.commit()
            raise
```

### 5. Análisis de Sentimiento — `app/agents/nodes/sentiment.py`

**5.1 Nodo de sentimiento**

```python
# app/agents/nodes/sentiment.py
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from enum import Enum
from app.agents.state import ConversationState

class SentimentLevel(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    VERY_NEGATIVE = "very_negative"

class SentimentResult(BaseModel):
    """Resultado del análisis de sentimiento."""
    sentiment: SentimentLevel = Field(description="Nivel de sentimiento detectado")
    score: float = Field(ge=0.0, le=1.0, description="Confianza del análisis (0-1)")
    reasoning: str = Field(description="Breve explicación del sentimiento detectado")

SENTIMENT_PROMPT = """Analiza el sentimiento del siguiente mensaje de un usuario en una conversación de soporte.

Clasifica el sentimiento en una de estas categorías:
- positive: El usuario está satisfecho, agradecido o contento.
- neutral: El usuario hace una consulta sin carga emocional.
- negative: El usuario está insatisfecho, frustrado o molesto.
- very_negative: El usuario está muy enfadado, usa lenguaje agresivo, amenaza o exige hablar con una persona.

Mensaje del usuario: {message}

Contexto de la conversación (últimos mensajes):
{context}

Responde SOLO con el JSON estructurado."""

async def sentiment_analysis_node(state: ConversationState) -> ConversationState:
    """
    Nodo de análisis de sentimiento en el grafo LangGraph.
    Ubicación: después de intent_routing, antes del nodo destino.
    """
    # Obtener el mensaje más reciente del usuario
    last_user_message = state.get("last_user_message", "")

    # Contexto: últimos 3 mensajes de la conversación
    recent_messages = state.get("messages", [])[-6:]  # 3 pares user/bot
    context = "\n".join([f"{'Usuario' if m['role'] == 'user' else 'Bot'}: {m['content']}" for m in recent_messages])

    # Usar GPT-4o-mini con structured output para minimizar costo
    llm = ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0,
    ).with_structured_output(SentimentResult)

    result = await llm.ainvoke(
        SENTIMENT_PROMPT.format(message=last_user_message, context=context)
    )

    # Almacenar resultado en el state
    state["current_sentiment"] = result.sentiment.value
    state["sentiment_score"] = result.score

    # Guardar en metadata del mensaje
    state["message_metadata"] = {
        **(state.get("message_metadata") or {}),
        "sentiment": {
            "level": result.sentiment.value,
            "score": result.score,
            "reasoning": result.reasoning,
        },
    }

    # Regla de escalamiento: 2 mensajes very_negative consecutivos → handoff
    consecutive_negative = state.get("consecutive_very_negative", 0)
    if result.sentiment == SentimentLevel.VERY_NEGATIVE:
        consecutive_negative += 1
    else:
        consecutive_negative = 0  # Reset si no es very_negative

    state["consecutive_very_negative"] = consecutive_negative

    # Decidir si escalar
    if consecutive_negative >= 2:
        state["force_handoff"] = True
        state["handoff_reason"] = "negative_sentiment"
        state["next_node"] = "human_handoff"

    return state
```

**5.2 Integración en ConversationState**

Agregar campos al estado de la conversación:

```python
# En app/agents/state.py (agregar campos)
class ConversationState(TypedDict):
    # ... campos existentes
    current_sentiment: Optional[str]  # positive, neutral, negative, very_negative
    sentiment_score: Optional[float]  # 0.0 - 1.0
    consecutive_very_negative: int  # Contador de mensajes very_negative consecutivos
    force_handoff: bool  # True si se debe forzar handoff
    handoff_reason: Optional[str]  # Razón del handoff forzado
```

### 6. Integración en el Grafo — Modificar `app/agents/graph.py`

**6.1 Nuevo flujo del grafo**

```
START → token_budget_check → intent_routing → sentiment_analysis → [router]
                                                                        ├── rag_query (si intent = general/faq)
                                                                        ├── scheduling (si intent = appointment)
                                                                        ├── human_handoff (si intent = handoff O force_handoff)
                                                                        └── respond (si intent = greeting/farewell)
```

**6.2 Implementación**

```python
# En app/agents/graph.py
from app.agents.nodes.sentiment import sentiment_analysis_node

def build_graph(agent_config: AgentConfig) -> StateGraph:
    graph = StateGraph(ConversationState)

    # Nodos
    graph.add_node("token_budget_check", token_budget_check_node)
    graph.add_node("intent_routing", intent_routing_node)
    graph.add_node("sentiment_analysis", sentiment_analysis_node)  # NUEVO
    graph.add_node("rag_query", rag_query_node)
    graph.add_node("scheduling", scheduling_node)
    graph.add_node("human_handoff", human_handoff_node)
    graph.add_node("respond", respond_node)

    # Edges
    graph.set_entry_point("token_budget_check")
    graph.add_edge("token_budget_check", "intent_routing")
    graph.add_edge("intent_routing", "sentiment_analysis")  # NUEVO

    # Router condicional después de sentiment
    def route_after_sentiment(state: ConversationState) -> str:
        # Si sentiment fuerza handoff, ir directo
        if state.get("force_handoff"):
            return "human_handoff"
        # Si no, seguir el intent routing original
        return state.get("next_node", "rag_query")

    graph.add_conditional_edges(
        "sentiment_analysis",
        route_after_sentiment,
        {
            "rag_query": "rag_query",
            "scheduling": "scheduling",
            "human_handoff": "human_handoff",
            "respond": "respond",
        }
    )

    # Edges finales
    graph.add_edge("rag_query", "respond")
    graph.add_edge("scheduling", "respond")
    graph.add_edge("respond", END)
    graph.add_edge("human_handoff", END)

    return graph.compile()
```

### 7. Schemas Pydantic — `app/schemas/template.py`

```python
# app/schemas/template.py
from pydantic import BaseModel, EmailStr
from uuid import UUID
from typing import Optional

class TemplateCreate(BaseModel):
    name: str
    description: Optional[str] = None
    source_client_id: UUID
    is_public: bool = False

class TemplateInstantiate(BaseModel):
    tenant_name: str
    admin_email: EmailStr
    overrides: Optional[dict] = None  # Overrides de configuración

class TemplateResponse(BaseModel):
    id: UUID
    name: str
    description: Optional[str]
    source_client_id: UUID
    is_public: bool
    version: str
    created_at: str

    class Config:
        from_attributes = True

class InstantiationResponse(BaseModel):
    id: UUID
    template_id: UUID
    target_client_id: Optional[UUID]
    status: str
    progress: Optional[dict]
    error_message: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]

    class Config:
        from_attributes = True
```

### 8. Tests

**8.1 Tests unitarios de clonación — `tests/unit/test_tenant_cloner.py`**

```python
import pytest
from app.services.tenant_cloner import TenantCloner

class TestTenantCloner:
    @pytest.fixture
    def cloner(self):
        return TenantCloner()

    async def test_create_snapshot(self, cloner, db, test_tenant_with_configs):
        """Snapshot incluye todas las configs esperadas."""
        snapshot = await cloner.create_snapshot(db, test_tenant_with_configs.id)

        assert "agent_configs" in snapshot
        assert "quick_replies" in snapshot
        assert "documents" in snapshot
        assert "tags" in snapshot
        assert "channel_configs" in snapshot
        assert "client_settings" in snapshot

        # Verificar que NO incluye secrets
        for cc in snapshot["channel_configs"]:
            assert "access_token" not in cc.get("provider_config_template", {})
            assert "api_key" not in cc.get("provider_config_template", {})

    async def test_snapshot_excludes_contacts_and_conversations(self, cloner, db, test_tenant_with_data):
        """Snapshot NO incluye datos de contactos ni conversaciones."""
        snapshot = await cloner.create_snapshot(db, test_tenant_with_data.id)
        assert "contacts" not in snapshot
        assert "conversations" not in snapshot
        assert "messages" not in snapshot

    async def test_instantiate_creates_new_tenant(self, cloner, db, test_template):
        """Instanciación crea un nuevo tenant con las configs del template."""
        new_client_id = await cloner.instantiate(
            db, test_template, "Nuevo Tenant", "admin@nuevo.com"
        )

        # Verificar client creado
        client = await db.get(Client, new_client_id)
        assert client is not None
        assert client.name == "Nuevo Tenant"

        # Verificar agent configs copiadas
        configs = await db.execute(
            select(AgentConfig).where(AgentConfig.client_id == new_client_id)
        )
        assert len(configs.scalars().all()) > 0

    async def test_instantiate_with_overrides(self, cloner, db, test_template):
        """Overrides sobrescriben configs específicas."""
        overrides = {
            "agent_configs": {
                "general": {"system_prompt": "Prompt personalizado para el nuevo tenant"}
            }
        }
        new_client_id = await cloner.instantiate(
            db, test_template, "Override Tenant", "admin@override.com", overrides
        )

        config = await db.execute(
            select(AgentConfig).where(
                AgentConfig.client_id == new_client_id,
                AgentConfig.agent_type == "general",
            )
        )
        assert config.scalar_one().system_prompt == "Prompt personalizado para el nuevo tenant"

    async def test_channel_configs_inactive_without_secrets(self, cloner, db, test_template):
        """Canales clonados están inactivos hasta configurar secrets."""
        new_client_id = await cloner.instantiate(
            db, test_template, "No Secrets", "admin@nosecrets.com"
        )

        channels = await db.execute(
            select(ChannelConfig).where(ChannelConfig.client_id == new_client_id)
        )
        for cc in channels.scalars():
            assert cc.is_active is False
```

**8.2 Tests unitarios de sentimiento — `tests/unit/test_sentiment_analysis.py`**

```python
import pytest
from unittest.mock import AsyncMock, patch
from app.agents.nodes.sentiment import sentiment_analysis_node, SentimentLevel

class TestSentimentAnalysis:
    async def test_positive_sentiment(self):
        """Mensaje positivo no afecta el flujo."""
        state = {
            "last_user_message": "Muchas gracias, me ayudaron mucho!",
            "messages": [],
            "consecutive_very_negative": 0,
        }

        with patch("app.agents.nodes.sentiment.ChatOpenAI") as mock_llm:
            mock_llm.return_value.with_structured_output.return_value.ainvoke = AsyncMock(
                return_value=SentimentResult(
                    sentiment=SentimentLevel.POSITIVE, score=0.95, reasoning="Agradecimiento"
                )
            )
            result = await sentiment_analysis_node(state)

        assert result["current_sentiment"] == "positive"
        assert result.get("force_handoff") is not True
        assert result["consecutive_very_negative"] == 0

    async def test_very_negative_once_no_handoff(self):
        """Un solo very_negative no causa handoff."""
        state = {
            "last_user_message": "¡Esto es inaceptable! ¡Quiero hablar con un gerente!",
            "messages": [],
            "consecutive_very_negative": 0,
        }

        with patch("app.agents.nodes.sentiment.ChatOpenAI") as mock_llm:
            mock_llm.return_value.with_structured_output.return_value.ainvoke = AsyncMock(
                return_value=SentimentResult(
                    sentiment=SentimentLevel.VERY_NEGATIVE, score=0.92, reasoning="Muy frustrado"
                )
            )
            result = await sentiment_analysis_node(state)

        assert result["consecutive_very_negative"] == 1
        assert result.get("force_handoff") is not True

    async def test_very_negative_twice_forces_handoff(self):
        """Dos very_negative consecutivos fuerzan handoff."""
        state = {
            "last_user_message": "¡Son unos incompetentes! ¡Nunca resuelven nada!",
            "messages": [],
            "consecutive_very_negative": 1,  # Ya tenía 1 previo
        }

        with patch("app.agents.nodes.sentiment.ChatOpenAI") as mock_llm:
            mock_llm.return_value.with_structured_output.return_value.ainvoke = AsyncMock(
                return_value=SentimentResult(
                    sentiment=SentimentLevel.VERY_NEGATIVE, score=0.97, reasoning="Insultos, exige persona"
                )
            )
            result = await sentiment_analysis_node(state)

        assert result["consecutive_very_negative"] == 2
        assert result["force_handoff"] is True
        assert result["handoff_reason"] == "negative_sentiment"
        assert result["next_node"] == "human_handoff"

    async def test_neutral_resets_counter(self):
        """Mensaje neutral resetea el contador de very_negative."""
        state = {
            "last_user_message": "Entendido, voy a intentar eso.",
            "messages": [],
            "consecutive_very_negative": 1,
        }

        with patch("app.agents.nodes.sentiment.ChatOpenAI") as mock_llm:
            mock_llm.return_value.with_structured_output.return_value.ainvoke = AsyncMock(
                return_value=SentimentResult(
                    sentiment=SentimentLevel.NEUTRAL, score=0.85, reasoning="Respuesta neutra"
                )
            )
            result = await sentiment_analysis_node(state)

        assert result["consecutive_very_negative"] == 0
        assert result.get("force_handoff") is not True
```

## Criterios de Aceptación

| # | Criterio | Verificación |
|---|---|---|
| 1 | Template creado desde tenant activo con todas las configs | POST /admin/templates → snapshot JSON válido con agent_configs, quick_replies, docs, tags, channels |
| 2 | Snapshot NO incluye secrets de canales | Inspeccionar config.channel_configs → sin access_token, api_key, etc. |
| 3 | Nuevo tenant instanciado desde template | POST /admin/templates/{id}/instantiate → nuevo client_id en DB |
| 4 | Configs copiadas correctamente | Nuevo tenant tiene mismas agent_configs, quick_replies y tags |
| 5 | Documentos copiados en Storage | Archivos en nuevo bucket `documents-{new_client_id}` |
| 6 | Embeddings REGENERADOS (no copiados) | `indexing_status` = "pending" → Celery task ejecuta → embeddings nuevos |
| 7 | Sentimiento positivo/neutral no afecta flujo | Mensaje positivo → flujo normal sin handoff |
| 8 | 2x very_negative → handoff automático | 2 mensajes consecutivos very_negative → `force_handoff` = True |
| 9 | Progreso de instanciación trackeable | GET /admin/templates/instantiations/{id} → status, progress |
| 10 | Sentimiento almacenado en message.metadata | Campo `sentiment` presente en metadata del mensaje |

## Notas Técnicas

- **Clonación async**: La clonación se ejecuta en la cola `bulk` de Celery porque regenerar embeddings puede tomar minutos u horas dependiendo de la cantidad de documentos. El endpoint retorna 202 Accepted.
- **Embeddings NO se copian**: Los embeddings son dependientes del modelo de embedding utilizado. Si el tenant origen usa `text-embedding-ada-002` y el nuevo usa `text-embedding-3-small`, los embeddings serían incompatibles. Por eso siempre se regeneran.
- **Secrets de canales**: Los secrets (tokens, API keys) NUNCA se incluyen en el snapshot. El administrador del nuevo tenant debe configurarlos manualmente después de la instanciación.
- **Archivos en Supabase Storage**: Los archivos se copian a un nuevo bucket (`documents-{new_client_id}`). Si el archivo original fue eliminado, la copia falla con error registrado pero no bloquea el resto de la clonación.
- **Sentimiento con GPT-4o-mini**: Se usa GPT-4o-mini porque el análisis de sentimiento es una tarea de clasificación simple que no requiere un modelo grande. Esto minimiza el costo (aprox. 10x más barato que GPT-4o).
- **RBAC**: Solo `super_admin` puede crear templates e instanciar tenants. `admin` puede ver templates públicos.

## Dependencias

| Dependencia | Versión | Propósito |
|---|---|---|
| langchain-openai | >=0.1.0 | ChatOpenAI con structured output |
| pydantic | >=2.6.0 | Schemas y structured output |
| Celery | (existente) | Cola `bulk` para operaciones pesadas |
| Supabase Storage | (existente) | Almacenamiento y copia de archivos |
