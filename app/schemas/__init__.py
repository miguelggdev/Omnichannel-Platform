"""Pydantic schemas — Validación de entrada/salida para la API.

Exporta todos los schemas para import directo:
    from app.schemas import LoginRequest, TokenResponse, ContactCreate
"""

from app.schemas.auth import LoginRequest, RefreshRequest, TokenResponse
from app.schemas.common import ErrorResponse, PaginatedResponse, PaginationParams
from app.schemas.client import ClientCreate, ClientUpdate, ClientResponse, ClientListResponse
from app.schemas.user import UserCreate, UserUpdate, UserResponse, UserListResponse
from app.schemas.contact import ContactCreate, ContactUpdate, ContactResponse, ContactListResponse
from app.schemas.conversation import (
    ConversationCreate, ConversationUpdate, ConversationResponse, ConversationListResponse,
)
from app.schemas.message import MessageCreate, MessageUpdate, MessageResponse, MessageListResponse
from app.schemas.document import DocumentCreate, DocumentUpdate, DocumentResponse, DocumentListResponse
from app.schemas.agent_config import (
    AgentConfigCreate, AgentConfigUpdate, AgentConfigResponse, AgentConfigListResponse,
)

__all__ = [
    # Auth
    "LoginRequest", "RefreshRequest", "TokenResponse",
    # Common
    "ErrorResponse", "PaginatedResponse", "PaginationParams",
    # Client
    "ClientCreate", "ClientUpdate", "ClientResponse", "ClientListResponse",
    # User
    "UserCreate", "UserUpdate", "UserResponse", "UserListResponse",
    # Contact
    "ContactCreate", "ContactUpdate", "ContactResponse", "ContactListResponse",
    # Conversation
    "ConversationCreate", "ConversationUpdate", "ConversationResponse", "ConversationListResponse",
    # Message
    "MessageCreate", "MessageUpdate", "MessageResponse", "MessageListResponse",
    # Document
    "DocumentCreate", "DocumentUpdate", "DocumentResponse", "DocumentListResponse",
    # AgentConfig
    "AgentConfigCreate", "AgentConfigUpdate", "AgentConfigResponse", "AgentConfigListResponse",
]
