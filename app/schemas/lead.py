"""Schemas del modulo de leads (Sprint 16, ADR-082): leads, etapas, fuentes y captura publica.

Son el contrato entre la base (modelos) y la API: lo que entra se valida aqui y lo que sale
nunca lleva el hash del token de captura ni los indices ciegos.
"""

import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

_TELEFONO = re.compile(r"^\+?[0-9 ()\-]{6,25}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_]{0,49}$")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

SourceType = Literal[
    "web_form",
    "linkedin",
    "facebook_ad",
    "google_ad",
    "instagram",
    "referral",
    "manual",
    "api",
    "whatsapp",
    "import",
]
LeadStatus = Literal["active", "won", "lost", "disqualified"]
Temperature = Literal["cold", "warm", "hot"]


def _telefono(valor: str | None) -> str | None:
    """Valida un telefono tolerante: digitos, espacios, parentesis, guiones y un `+` inicial."""
    if valor is None:
        return None
    limpio = valor.strip()
    if not limpio:
        return None
    if not _TELEFONO.match(limpio):
        raise ValueError("Telefono no valido")
    return limpio


class _DatosDeLead(BaseModel):
    """Campos que describen a la persona y su empresa, comunes a crear, editar y capturar."""

    model_config = ConfigDict(str_strip_whitespace=True)

    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=50)
    linkedin_url: str | None = Field(default=None, max_length=500)
    company_name: str | None = Field(default=None, max_length=200)
    company_domain: str | None = Field(default=None, max_length=255)
    company_size: str | None = Field(default=None, max_length=50)
    industry: str | None = Field(default=None, max_length=100)
    job_title: str | None = Field(default=None, max_length=150)

    @field_validator("phone")
    @classmethod
    def _validar_telefono(cls, valor: str | None) -> str | None:
        return _telefono(valor)

    @field_validator("linkedin_url")
    @classmethod
    def _validar_linkedin(cls, valor: str | None) -> str | None:
        """Solo `https://` : la URL se muestra como enlace en el panel."""
        if valor and not valor.lower().startswith("https://"):
            raise ValueError("La URL de LinkedIn debe empezar por https://")
        return valor or None


class LeadCreate(_DatosDeLead):
    """Cuerpo de `POST /leads`.

    Un lead sin ninguna forma de contacto no se puede deduplicar ni trabajar: hace falta al
    menos email, telefono o LinkedIn.

    Attributes:
        source_id: Fuente por la que llego.
        assigned_user_id: Usuario responsable.
        pipeline_stage_id: Etapa inicial; si falta, la primera del pipeline.
        estimated_value: Valor estimado del negocio, no negativo.
        currency: Codigo ISO 4217 de tres letras.
        temperature: `cold`, `warm` o `hot`.
    """

    source_id: UUID | None = None
    assigned_user_id: UUID | None = None
    pipeline_stage_id: UUID | None = None
    estimated_value: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    temperature: Temperature = "cold"

    @model_validator(mode="after")
    def _alguna_forma_de_contacto(self) -> Self:
        if not (self.email or self.phone or self.linkedin_url):
            raise ValueError("Indica al menos email, telefono o LinkedIn")
        return self


class LeadUpdate(_DatosDeLead):
    """Cuerpo de `PUT /leads/{id}`: solo se cambia lo que se envia; `null` borra el campo.

    Los scores y la etapa no se cambian aqui: los scores los calculan los Sprints 17-18 y la
    etapa tiene su propio endpoint (`PATCH /leads/{id}/stage`).
    """

    assigned_user_id: UUID | None = None
    estimated_value: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    temperature: Temperature | None = None
    next_follow_up_at: datetime | None = None

    @model_validator(mode="after")
    def _al_menos_un_campo(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Indica al menos un campo")
        return self


class LeadStageMove(BaseModel):
    """Cuerpo de `PATCH /leads/{id}/stage`.

    Attributes:
        stage_id: Etapa de destino.
        reason: Motivo; obligatorio si la etapa destino es la de descalificado.
    """

    stage_id: UUID
    reason: str | None = Field(default=None, max_length=500)


class LeadResponse(BaseModel):
    """Un lead tal como lo ve el panel."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    client_id: UUID
    contact_id: UUID | None
    source_id: UUID | None
    assigned_user_id: UUID | None
    pipeline_stage_id: UUID | None
    first_name: str | None
    last_name: str | None
    email: str | None
    phone: str | None
    linkedin_url: str | None
    company_name: str | None
    company_domain: str | None
    company_size: str | None
    industry: str | None
    job_title: str | None
    fit_score: int
    behavioral_score: int
    ai_score: int
    total_score: int
    status: LeadStatus
    temperature: Temperature
    estimated_value: Decimal | None
    currency: str
    last_activity_at: datetime | None
    next_follow_up_at: datetime | None
    converted_at: datetime | None
    disqualified_at: datetime | None
    disqualified_reason: str | None
    enriched_at: datetime | None
    created_at: datetime
    updated_at: datetime


class StageCreate(BaseModel):
    """Cuerpo de `POST /lead-pipeline-stages`."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=50)
    position: int | None = Field(default=None, ge=0)
    color: str | None = None
    auto_actions: dict[str, Any] = Field(default_factory=dict)
    is_terminal: bool = False

    @field_validator("slug")
    @classmethod
    def _validar_slug(cls, valor: str) -> str:
        if not _SLUG.match(valor):
            raise ValueError("El slug solo admite minusculas, numeros y guion bajo")
        return valor

    @field_validator("color")
    @classmethod
    def _validar_color(cls, valor: str | None) -> str | None:
        if valor is not None and not _COLOR.match(valor):
            raise ValueError("El color debe ser #RRGGBB")
        return valor


class StageUpdate(BaseModel):
    """Cuerpo de `PUT /lead-pipeline-stages/{id}`. El slug no cambia: lo referencian automatizaciones."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=100)
    color: str | None = None
    auto_actions: dict[str, Any] | None = None
    is_terminal: bool | None = None

    @field_validator("color")
    @classmethod
    def _validar_color(cls, valor: str | None) -> str | None:
        if valor is not None and not _COLOR.match(valor):
            raise ValueError("El color debe ser #RRGGBB")
        return valor

    @model_validator(mode="after")
    def _al_menos_un_campo(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Indica al menos un campo")
        return self


class StageReorder(BaseModel):
    """Cuerpo de `PUT /lead-pipeline-stages/order`: todas las etapas, en el orden nuevo."""

    stage_ids: list[UUID] = Field(min_length=1)

    @field_validator("stage_ids")
    @classmethod
    def _sin_repetidos(cls, valor: list[UUID]) -> list[UUID]:
        if len(set(valor)) != len(valor):
            raise ValueError("Hay etapas repetidas")
        return valor


class StageResponse(BaseModel):
    """Una etapa del pipeline."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    position: int
    color: str | None
    auto_actions: dict[str, Any]
    is_terminal: bool
    created_at: datetime


class SourceCreate(BaseModel):
    """Cuerpo de `POST /lead-sources`."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    source_type: SourceType
    config: dict[str, Any] = Field(default_factory=dict)
    utm_tracking: dict[str, Any] = Field(default_factory=dict)
    enable_capture: bool = Field(
        default=False,
        description="Genera el token del endpoint publico de captura (se muestra una sola vez).",
    )


class SourceUpdate(BaseModel):
    """Cuerpo de `PUT /lead-sources/{id}`."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=100)
    config: dict[str, Any] | None = None
    utm_tracking: dict[str, Any] | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _al_menos_un_campo(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Indica al menos un campo")
        return self


class SourceResponse(BaseModel):
    """Una fuente. Nunca lleva el hash del token: solo si la captura publica esta habilitada."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    source_type: SourceType
    config: dict[str, Any]
    utm_tracking: dict[str, Any]
    is_active: bool
    capture_enabled: bool = False
    #: Leads no borrados que llegaron por esta fuente.
    leads_count: int = 0
    created_at: datetime


class SourceCreated(SourceResponse):
    """Respuesta de crear (o regenerar) una fuente con captura: trae el token en claro.

    Attributes:
        capture_token: Token del endpoint publico. Es la unica vez que se devuelve.
    """

    capture_token: str | None = None


class LeadCapture(_DatosDeLead):
    """Cuerpo de `POST /capture/{source_token}`: lo que envia un formulario web publico.

    Attributes:
        utm_source: Parametros UTM de la visita, si los hay.
        website: Campo trampa (honeypot): oculto para personas, los bots lo rellenan. Si trae
            algo, la captura se descarta en silencio.
    """

    utm_source: str | None = Field(default=None, max_length=100)
    utm_medium: str | None = Field(default=None, max_length=100)
    utm_campaign: str | None = Field(default=None, max_length=150)
    website: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _alguna_forma_de_contacto(self) -> Self:
        if not (self.email or self.phone or self.linkedin_url):
            raise ValueError("Indica al menos email, telefono o LinkedIn")
        return self


class KanbanColumn(BaseModel):
    """Una columna del tablero: la etapa y sus leads.

    Attributes:
        stage: Etapa.
        leads: Leads de la columna (hasta el limite de la consulta).
        total: Cuantos leads tiene la etapa en realidad.
        total_value: Suma del valor estimado de todos los leads de la etapa.
    """

    stage: StageResponse
    leads: list[LeadResponse]
    total: int
    total_value: Decimal


class LeadModuleStatus(BaseModel):
    """Estado del modulo de leads del tenant.

    Attributes:
        enabled: Si el tenant tiene el modulo activo.
        stages: Cuantas etapas tiene su pipeline.
    """

    enabled: bool
    stages: int


class LeadModuleUpdate(BaseModel):
    """Cuerpo de `PUT /platform/clients/{id}/lead-management`."""

    enabled: bool


class LeadModuleUpdated(BaseModel):
    """Resultado de activar o desactivar el modulo.

    Attributes:
        client_id: Tenant modificado.
        enabled: Estado final.
        stages_created: Etapas por defecto que se crearon (0 si ya tenia pipeline o se apago).
    """

    client_id: UUID
    enabled: bool
    stages_created: int


class LeadContactLink(BaseModel):
    """Cuerpo de `PUT /leads/{id}/contact`: el contacto con el que se enlaza."""

    contact_id: UUID
