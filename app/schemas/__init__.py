"""Pydantic schemas — Validación de entrada/salida para la API.

Exporta todos los schemas para import directo:
    from app.schemas import LoginRequest, TokenResponse, ContactCreate
"""

from app.schemas.agent_config import (
    AgentConfigCreate,
    AgentConfigListResponse,
    AgentConfigResponse,
    AgentConfigUpdate,
)
from app.schemas.auth import LoginRequest, RefreshRequest, TokenResponse
from app.schemas.client import ClientCreate, ClientListResponse, ClientResponse, ClientUpdate
from app.schemas.common import ErrorResponse, PaginatedResponse, PaginationParams
from app.schemas.contact import ContactCreate, ContactListResponse, ContactResponse, ContactUpdate
from app.schemas.conversation import (
    ConversationCreate,
    ConversationListResponse,
    ConversationResponse,
    ConversationUpdate,
)
from app.schemas.document import (
    DocumentCreate,
    DocumentListResponse,
    DocumentResponse,
    DocumentUpdate,
)
from app.schemas.message import MessageCreate, MessageListResponse, MessageResponse, MessageUpdate
from app.schemas.user import UserCreate, UserListResponse, UserResponse, UserUpdate

__all__ = [
    "AgentConfigCreate",
    "AgentConfigListResponse",
    "AgentConfigResponse",
    "AgentConfigUpdate",
    "ClientCreate",
    "ClientListResponse",
    "ClientResponse",
    "ClientUpdate",
    "ContactCreate",
    "ContactListResponse",
    "ContactResponse",
    "ContactUpdate",
    "ConversationCreate",
    "ConversationListResponse",
    "ConversationResponse",
    "ConversationUpdate",
    "DocumentCreate",
    "DocumentListResponse",
    "DocumentResponse",
    "DocumentUpdate",
    "ErrorResponse",
    "LoginRequest",
    "MessageCreate",
    "MessageListResponse",
    "MessageResponse",
    "MessageUpdate",
    "PaginatedResponse",
    "PaginationParams",
    "RefreshRequest",
    "TokenResponse",
    "UserCreate",
    "UserListResponse",
    "UserResponse",
    "UserUpdate",
]
