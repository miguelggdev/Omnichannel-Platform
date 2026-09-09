"""Schemas de AgentConfig — Configuración del agente IA."""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AgentConfigCreate(BaseModel):
    """Schema para crear configuración de agente."""

    name: str = Field(max_length=100)
    system_prompt: str | None = None
    welcome_message: str | None = None
    model: str = "gpt-4o"
    temperature: float = Field(default=0.7, ge=0.0, le=1.0)
    max_tokens: int = Field(default=1024, ge=1)
    training_mode: bool = False
    similarity_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    handoff_message: str | None = None
    config: dict[str, Any] = {}


class AgentConfigUpdate(BaseModel):
    """Schema para actualizar configuración de agente."""

    name: str | None = None
    system_prompt: str | None = None
    welcome_message: str | None = None
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, ge=1)
    training_mode: bool | None = None
    similarity_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    handoff_message: str | None = None
    config: dict[str, Any] | None = None
    is_active: bool | None = None


class AgentConfigResponse(BaseModel):
    """Schema de respuesta de configuración de agente."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    client_id: UUID
    name: str
    system_prompt: str | None
    welcome_message: str | None
    model: str
    temperature: float
    max_tokens: int
    training_mode: bool
    similarity_threshold: float
    handoff_message: str | None
    config: dict[str, Any]
    is_active: bool


class AgentConfigListResponse(BaseModel):
    """Schema de lista de configuraciones de agente."""

    items: list[AgentConfigResponse]
    total: int
    page: int
    page_size: int
