# Sprint 12 — Agentes Financiero & Marketing (Fase 2)

## Objetivo

Agregar agentes especializados al grafo de LangGraph: un agente financiero para facturación electrónica (contexto DIAN Colombia) y un agente de marketing para campañas masivas y segmentación. Además, mejorar el RAG con re-ranking via cross-encoder e implementar scoring predictivo de contactos.

## Prerequisitos

- Sprint 11 completado (webhooks y CSAT funcionales).
- LangGraph grafo funcional con intent routing, sentimiento y nodos base.
- RAG operativo con embeddings y retrieval (Sprint 6).
- `agent_configs` por tenant con estructura de settings flexible (JSONB).
- Celery con cola `bulk` para envíos masivos.
- Familiaridad con la API DIAN para facturación electrónica colombiana (o mock para desarrollo).

## Archivos a Crear/Modificar

| Archivo | Acción | Descripción |
|---|---|---|
| `app/agents/nodes/financial.py` | Crear | Nodo agente financiero para el grafo |
| `app/agents/nodes/marketing.py` | Crear | Nodo agente de marketing |
| `app/agents/tools/invoice_tools.py` | Crear | Tools de facturación electrónica |
| `app/agents/tools/marketing_tools.py` | Crear | Tools de campañas y segmentación |
| `app/services/rag.py` | Modificar | Agregar re-ranking con cross-encoder |
| `app/services/contact_scoring.py` | Crear | Servicio de scoring predictivo |
| `app/api/v1/campaigns.py` | Crear | CRUD de campañas de marketing |
| `app/api/v1/contacts.py` | Modificar | Agregar endpoint de recalcular score |
| `app/models/campaign.py` | Crear | Modelo SQLAlchemy para campañas |
| `app/schemas/campaign.py` | Crear | Schemas Pydantic para campañas |
| `app/agents/graph.py` | Modificar | Agregar nodos financial y marketing |
| `app/agents/nodes/intent_routing.py` | Modificar | Agregar intents financiero y marketing |
| `migrations/versions/xxx_campaigns.py` | Crear | Migración para tabla campaigns |
| `tests/unit/test_financial_agent.py` | Crear | Tests del agente financiero |
| `tests/unit/test_marketing_agent.py` | Crear | Tests del agente de marketing |
| `tests/unit/test_rag_reranking.py` | Crear | Tests del re-ranking |
| `tests/unit/test_contact_scoring.py` | Crear | Tests del scoring predictivo |

## Tareas Detalladas

### 1. Agente Financiero — `app/agents/nodes/financial.py`

**1.1 Definición del nodo**

```python
# app/agents/nodes/financial.py
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from app.agents.state import ConversationState
from app.agents.tools.invoice_tools import (
    validate_nit,
    create_invoice,
    get_invoice_status,
    list_invoices,
)

FINANCIAL_SYSTEM_PROMPT = """Eres un agente financiero especializado en facturación electrónica colombiana.

Tu función es ayudar a los usuarios con:
1. Validación de NIT/RUT
2. Emisión de facturas electrónicas
3. Consulta de estado de facturas
4. Historial de facturación

Reglas:
- Siempre valida el NIT antes de emitir una factura.
- Recopila TODOS los datos necesarios antes de crear la factura (items, impuestos, datos fiscales).
- Confirma con el usuario antes de emitir la factura definitiva.
- Si falta información, pregunta de forma clara y específica.
- Responde en el idioma del usuario.

Datos requeridos para una factura:
- NIT o cédula del comprador
- Razón social o nombre completo
- Items (descripción, cantidad, valor unitario)
- Tipo de IVA (0%, 5%, 19%)
- Dirección de facturación (opcional, según régimen)

Estado actual de la conversación: {conversation_context}
"""

async def financial_agent_node(state: ConversationState) -> ConversationState:
    """Nodo del agente financiero en el grafo LangGraph."""

    # Solo procesar si el agente financiero está habilitado
    agent_config = state.get("agent_configs", {}).get("financial")
    if not agent_config:
        state["response"] = "Lo siento, el módulo de facturación no está habilitado. Contacte al administrador."
        state["next_node"] = "respond"
        return state

    # Construir contexto de la conversación
    context = build_financial_context(state)

    llm = ChatOpenAI(
        model=agent_config.get("model", "gpt-4o"),
        temperature=0.1,
    )

    # Bind tools
    llm_with_tools = llm.bind_tools([
        validate_nit,
        create_invoice,
        get_invoice_status,
        list_invoices,
    ])

    messages = [
        SystemMessage(content=FINANCIAL_SYSTEM_PROMPT.format(conversation_context=context)),
        *state.get("messages", []),
    ]

    # Ejecutar agente con tools (loop de tool calling)
    response = await run_agent_with_tools(llm_with_tools, messages, max_iterations=5)

    state["response"] = response.content
    state["next_node"] = "respond"

    # Registrar tokens usados
    state["tokens_used"] = state.get("tokens_used", 0) + response.usage_metadata.get("total_tokens", 0)

    return state


def build_financial_context(state: ConversationState) -> str:
    """Construir contexto de facturación para el prompt."""
    context_parts = []

    # Datos del contacto si existen
    contact = state.get("contact")
    if contact:
        context_parts.append(f"Contacto: {contact.get('first_name', '')} {contact.get('last_name', '')}")
        if contact.get("metadata", {}).get("nit"):
            context_parts.append(f"NIT registrado: {contact['metadata']['nit']}")

    # Datos de facturación en curso (multi-turno)
    invoice_draft = state.get("invoice_draft")
    if invoice_draft:
        context_parts.append(f"Borrador de factura en curso: {json.dumps(invoice_draft, ensure_ascii=False)}")

    return "\n".join(context_parts) or "Sin contexto previo."
```

**1.2 Tools de facturación — `app/agents/tools/invoice_tools.py`**

```python
# app/agents/tools/invoice_tools.py
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from typing import Optional
import httpx

class InvoiceItem(BaseModel):
    description: str = Field(description="Descripción del producto o servicio")
    quantity: int = Field(ge=1, description="Cantidad")
    unit_price: float = Field(ge=0, description="Precio unitario en COP")
    tax_rate: float = Field(description="Tasa de IVA: 0, 0.05 o 0.19")

@tool
async def validate_nit(nit: str) -> dict:
    """
    Validar un NIT/RUT colombiano contra la base de datos de la DIAN.
    Retorna información del contribuyente si el NIT es válido.

    Args:
        nit: Número de Identificación Tributaria (sin dígito de verificación)
    """
    # Validación de formato
    clean_nit = nit.replace(".", "").replace("-", "").strip()
    if not clean_nit.isdigit() or len(clean_nit) < 6 or len(clean_nit) > 15:
        return {"valid": False, "error": "Formato de NIT inválido"}

    # Calcular dígito de verificación
    verification_digit = calculate_verification_digit(clean_nit)

    # Consultar DIAN API (o mock)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{settings.DIAN_API_URL}/taxpayers/{clean_nit}",
                headers={"Authorization": f"Bearer {settings.DIAN_API_TOKEN}"},
                timeout=10.0,
            )
            if response.status_code == 200:
                data = response.json()
                return {
                    "valid": True,
                    "nit": clean_nit,
                    "verification_digit": verification_digit,
                    "business_name": data.get("razon_social"),
                    "tax_regime": data.get("regimen"),
                    "economic_activity": data.get("actividad_economica"),
                    "address": data.get("direccion"),
                    "city": data.get("ciudad"),
                }
            elif response.status_code == 404:
                return {"valid": False, "error": "NIT no encontrado en la DIAN"}
    except Exception as e:
        # Fallback: solo validar formato y dígito
        return {
            "valid": True,
            "nit": clean_nit,
            "verification_digit": verification_digit,
            "warning": "No se pudo verificar en línea. Formato válido.",
        }


def calculate_verification_digit(nit: str) -> int:
    """Calcular dígito de verificación del NIT colombiano."""
    weights = [3, 7, 13, 17, 19, 23, 29, 37, 41, 43, 47, 53, 59, 67, 71]
    nit_padded = nit.zfill(15)
    total = sum(int(d) * w for d, w in zip(nit_padded, weights))
    remainder = total % 11
    if remainder <= 1:
        return remainder
    return 11 - remainder


@tool
async def create_invoice(
    contact_id: str,
    buyer_nit: str,
    buyer_name: str,
    items: list[dict],
    tax_info: dict = None,
) -> dict:
    """
    Crear una factura electrónica para un contacto.

    Args:
        contact_id: ID del contacto comprador
        buyer_nit: NIT del comprador
        buyer_name: Razón social o nombre del comprador
        items: Lista de items [{description, quantity, unit_price, tax_rate}]
        tax_info: Información fiscal adicional (régimen, retenciones)
    """
    # Calcular totales
    subtotal = sum(item["quantity"] * item["unit_price"] for item in items)
    tax_total = sum(
        item["quantity"] * item["unit_price"] * item.get("tax_rate", 0.19)
        for item in items
    )
    total = subtotal + tax_total

    # Crear factura en el sistema (o enviar a API DIAN)
    invoice_data = {
        "invoice_number": generate_invoice_number(),  # Consecutivo autorizado DIAN
        "buyer_nit": buyer_nit,
        "buyer_name": buyer_name,
        "items": items,
        "subtotal": subtotal,
        "tax_total": tax_total,
        "total": total,
        "currency": "COP",
        "issue_date": datetime.utcnow().isoformat(),
        "status": "draft",  # draft → pending_dian → approved → rejected
    }

    # Enviar a DIAN para validación
    try:
        dian_response = await submit_to_dian(invoice_data)
        invoice_data["dian_cufe"] = dian_response.get("cufe")  # CUFE único
        invoice_data["status"] = "approved" if dian_response.get("success") else "rejected"
        invoice_data["dian_response"] = dian_response
    except Exception as e:
        invoice_data["status"] = "pending_dian"
        invoice_data["error"] = str(e)

    # Guardar en DB
    # ...

    return invoice_data


@tool
async def get_invoice_status(invoice_id: str) -> dict:
    """
    Consultar el estado de una factura electrónica.

    Args:
        invoice_id: ID o número de la factura
    """
    # Buscar en DB
    # Consultar estado en DIAN si está pendiente
    return {
        "invoice_id": invoice_id,
        "invoice_number": "FE-001234",
        "status": "approved",
        "total": 1190000,
        "issue_date": "2025-01-15",
        "dian_cufe": "abc123...",
    }


@tool
async def list_invoices(
    contact_id: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    """
    Listar facturas de un contacto con filtros opcionales.

    Args:
        contact_id: ID del contacto
        date_from: Fecha inicio (YYYY-MM-DD)
        date_to: Fecha fin (YYYY-MM-DD)
        status: Filtrar por estado (draft, approved, rejected)
    """
    # Consultar DB con filtros
    return {
        "contact_id": contact_id,
        "invoices": [],
        "total_count": 0,
        "filters_applied": {
            "date_from": date_from,
            "date_to": date_to,
            "status": status,
        },
    }
```

### 2. Agente de Marketing — `app/agents/nodes/marketing.py`

**2.1 Definición del nodo**

```python
# app/agents/nodes/marketing.py
from app.agents.tools.marketing_tools import (
    create_campaign,
    segment_contacts,
    send_campaign,
    get_campaign_metrics,
)

MARKETING_SYSTEM_PROMPT = """Eres un agente de marketing conversacional.

Tu función es ayudar a los usuarios con:
1. Creación de campañas de mensajería masiva
2. Segmentación de contactos por criterios
3. Envío de campañas
4. Consulta de métricas de campañas

Reglas:
- Siempre confirma el segmento objetivo antes de enviar.
- Los templates de WhatsApp deben estar pre-aprobados por Meta.
- El envío masivo tiene throttling: máximo 100 mensajes/segundo.
- No enviar campañas duplicadas al mismo segmento en menos de 24 horas.
- Mostrar preview del mensaje antes de enviar.

Estado actual: {conversation_context}
"""

async def marketing_agent_node(state: ConversationState) -> ConversationState:
    """Nodo del agente de marketing en el grafo LangGraph."""
    agent_config = state.get("agent_configs", {}).get("marketing")
    if not agent_config:
        state["response"] = "Lo siento, el módulo de marketing no está habilitado."
        state["next_node"] = "respond"
        return state

    llm = ChatOpenAI(
        model=agent_config.get("model", "gpt-4o"),
        temperature=0.2,
    )

    llm_with_tools = llm.bind_tools([
        create_campaign,
        segment_contacts,
        send_campaign,
        get_campaign_metrics,
    ])

    messages = [
        SystemMessage(content=MARKETING_SYSTEM_PROMPT.format(
            conversation_context=build_marketing_context(state)
        )),
        *state.get("messages", []),
    ]

    response = await run_agent_with_tools(llm_with_tools, messages, max_iterations=5)
    state["response"] = response.content
    state["next_node"] = "respond"

    return state
```

**2.2 Tools de marketing — `app/agents/tools/marketing_tools.py`**

```python
# app/agents/tools/marketing_tools.py
from langchain_core.tools import tool

@tool
async def create_campaign(
    name: str,
    segment_criteria: dict,
    message_template: str,
    channel: str = "whatsapp",
    schedule: str = None,
) -> dict:
    """
    Crear una campaña de marketing masivo.

    Args:
        name: Nombre descriptivo de la campaña
        segment_criteria: Criterios de segmentación {tags: [...], metadata: {...}, last_active_days: N}
        message_template: Template del mensaje (con variables {{contact_name}}, etc.)
        channel: Canal de envío (whatsapp, telegram, email, sms)
        schedule: Fecha/hora de envío programado (ISO 8601). Si None, envío inmediato.
    """
    # Validar template de WhatsApp (debe estar pre-aprobado)
    if channel == "whatsapp":
        template_approved = await check_whatsapp_template_approval(message_template)
        if not template_approved:
            return {
                "success": False,
                "error": "El template de WhatsApp no está aprobado por Meta. "
                         "Usa un template pre-aprobado o solicita aprobación.",
            }

    # Contar contactos del segmento
    segment_count = await count_segment(segment_criteria)

    # Crear campaña en DB
    campaign = {
        "id": str(uuid4()),
        "name": name,
        "channel": channel,
        "segment_criteria": segment_criteria,
        "message_template": message_template,
        "target_count": segment_count,
        "status": "draft",  # draft → scheduled → sending → completed → failed
        "schedule": schedule,
        "created_at": datetime.utcnow().isoformat(),
    }

    # Guardar en DB
    # ...

    return {
        "success": True,
        "campaign_id": campaign["id"],
        "name": name,
        "target_count": segment_count,
        "status": "draft",
        "message": f"Campaña '{name}' creada. {segment_count} contactos en el segmento. "
                   "Usa send_campaign para enviar.",
    }


@tool
async def segment_contacts(criteria: dict) -> dict:
    """
    Segmentar contactos por criterios.

    Args:
        criteria: Criterios de segmentación
            - tags: lista de tags (AND logic)
            - metadata: filtros en metadata JSONB
            - last_active_days: contactos activos en los últimos N días
            - channel: filtrar por canal
            - sentiment_avg: filtrar por sentimiento promedio
            - score_min: score mínimo del contacto
    """
    # Construir query dinámica
    query = select(Contact).where(Contact.client_id == current_client_id)

    if criteria.get("tags"):
        # Contactos que tienen TODOS los tags especificados
        for tag_name in criteria["tags"]:
            query = query.filter(
                Contact.tags.any(Tag.name == tag_name)
            )

    if criteria.get("last_active_days"):
        cutoff = datetime.utcnow() - timedelta(days=criteria["last_active_days"])
        query = query.filter(Contact.last_activity_at >= cutoff)

    if criteria.get("channel"):
        query = query.join(ContactIdentifier).filter(
            ContactIdentifier.channel == criteria["channel"]
        )

    if criteria.get("score_min"):
        query = query.filter(
            Contact.metadata["score"].as_float() >= criteria["score_min"]
        )

    # Ejecutar
    contacts = await db.execute(query)
    contact_list = contacts.scalars().all()

    return {
        "total_count": len(contact_list),
        "criteria_applied": criteria,
        "sample": [
            {"id": str(c.id), "name": f"{c.first_name} {c.last_name}", "channel": c.primary_channel}
            for c in contact_list[:10]
        ],
        "channels_distribution": _count_by_channel(contact_list),
    }


@tool
async def send_campaign(campaign_id: str, confirm: bool = False) -> dict:
    """
    Enviar una campaña de marketing. Requiere confirmación explícita.

    Args:
        campaign_id: ID de la campaña a enviar
        confirm: Debe ser True para confirmar el envío
    """
    if not confirm:
        return {
            "success": False,
            "message": "Debes confirmar el envío con confirm=True. "
                       "¿Estás seguro de enviar la campaña?",
        }

    campaign = await get_campaign(campaign_id)
    if not campaign:
        return {"success": False, "error": "Campaña no encontrada"}

    if campaign.status != "draft":
        return {"success": False, "error": f"Campaña en estado '{campaign.status}', no se puede enviar"}

    # Verificar que no se envió una campaña al mismo segmento en < 24h
    recent = await check_recent_campaign(campaign.segment_criteria, hours=24)
    if recent:
        return {
            "success": False,
            "error": f"Ya se envió una campaña similar hace menos de 24h ('{recent.name}'). "
                     "Espera antes de enviar otra.",
        }

    # Encolar envío masivo con throttling
    from app.tasks.campaign_tasks import execute_campaign
    execute_campaign.delay(str(campaign_id))

    campaign.status = "scheduled"
    await db.commit()

    return {
        "success": True,
        "campaign_id": campaign_id,
        "target_count": campaign.target_count,
        "status": "scheduled",
        "message": f"Campaña encolada para envío. {campaign.target_count} mensajes serán enviados "
                   f"con throttling de 100 msg/s.",
    }


@tool
async def get_campaign_metrics(campaign_id: str) -> dict:
    """
    Obtener métricas de una campaña.

    Args:
        campaign_id: ID de la campaña
    """
    campaign = await get_campaign(campaign_id)
    if not campaign:
        return {"error": "Campaña no encontrada"}

    return {
        "campaign_id": campaign_id,
        "name": campaign.name,
        "status": campaign.status,
        "metrics": {
            "total_targeted": campaign.target_count,
            "delivered": campaign.delivered_count,
            "read": campaign.read_count,
            "replied": campaign.replied_count,
            "failed": campaign.failed_count,
            "delivery_rate": round(campaign.delivered_count / max(campaign.target_count, 1) * 100, 1),
            "read_rate": round(campaign.read_count / max(campaign.delivered_count, 1) * 100, 1),
            "reply_rate": round(campaign.replied_count / max(campaign.delivered_count, 1) * 100, 1),
        },
        "started_at": campaign.started_at,
        "completed_at": campaign.completed_at,
    }
```

### 3. Modelo de Campaña — `app/models/campaign.py`

```python
# app/models/campaign.py
from sqlalchemy import Column, String, Integer, DateTime, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.models.base import Base, TimestampMixin
import uuid

class Campaign(Base, TimestampMixin):
    __tablename__ = "campaigns"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    channel = Column(String(50), nullable=False)
    segment_criteria = Column(JSONB, nullable=False)
    message_template = Column(Text, nullable=False)
    status = Column(String(20), default="draft")  # draft, scheduled, sending, completed, failed
    target_count = Column(Integer, default=0)
    delivered_count = Column(Integer, default=0)
    read_count = Column(Integer, default=0)
    replied_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    schedule = Column(DateTime)  # NULL = envío inmediato
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    created_by = Column(UUID(as_uuid=True))
    error_log = Column(JSONB, default=[])
```

Migración:

```sql
CREATE TABLE campaigns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL REFERENCES clients(id),
    name VARCHAR(200) NOT NULL,
    channel VARCHAR(50) NOT NULL,
    segment_criteria JSONB NOT NULL,
    message_template TEXT NOT NULL,
    status VARCHAR(20) DEFAULT 'draft',
    target_count INTEGER DEFAULT 0,
    delivered_count INTEGER DEFAULT 0,
    read_count INTEGER DEFAULT 0,
    replied_count INTEGER DEFAULT 0,
    failed_count INTEGER DEFAULT 0,
    schedule TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_by UUID REFERENCES users(id),
    error_log JSONB DEFAULT '[]',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE campaigns ENABLE ROW LEVEL SECURITY;
CREATE POLICY campaigns_isolation ON campaigns
    USING (client_id = current_setting('app.current_client_id')::UUID);

CREATE INDEX idx_campaigns_client_status ON campaigns (client_id, status);
```

### 4. CRUD de Campañas — `app/api/v1/campaigns.py`

```python
# app/api/v1/campaigns.py
from fastapi import APIRouter, Depends, HTTPException, Query
from uuid import UUID

router = APIRouter(prefix="/api/v1/campaigns", tags=["Campaigns"])

@router.get("")
async def list_campaigns(
    status: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "super_admin"])),
):
    """Listar campañas del tenant."""
    query = select(Campaign).where(Campaign.client_id == current_user.client_id)
    if status:
        query = query.where(Campaign.status == status)
    query = query.order_by(Campaign.created_at.desc()).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()

@router.get("/{campaign_id}")
async def get_campaign(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "super_admin"])),
):
    """Obtener detalle de una campaña con métricas."""
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaña no encontrada")
    return campaign

@router.post("", status_code=201)
async def create_campaign_endpoint(
    data: CampaignCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "super_admin"])),
):
    """Crear campaña (modo draft)."""
    campaign = Campaign(
        client_id=current_user.client_id,
        name=data.name,
        channel=data.channel,
        segment_criteria=data.segment_criteria,
        message_template=data.message_template,
        schedule=data.schedule,
        created_by=current_user.id,
    )
    # Contar segmento
    campaign.target_count = await count_segment(data.segment_criteria, current_user.client_id)
    db.add(campaign)
    await db.commit()
    return campaign

@router.post("/{campaign_id}/send", status_code=202)
async def send_campaign_endpoint(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "super_admin"])),
):
    """Enviar campaña (encolar en Celery bulk queue)."""
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaña no encontrada")
    if campaign.status != "draft":
        raise HTTPException(400, f"Campaña en estado '{campaign.status}', solo se puede enviar desde 'draft'")

    from app.tasks.campaign_tasks import execute_campaign
    execute_campaign.delay(str(campaign_id))

    campaign.status = "scheduled"
    await db.commit()

    return {"status": "scheduled", "target_count": campaign.target_count}

@router.delete("/{campaign_id}", status_code=204)
async def delete_campaign(
    campaign_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin", "super_admin"])),
):
    """Eliminar campaña (solo en estado draft)."""
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaña no encontrada")
    if campaign.status != "draft":
        raise HTTPException(400, "Solo se pueden eliminar campañas en estado 'draft'")
    await db.delete(campaign)
    await db.commit()
```

### 5. Task de envío masivo con throttling

```python
# app/tasks/campaign_tasks.py
import asyncio
from app.core.celery_app import celery_app

MAX_MESSAGES_PER_SECOND = 100  # Throttling por tenant

@celery_app.task(queue="bulk", bind=True)
async def execute_campaign(self, campaign_id: str):
    """
    Ejecutar campaña de marketing con throttling.
    Envía mensajes en lotes respetando el rate limit.
    """
    async with get_db_session() as db:
        campaign = await db.get(Campaign, campaign_id)
        if not campaign or campaign.status not in ("draft", "scheduled"):
            return

        campaign.status = "sending"
        campaign.started_at = datetime.utcnow()
        await db.commit()

        try:
            # Obtener contactos del segmento
            contacts = await get_segment_contacts(db, campaign.client_id, campaign.segment_criteria)
            campaign.target_count = len(contacts)

            # Obtener provider
            channel_config = await get_active_channel_config(db, campaign.client_id, campaign.channel)
            provider = ProviderFactory.get_provider(campaign.channel, channel_config.provider_config)

            # Enviar con throttling
            sent = 0
            failed = 0
            batch_size = MAX_MESSAGES_PER_SECOND
            errors = []

            for i in range(0, len(contacts), batch_size):
                batch = contacts[i:i + batch_size]
                batch_start = time.monotonic()

                for contact in batch:
                    try:
                        # Resolver variables en template
                        message = resolve_template(
                            campaign.message_template,
                            {"contact_name": contact.first_name or ""},
                        )

                        identifier = await get_contact_identifier(db, contact.id, campaign.channel)
                        if not identifier:
                            failed += 1
                            continue

                        await provider.send_message(
                            recipient_id=identifier.identifier_value,
                            content=message,
                        )
                        sent += 1
                    except Exception as e:
                        failed += 1
                        errors.append({"contact_id": str(contact.id), "error": str(e)})

                # Actualizar progreso
                campaign.delivered_count = sent
                campaign.failed_count = failed
                await db.commit()

                # Throttling: esperar si el batch se procesó más rápido que 1 segundo
                elapsed = time.monotonic() - batch_start
                if elapsed < 1.0:
                    await asyncio.sleep(1.0 - elapsed)

            # Finalizar
            campaign.status = "completed"
            campaign.completed_at = datetime.utcnow()
            campaign.error_log = errors[:100]  # Guardar máximo 100 errores
            await db.commit()

        except Exception as exc:
            campaign.status = "failed"
            campaign.error_log = [{"error": str(exc)}]
            await db.commit()
            raise
```

### 6. Re-ranking RAG — Modificar `app/services/rag.py`

**6.1 Pipeline de re-ranking**

```python
# En app/services/rag.py (agregar re-ranking)
from sentence_transformers import CrossEncoder

class RAGService:
    def __init__(self):
        self.cross_encoder = None  # Lazy loading

    def _get_cross_encoder(self):
        """Cargar cross-encoder bajo demanda (lazy)."""
        if self.cross_encoder is None:
            self.cross_encoder = CrossEncoder(
                "cross-encoder/ms-marco-MiniLM-L-6-v2",
                max_length=512,
            )
        return self.cross_encoder

    async def retrieve_with_reranking(
        self,
        query: str,
        client_id: str,
        collection: str,
        initial_top_k: int = 20,
        final_top_k: int = 5,
        use_reranking: bool = True,
    ) -> list[dict]:
        """
        Pipeline de retrieval con re-ranking opcional.
        1. Embedding search (top 20)
        2. Cross-encoder re-rank (si habilitado)
        3. Retornar top 5
        """
        # 1. Retrieval inicial por embeddings
        initial_results = await self.embedding_search(
            query=query,
            client_id=client_id,
            collection=collection,
            top_k=initial_top_k,
        )

        if not use_reranking or len(initial_results) <= final_top_k:
            return initial_results[:final_top_k]

        # 2. Re-ranking con cross-encoder
        cross_encoder = self._get_cross_encoder()

        # Preparar pares (query, document) para el cross-encoder
        pairs = [(query, doc["content"]) for doc in initial_results]

        # Calcular scores del cross-encoder
        ce_scores = cross_encoder.predict(pairs)

        # 3. Combinar y reordenar
        for doc, score in zip(initial_results, ce_scores):
            doc["rerank_score"] = float(score)

        reranked = sorted(initial_results, key=lambda x: x["rerank_score"], reverse=True)

        # Registrar métricas
        from app.core.telemetry import rag_retrieval_latency
        # (latencia ya registrada en el embedding_search, registrar re-ranking aparte)

        return reranked[:final_top_k]
```

**6.2 Configuración por tenant**

```python
# En agent_configs.settings (JSONB)
{
    "rag": {
        "use_reranking": true,          # Activar/desactivar cross-encoder
        "initial_top_k": 20,             # Candidatos iniciales
        "final_top_k": 5,                # Resultados finales
        "min_similarity": 0.3,           # Umbral mínimo de similitud
        "reranking_model": "ms-marco-MiniLM-L-6-v2",  # Modelo de cross-encoder
    }
}
```

### 7. Scoring Predictivo — `app/services/contact_scoring.py`

```python
# app/services/contact_scoring.py

class ContactScoring:
    """
    Scoring predictivo de contactos basado en:
    - Actividad (frecuencia de interacción)
    - Recencia (tiempo desde última interacción)
    - Sentimiento acumulado
    - Conversiones (citas agendadas, facturas emitidas)
    """

    WEIGHTS = {
        "recency": 0.25,        # Peso de recencia
        "frequency": 0.25,      # Peso de frecuencia
        "sentiment": 0.20,      # Peso de sentimiento
        "engagement": 0.15,     # Peso de engagement (tasa de respuesta)
        "conversion": 0.15,     # Peso de conversiones
    }

    async def calculate_score(self, db: AsyncSession, contact_id: UUID) -> float:
        """Calcular score predictivo (0-100) para un contacto."""

        # 1. Recencia: días desde última interacción
        last_activity = await self._get_last_activity(db, contact_id)
        recency_score = self._recency_score(last_activity)

        # 2. Frecuencia: mensajes en los últimos 30 días
        message_count = await self._get_message_count(db, contact_id, days=30)
        frequency_score = self._frequency_score(message_count)

        # 3. Sentimiento acumulado
        avg_sentiment = await self._get_avg_sentiment(db, contact_id)
        sentiment_score = self._sentiment_score(avg_sentiment)

        # 4. Engagement: tasa de respuesta a mensajes salientes
        engagement_rate = await self._get_engagement_rate(db, contact_id)
        engagement_score = engagement_rate * 100

        # 5. Conversiones: citas, facturas en los últimos 90 días
        conversion_count = await self._get_conversions(db, contact_id, days=90)
        conversion_score = min(conversion_count * 20, 100)

        # Score ponderado
        total_score = (
            recency_score * self.WEIGHTS["recency"]
            + frequency_score * self.WEIGHTS["frequency"]
            + sentiment_score * self.WEIGHTS["sentiment"]
            + engagement_score * self.WEIGHTS["engagement"]
            + conversion_score * self.WEIGHTS["conversion"]
        )

        return round(total_score, 2)

    def _recency_score(self, last_activity: Optional[datetime]) -> float:
        if not last_activity:
            return 0
        days_ago = (datetime.utcnow() - last_activity).days
        if days_ago <= 1:
            return 100
        elif days_ago <= 7:
            return 80
        elif days_ago <= 30:
            return 50
        elif days_ago <= 90:
            return 20
        return 0

    def _frequency_score(self, count: int) -> float:
        if count >= 20:
            return 100
        elif count >= 10:
            return 80
        elif count >= 5:
            return 60
        elif count >= 1:
            return 30
        return 0

    def _sentiment_score(self, avg: Optional[float]) -> float:
        """Convertir sentimiento promedio (0-1 donde 1=positive) a score."""
        if avg is None:
            return 50  # Neutral por defecto
        return avg * 100
```

**Endpoint para recalcular score:**

```python
# En app/api/v1/contacts.py
@router.put("/contacts/{contact_id}/score")
async def recalculate_score(
    contact_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Recalcular score predictivo de un contacto."""
    contact = await db.get(Contact, contact_id)
    if not contact:
        raise HTTPException(404, "Contacto no encontrado")

    scoring = ContactScoring()
    score = await scoring.calculate_score(db, contact_id)

    contact.metadata = {**(contact.metadata or {}), "score": score, "score_updated_at": datetime.utcnow().isoformat()}
    await db.commit()

    return {"contact_id": str(contact_id), "score": score}
```

### 8. Actualizar Intent Routing

```python
# En app/agents/nodes/intent_routing.py (agregar intents)

INTENTS = [
    # Existentes
    "general",
    "faq",
    "appointment",
    "handoff",
    "greeting",
    "farewell",
    # Nuevos
    "financial",       # "necesito una factura", "consultar NIT", "estado de mi factura"
    "marketing",       # "crear campaña", "enviar mensaje masivo", "segmentar contactos"
    "invoice_request", # Subintent de financial, más específico
]

# Actualizar routing condicional
def route_by_intent(state: ConversationState) -> str:
    intent = state.get("intent")
    agent_configs = state.get("agent_configs", {})

    # Routing condicional: solo si el agente está habilitado
    if intent in ("financial", "invoice_request"):
        if "financial" in agent_configs:
            return "financial_agent"
        return "rag_query"  # Fallback a RAG si no está habilitado

    if intent == "marketing":
        if "marketing" in agent_configs:
            return "marketing_agent"
        return "rag_query"

    # ... routing existente
```

### 9. Tests

**9.1 Tests del agente financiero — `tests/unit/test_financial_agent.py`**

```python
import pytest
from unittest.mock import AsyncMock, patch
from app.agents.tools.invoice_tools import validate_nit, calculate_verification_digit

class TestFinancialAgent:
    def test_calculate_verification_digit(self):
        """Verificar cálculo de dígito de verificación NIT."""
        # NIT conocido de la DIAN: 899999090-2
        assert calculate_verification_digit("899999090") == 2
        # Otro ejemplo: 860000000
        assert calculate_verification_digit("860000000") is not None

    @pytest.mark.asyncio
    async def test_validate_nit_format(self):
        """NIT con formato inválido retorna error."""
        result = await validate_nit.ainvoke({"nit": "abc"})
        assert result["valid"] is False
        assert "formato" in result["error"].lower() or "inválido" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_validate_nit_success(self):
        """NIT válido retorna información del contribuyente."""
        with patch("httpx.AsyncClient") as mock:
            mock.return_value.__aenter__ = AsyncMock(return_value=mock.return_value)
            mock.return_value.__aexit__ = AsyncMock(return_value=False)
            mock.return_value.get = AsyncMock(return_value=MockResponse(
                status_code=200,
                json_data={"razon_social": "Empresa Test SAS", "regimen": "Responsable de IVA"}
            ))

            result = await validate_nit.ainvoke({"nit": "900123456"})
            assert result["valid"] is True
            assert result["business_name"] == "Empresa Test SAS"

    @pytest.mark.asyncio
    async def test_create_invoice_calculates_totals(self):
        """Factura calcula subtotal, IVA y total correctamente."""
        result = await create_invoice.ainvoke({
            "contact_id": "test-contact",
            "buyer_nit": "900123456",
            "buyer_name": "Empresa Test",
            "items": [
                {"description": "Servicio", "quantity": 1, "unit_price": 1000000, "tax_rate": 0.19},
            ],
        })
        assert result["subtotal"] == 1000000
        assert result["tax_total"] == 190000
        assert result["total"] == 1190000

    @pytest.mark.asyncio
    async def test_financial_node_disabled(self):
        """Si agente financiero no está habilitado, retorna mensaje de error."""
        state = {"agent_configs": {}, "last_user_message": "Necesito una factura"}
        result = await financial_agent_node(state)
        assert "no está habilitado" in result["response"]
```

**9.2 Tests del agente de marketing — `tests/unit/test_marketing_agent.py`**

```python
class TestMarketingAgent:
    @pytest.mark.asyncio
    async def test_segment_contacts_by_tags(self, db, test_contacts_with_tags):
        """Segmentar contactos por tags."""
        result = await segment_contacts.ainvoke({
            "criteria": {"tags": ["premium"]}
        })
        assert result["total_count"] > 0
        assert "premium" in result["criteria_applied"]["tags"]

    @pytest.mark.asyncio
    async def test_send_campaign_requires_confirmation(self):
        """Enviar campaña sin confirm=True falla."""
        result = await send_campaign.ainvoke({
            "campaign_id": "test-campaign",
            "confirm": False,
        })
        assert result["success"] is False
        assert "confirmar" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_throttling_respects_rate_limit(self):
        """Verificar que el throttling no excede 100 msg/s."""
        # Medir tiempo de envío de 200 mensajes
        # Debe tomar al menos 2 segundos
        pass
```

**9.3 Tests del re-ranking — `tests/unit/test_rag_reranking.py`**

```python
class TestRAGReranking:
    @pytest.mark.asyncio
    async def test_reranking_improves_relevance(self, rag_service):
        """Re-ranking reordena resultados mejorando la relevancia."""
        # Query específica con resultados conocidos
        results_without = await rag_service.retrieve_with_reranking(
            query="¿Cómo restablecer mi contraseña?",
            client_id="test",
            collection="faqs",
            use_reranking=False,
        )
        results_with = await rag_service.retrieve_with_reranking(
            query="¿Cómo restablecer mi contraseña?",
            client_id="test",
            collection="faqs",
            use_reranking=True,
        )
        # El resultado más relevante debería estar primero con re-ranking
        assert results_with[0]["rerank_score"] >= results_with[-1]["rerank_score"]

    @pytest.mark.asyncio
    async def test_reranking_disabled_returns_original_order(self, rag_service):
        """Sin re-ranking, el orden es por similitud de embedding."""
        results = await rag_service.retrieve_with_reranking(
            query="test",
            client_id="test",
            collection="faqs",
            use_reranking=False,
        )
        assert "rerank_score" not in results[0]
```

## Criterios de Aceptación

| # | Criterio | Verificación |
|---|---|---|
| 1 | "Necesito una factura" → routing correcto → financial_agent | Intent routing detecta "financial" → nodo financial_agent |
| 2 | Validación de NIT funcional | NIT válido → datos del contribuyente; NIT inválido → error claro |
| 3 | Factura creada con cálculos correctos | Items + IVA → subtotal, tax_total, total verificados |
| 4 | Agente financiero deshabilitado → mensaje de error | Sin config "financial" → respuesta "no habilitado" |
| 5 | Campaña de marketing enviada solo al segmento | Segmentar por tags → campaña enviada solo a esos contactos |
| 6 | Throttling funciona (max 100 msg/s) | 200 mensajes tardan al menos 2 segundos |
| 7 | Template WhatsApp no aprobado → error | Intentar enviar template no aprobado → rechazo |
| 8 | Re-ranking mejora resultados RAG (cuando habilitado) | Top result con re-ranking es más relevante que sin re-ranking |
| 9 | Re-ranking configurable por tenant | Activar/desactivar en agent_configs.settings.rag |
| 10 | Scoring de contactos funcional | Score 0-100 calculado con 5 factores ponderados |
| 11 | Custom fields usan JSONB (no columnas dinámicas) | Datos en contacts.metadata, no columnas nuevas |

## Notas Técnicas

- **Agente financiero específico para Colombia**: La integración con DIAN es específica para Colombia. Para otros países, crear implementaciones adicionales de los tools (facturas electrónicas de México/CFDI, Argentina/AFIP, etc.). La ABC del agente financiero se mantiene.
- **Envío masivo usa cola `bulk`**: El envío de campañas DEBE usar la cola `bulk` de Celery con throttling. NUNCA enviar masivamente en la cola principal — bloquearía el procesamiento de mensajes individuales.
- **Cross-encoder en CPU**: El modelo `ms-marco-MiniLM-L-6-v2` es lo suficientemente pequeño para ejecutarse en CPU con latencia aceptable (~10ms por par query-document). No requiere GPU.
- **Custom fields con JSONB**: Los campos personalizados del CRM se almacenan en `contacts.metadata` (JSONB). NO crear columnas dinámicas en la tabla — JSONB es flexible y eficiente con índices GIN.
- **Template de WhatsApp**: Los templates de mensajes masivos por WhatsApp deben estar pre-aprobados por Meta. La plataforma debe verificar esto antes de enviar una campaña.

## Dependencias

| Dependencia | Versión | Propósito |
|---|---|---|
| sentence-transformers | >=2.5.0 | CrossEncoder para re-ranking |
| langchain-core | >=0.1.0 | Tools y agent framework |
| langchain-openai | >=0.1.0 | ChatOpenAI con tool calling |
| httpx | >=0.27.0 | Requests a API DIAN |
