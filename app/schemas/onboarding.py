"""Schemas del onboarding publico (auto-registro de un tenant)."""

import re
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.services.i18n import SUPPORTED_LANGUAGES


class BusinessType(StrEnum):
    """Tipos de negocio que se pueden elegir al registrarse."""

    RESTAURANT = "restaurant"
    CLINIC = "clinic"
    ECOMMERCE = "ecommerce"
    SERVICES = "services"
    EDUCATION = "education"
    OTHER = "other"


class OnboardingRequest(BaseModel):
    """Cuerpo de `POST /api/v1/onboarding/register`.

    Attributes:
        business_name: Nombre del negocio.
        business_type: Categoria del negocio.
        admin_full_name: Nombre completo de quien administrara el tenant.
        admin_email: Email del administrador (tambien su usuario de acceso).
        admin_password: Contrasena: 8-72 caracteres con mayuscula, minuscula y numero
            (72 es el limite de bcrypt).
        country: Codigo ISO 3166-1 alfa-2.
        language: Idioma de la interfaz del administrador (uno de los 6 soportados).
        terms_accepted: Debe ser `True`.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    business_name: str = Field(min_length=2, max_length=255)
    business_type: BusinessType
    admin_full_name: str = Field(min_length=2, max_length=200)
    admin_email: EmailStr
    admin_password: str = Field(min_length=8, max_length=72)
    country: str = Field(default="CO", pattern=r"^[A-Z]{2}$")
    language: str = "es"
    terms_accepted: bool

    @field_validator("admin_password")
    @classmethod
    def _password_robusta(cls, valor: str) -> str:
        """Exige mayuscula, minuscula y numero."""
        if not (
            re.search(r"[A-Z]", valor) and re.search(r"[a-z]", valor) and re.search(r"\d", valor)
        ):
            raise ValueError("La contraseña debe tener mayúscula, minúscula y número")
        return valor

    @field_validator("language")
    @classmethod
    def _idioma_soportado(cls, valor: str) -> str:
        """Solo los idiomas de la plataforma."""
        if valor not in SUPPORTED_LANGUAGES:
            raise ValueError(f"Idioma no soportado: {valor}")
        return valor

    @model_validator(mode="after")
    def _terminos(self) -> "OnboardingRequest":
        """Los terminos deben estar aceptados."""
        if not self.terms_accepted:
            raise ValueError("Debes aceptar los términos y condiciones")
        return self


class OnboardingResponse(BaseModel):
    """Tenant creado y sesion del administrador.

    Attributes:
        client_id: Tenant nuevo.
        user_id: Administrador creado.
        access_token: JWT de acceso.
        refresh_token: JWT de renovacion.
        token_type: Siempre `bearer`.
    """

    client_id: UUID
    user_id: UUID
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105  # nosec B105


class VerifyEmailRequest(BaseModel):
    """Cuerpo de `POST /api/v1/onboarding/verify-email`."""

    token: str = Field(min_length=10, max_length=2000)


class VerifyEmailResponse(BaseModel):
    """Resultado de la verificacion."""

    verified: bool


class VerificationStatus(BaseModel):
    """Estado de verificacion del email del usuario autenticado.

    Attributes:
        email: Email actual.
        verified: `True`/`False` si la cuenta nacio del registro publico; `None` si no
            hay nada que verificar (cuentas creadas por un administrador).
    """

    email: str
    verified: bool | None


class ResendVerificationResponse(BaseModel):
    """Resultado del reenvio."""

    sent: bool
