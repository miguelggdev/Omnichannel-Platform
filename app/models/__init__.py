"""SQLAlchemy models — 18 tablas MVP.

Exporta todos los modelos para import directo:
    from app.models import User, Contact, Conversation
"""

from app.models.agent_config import AgentConfig
from app.models.approved_response import ApprovedResponse
from app.models.base import Base, TenantBaseModel
from app.models.client import Client
from app.models.contact import Contact
from app.models.contact_identifier import ContactIdentifier
from app.models.contact_tag import ContactTag
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.internal_note import InternalNote
from app.models.message import Message
from app.models.pending_response import PendingResponse
from app.models.quick_reply import QuickReply
from app.models.tag import Tag
from app.models.token_budget import TokenBudget
from app.models.token_usage_log import TokenUsageLog
from app.models.user import User
from app.models.webhook_dedup import WebhookDedup

__all__ = [
    "AgentConfig",
    "ApprovedResponse",
    "Base",
    "Client",
    "Contact",
    "ContactIdentifier",
    "ContactTag",
    "Conversation",
    "Document",
    "DocumentChunk",
    "InternalNote",
    "Message",
    "PendingResponse",
    "QuickReply",
    "Tag",
    "TenantBaseModel",
    "TokenBudget",
    "TokenUsageLog",
    "User",
    "WebhookDedup",
]
