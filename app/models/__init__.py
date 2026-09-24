"""SQLAlchemy models — 22 tablas (18 MVP + service_types/appointments/agent_action_log de Sprint 7 + audit_logs de Sprint 8).

Exporta todos los modelos para import directo:
    from app.models import User, Contact, Conversation
"""

from app.models.agent_action_log import AgentActionLog
from app.models.agent_config import AgentConfig
from app.models.approved_response import ApprovedResponse
from app.models.audit_log import AuditLog
from app.models.base import Base, TenantBaseModel
from app.models.campaign import Campaign
from app.models.client import Client
from app.models.contact import Contact
from app.models.contact_identifier import ContactIdentifier
from app.models.contact_tag import ContactTag
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.internal_note import InternalNote
from app.models.invoice import Invoice
from app.models.message import Message
from app.models.outgoing_webhook_log import OutgoingWebhookLog
from app.models.pending_response import PendingResponse
from app.models.quick_reply import QuickReply
from app.models.service_type import Appointment, ServiceType
from app.models.tag import Tag
from app.models.tenant_template import InstantiationStatus, TemplateInstantiation, TenantTemplate
from app.models.tenant_webhook import TenantWebhook
from app.models.token_budget import TokenBudget
from app.models.token_usage_log import TokenUsageLog
from app.models.user import User
from app.models.webhook_dedup import WebhookDedup

__all__ = [
    "AgentActionLog",
    "AgentConfig",
    "Appointment",
    "ApprovedResponse",
    "AuditLog",
    "Base",
    "Campaign",
    "Client",
    "Contact",
    "ContactIdentifier",
    "ContactTag",
    "Conversation",
    "Document",
    "DocumentChunk",
    "InstantiationStatus",
    "InternalNote",
    "Invoice",
    "Message",
    "OutgoingWebhookLog",
    "PendingResponse",
    "QuickReply",
    "ServiceType",
    "Tag",
    "TemplateInstantiation",
    "TenantBaseModel",
    "TenantTemplate",
    "TenantWebhook",
    "TokenBudget",
    "TokenUsageLog",
    "User",
    "WebhookDedup",
]
