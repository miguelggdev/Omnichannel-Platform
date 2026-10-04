"""Schemas de la API del sandbox (Sprint 14c, ADR-078)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class SandboxStatus(BaseModel):
    """Estado del sandbox de un tenant.

    Attributes:
        exists: Si el tenant ya tiene sandbox.
        sandbox_client_id: El tenant sandbox, si existe.
        created_at: Cuando se creo.
        reset_at: La ultima vez que se rehizo desde produccion.
        last_published_at: La ultima publicacion a produccion.
        versions: Cuantas versiones hay en el historial (para el rollback).
    """

    exists: bool
    sandbox_client_id: UUID | None = None
    created_at: datetime | None = None
    reset_at: datetime | None = None
    last_published_at: datetime | None = None
    versions: int = 0


class SandboxAgentConfig(BaseModel):
    """El agente del sandbox, tal como lo leeria el grafo.

    Attributes:
        config: JSONB de configuracion. Nunca trae `clinical`, `marketing` ni
            `feature_flags`: son del tenant y no viajan entre sandbox y produccion.
    """

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


class SandboxMessageRequest(BaseModel):
    """Un mensaje de prueba para el agente del sandbox.

    Attributes:
        text: Lo que escribiria un contacto.
        new_conversation: Empezar una conversacion limpia, sin el contexto anterior.
    """

    text: str = Field(min_length=1, max_length=4000)
    new_conversation: bool = False


class SandboxMessageResponse(BaseModel):
    """Lo que contesto el agente.

    Attributes:
        conversation_id: Conversacion de prueba.
        response: Texto de la respuesta.
        intent: Intent que detecto el router.
        requires_handoff: Si el agente escalo a un humano.
        handoff_reason: Motivo del escalamiento, si lo hubo.
        detected_language: Idioma que detecto el agente.
    """

    conversation_id: UUID
    response: str | None
    intent: str | None
    requires_handoff: bool
    handoff_reason: str | None
    detected_language: str | None


class PublishResponse(BaseModel):
    """Resultado de publicar el sandbox.

    Attributes:
        version: Version del historial que guarda lo que habia en produccion.
    """

    version: int


class HistoryItem(BaseModel):
    """Una version del historial de configuracion.

    Attributes:
        version: Numero de version.
        reason: `publish` o `rollback`: por que se guardo.
        created_at: Cuando.
        created_by: Usuario que publico o revirtio.
    """

    version: int
    reason: str
    created_at: datetime
    created_by: UUID | None


class RollbackRequest(BaseModel):
    """Cuerpo de `POST /api/v1/sandbox/rollback`.

    Attributes:
        version: Version a restaurar; sin ella, la ultima del historial.
    """

    version: int | None = Field(default=None, ge=1)


class RollbackResponse(BaseModel):
    """Resultado de un rollback.

    Attributes:
        restored: Version que se restauro.
        saved_as: Version nueva donde quedo lo que habia antes de revertir.
    """

    restored: int
    saved_as: int
