# Sprint 3 — Addendum: Onboarding, Personalización y Gestión de Clientes

**Proyecto:** Plataforma SaaS Multi-Tenant de IA Conversacional  
**Sprint:** 3 (FastAPI Core & Auth)  
**Tipo:** Addendum  
**Fecha:** 2026-09-05  
**Autor:** Equipo de Arquitectura  
**Estado:** En revisión  

---

## Índice

1. [Resumen Ejecutivo](#1-resumen-ejecutivo)  
2. [Feature 1: Flujo de Onboarding de Clientes](#2-feature-1-flujo-de-onboarding-de-clientes)  
3. [Feature 2: Sección de Personalización del Negocio](#3-feature-2-sección-de-personalización-del-negocio)  
4. [Feature 3: Gestión de Clientes (Desactivación y Alertas de Pago)](#4-feature-3-gestión-de-clientes-desactivación-y-alertas-de-pago)  
5. [Migraciones SQL](#5-migraciones-sql)  
6. [Tests](#6-tests)  
7. [Criterios de Aceptación](#7-criterios-de-aceptación)  
8. [Notas de Implementación](#8-notas-de-implementación)  

---

## 1. Resumen Ejecutivo

Este addendum extiende el Sprint 3 (FastAPI Core & Auth) con tres funcionalidades críticas para la operación de la plataforma SaaS:

1. **Flujo de Onboarding:** Endpoint público para auto-registro de nuevos clientes (tenants), incluyendo creación de cuenta, usuario administrador, configuración por defecto y verificación de correo electrónico.
2. **Personalización del Negocio:** API completa para gestión del perfil empresarial, carga de logo, mensajes personalizados del agente y base de conocimientos por documentos.
3. **Gestión de Clientes:** Panel de super administración para activación/desactivación de clientes, alertas de pago, suspensión automática y monitoreo.

Todas las funcionalidades respetan las convenciones del proyecto: RLS con `client_id`, `SET LOCAL` para pgBouncer, type hints, Google-style docstrings, async/await, y RBAC de cuatro niveles.

---

## 2. Feature 1: Flujo de Onboarding de Clientes

### 2.1 Descripción General

URL pública `/onboarding` que permite a nuevos negocios registrarse en la plataforma sin requerir autenticación. El flujo completo:

1. El usuario ingresa nombre del negocio, datos de contacto y credenciales.
2. El sistema crea el registro en `clients`.
3. Se crea el usuario administrador asociado al client.
4. Se genera la configuración por defecto del agente.
5. Se asigna un presupuesto de tokens (free tier: 50,000 tokens/mes).
6. Se dispara tarea Celery para envío de email de verificación.
7. Se retornan credenciales (access_token + refresh_token).

### 2.2 Esquemas Pydantic

```python
# app/schemas/onboarding.py
"""Esquemas Pydantic v2 para el flujo de onboarding de clientes."""

import re
from datetime import datetime
from enum import StrEnum
from typing import Optional
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)


class BusinessType(StrEnum):
    """Tipos de negocio soportados por la plataforma."""

    RESTAURANT = "restaurant"
    CLINIC = "clinic"
    ECOMMERCE = "ecommerce"
    SERVICES = "services"
    EDUCATION = "education"
    OTHER = "other"


class OnboardingRequest(BaseModel):
    """Esquema de solicitud para registro de nuevo cliente (tenant).

    Attributes:
        business_name: Nombre del negocio a registrar.
        business_type: Categoría del negocio.
        admin_email: Correo electrónico del usuario administrador.
        admin_password: Contraseña del administrador (mínimo 8 caracteres).
        admin_full_name: Nombre completo del administrador.
        phone: Teléfono de contacto (opcional).
        country: Código ISO 3166-1 alpha-2 del país.
        language: Código ISO 639-1 del idioma.
        terms_accepted: Aceptación de términos y condiciones.
        captcha_token: Token de validación CAPTCHA (opcional, configurable).
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    business_name: str = Field(
        ...,
        min_length=2,
        max_length=255,
        examples=["Mi Restaurante SAS"],
    )
    business_type: BusinessType = Field(
        ...,
        examples=[BusinessType.RESTAURANT],
    )
    admin_email: EmailStr = Field(
        ...,
        examples=["admin@mirestaurante.com"],
    )
    admin_password: str = Field(
        ...,
        min_length=8,
        max_length=128,
    )
    admin_full_name: str = Field(
        ...,
        min_length=2,
        max_length=255,
        examples=["Juan Pérez"],
    )
    phone: Optional[str] = Field(
        default=None,
        max_length=20,
        examples=["+573001234567"],
    )
    country: str = Field(
        default="CO",
        min_length=2,
        max_length=2,
        pattern=r"^[A-Z]{2}$",
    )
    language: str = Field(
        default="es",
        min_length=2,
        max_length=5,
        pattern=r"^[a-z]{2}(-[A-Z]{2})?$",
    )
    terms_accepted: bool = Field(...)
    captcha_token: Optional[str] = Field(default=None)

    @field_validator("admin_password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        """Valida que la contraseña tenga al menos una mayúscula, una minúscula y un número."""
        if not re.search(r"[A-Z]", v):
            raise ValueError("La contraseña debe contener al menos una letra mayúscula.")
        if not re.search(r"[a-z]", v):
            raise ValueError("La contraseña debe contener al menos una letra minúscula.")
        if not re.search(r"\d", v):
            raise ValueError("La contraseña debe contener al menos un número.")
        return v

    @model_validator(mode="after")
    def validate_terms(self) -> "OnboardingRequest":
        """Valida que los términos y condiciones fueron aceptados."""
        if not self.terms_accepted:
            raise ValueError("Debe aceptar los términos y condiciones para registrarse.")
        return self


class OnboardingResponse(BaseModel):
    """Esquema de respuesta exitosa del onboarding.

    Attributes:
        client_id: UUID del nuevo tenant creado.
        user_id: UUID del usuario administrador creado.
        access_token: JWT de acceso.
        refresh_token: JWT de refresco.
        message: Mensaje informativo sobre verificación de email.
    """

    client_id: UUID
    user_id: UUID
    access_token: str
    refresh_token: str
    message: str = Field(
        default="Registro exitoso. Revise su correo electrónico para verificar su cuenta."
    )
```

### 2.3 Endpoint

```python
# app/api/v1/endpoints/onboarding.py
"""Endpoints públicos para onboarding de nuevos clientes."""

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.exceptions import AppException
from app.core.rate_limit import rate_limit
from app.schemas.onboarding import OnboardingRequest, OnboardingResponse
from app.services.onboarding_service import OnboardingService

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


@router.post(
    "/register",
    response_model=OnboardingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar nuevo cliente (tenant)",
    description=(
        "Endpoint público para auto-registro de nuevos clientes. "
        "Crea el tenant, usuario admin, configuración por defecto y "
        "dispara la verificación de correo electrónico. "
        "Rate limit: 5 solicitudes por IP por hora."
    ),
)
@rate_limit(max_requests=5, window_seconds=3600, key_func="ip")
async def register_client(
    request: Request,
    payload: OnboardingRequest,
    session: AsyncSession = Depends(get_async_session),
) -> OnboardingResponse:
    """Registra un nuevo cliente en la plataforma.

    Args:
        request: Objeto request de FastAPI (usado para rate limiting).
        payload: Datos del nuevo cliente y administrador.
        session: Sesión async de SQLAlchemy.

    Returns:
        OnboardingResponse con credenciales y IDs generados.

    Raises:
        AppException: Si el email ya está registrado o hay error de validación.
    """
    service = OnboardingService(session)
    return await service.register_new_client(payload)
```

### 2.4 Servicio de Onboarding

```python
# app/services/onboarding_service.py
"""Servicio de negocio para el flujo de onboarding de clientes."""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.security import create_access_token, create_refresh_token, hash_password
from app.schemas.onboarding import OnboardingRequest, OnboardingResponse
from app.tasks.email_tasks import send_verification_email


class OnboardingService:
    """Orquesta el flujo completo de registro de un nuevo cliente (tenant).

    Attributes:
        session: Sesión async de SQLAlchemy para operaciones de base de datos.
    """

    DEFAULT_TOKEN_BUDGET: int = 50_000
    DEFAULT_SYSTEM_PROMPT: str = (
        "Eres un asistente virtual amable y profesional. "
        "Responde de manera clara y concisa a las consultas de los clientes. "
        "Si no tienes la información solicitada, indica amablemente que "
        "transferirás la consulta a un agente humano."
    )

    def __init__(self, session: AsyncSession) -> None:
        """Inicializa el servicio con una sesión de base de datos.

        Args:
            session: Sesión async de SQLAlchemy.
        """
        self.session = session

    async def register_new_client(
        self,
        payload: OnboardingRequest,
    ) -> OnboardingResponse:
        """Ejecuta el flujo completo de onboarding para un nuevo cliente.

        Pasos:
            1. Valida que el email no exista.
            2. Crea el registro en ``clients``.
            3. Crea el usuario administrador.
            4. Crea la configuración por defecto del agente.
            5. Asigna el presupuesto de tokens (free tier).
            6. Genera tokens JWT.
            7. Dispara email de verificación (Celery).

        Args:
            payload: Datos validados del formulario de onboarding.

        Returns:
            OnboardingResponse con credenciales y IDs.

        Raises:
            AppException: 409 si el email ya está registrado.
            AppException: 500 si ocurre un error durante la creación.
        """
        await self._check_email_available(payload.admin_email)

        try:
            async with self.session.begin():
                client_id = await self._create_client(payload)
                user_id = await self._create_admin_user(client_id, payload)
                await self._create_default_agent_config(client_id)
                await self._create_token_budget(client_id)

        except AppException:
            raise
        except Exception as exc:
            raise AppException(
                status_code=500,
                detail="Error interno durante el registro. Intente nuevamente.",
                error_code="ONBOARDING_FAILED",
            ) from exc

        access_token = create_access_token(
            subject=str(user_id),
            client_id=str(client_id),
            role="admin",
        )
        refresh_token = create_refresh_token(subject=str(user_id))

        verification_token = self._generate_verification_token()
        send_verification_email.delay(
            email=payload.admin_email,
            full_name=payload.admin_full_name,
            token=verification_token,
            language=payload.language,
        )

        return OnboardingResponse(
            client_id=client_id,
            user_id=user_id,
            access_token=access_token,
            refresh_token=refresh_token,
        )

    async def _check_email_available(self, email: str) -> None:
        """Verifica que el email no esté registrado previamente.

        Args:
            email: Dirección de correo electrónico a verificar.

        Raises:
            AppException: 409 si el email ya existe en la tabla users.
        """
        result = await self.session.execute(
            text("SELECT id FROM users WHERE email = :email"),
            {"email": email},
        )
        if result.scalar_one_or_none() is not None:
            raise AppException(
                status_code=409,
                detail="El correo electrónico ya está registrado.",
                error_code="EMAIL_ALREADY_EXISTS",
            )

    async def _create_client(self, payload: OnboardingRequest) -> uuid.UUID:
        """Crea el registro del cliente (tenant) en la tabla ``clients``.

        Args:
            payload: Datos del onboarding con información del negocio.

        Returns:
            UUID del cliente creado.
        """
        client_id = uuid.uuid4()
        default_settings = self._build_default_settings(payload)

        await self.session.execute(
            text("""
                INSERT INTO clients (
                    id, business_name, business_type, is_active,
                    settings, onboarding_completed_at, created_at, updated_at
                ) VALUES (
                    :id, :business_name, :business_type, TRUE,
                    :settings::jsonb, :completed_at, NOW(), NOW()
                )
            """),
            {
                "id": client_id,
                "business_name": payload.business_name,
                "business_type": payload.business_type.value,
                "settings": self._serialize_json(default_settings),
                "completed_at": datetime.now(timezone.utc),
            },
        )
        return client_id

    async def _create_admin_user(
        self,
        client_id: uuid.UUID,
        payload: OnboardingRequest,
    ) -> uuid.UUID:
        """Crea el usuario administrador asociado al cliente.

        Args:
            client_id: UUID del tenant recién creado.
            payload: Datos del onboarding con credenciales del admin.

        Returns:
            UUID del usuario administrador creado.
        """
        user_id = uuid.uuid4()
        hashed_pw = hash_password(payload.admin_password)

        await self.session.execute(
            text("""
                INSERT INTO users (
                    id, client_id, email, hashed_password, full_name,
                    role, is_active, email_verified, phone, created_at, updated_at
                ) VALUES (
                    :id, :client_id, :email, :hashed_password, :full_name,
                    'admin', TRUE, FALSE, :phone, NOW(), NOW()
                )
            """),
            {
                "id": user_id,
                "client_id": client_id,
                "email": payload.admin_email,
                "hashed_password": hashed_pw,
                "full_name": payload.admin_full_name,
                "phone": payload.phone,
            },
        )
        return user_id

    async def _create_default_agent_config(self, client_id: uuid.UUID) -> None:
        """Crea la configuración por defecto del agente conversacional.

        Args:
            client_id: UUID del tenant.
        """
        config_id = uuid.uuid4()
        await self.session.execute(
            text("""
                INSERT INTO agent_configs (
                    id, client_id, system_prompt, model_name,
                    temperature, max_tokens, is_active, created_at, updated_at
                ) VALUES (
                    :id, :client_id, :system_prompt, :model_name,
                    :temperature, :max_tokens, TRUE, NOW(), NOW()
                )
            """),
            {
                "id": config_id,
                "client_id": client_id,
                "system_prompt": self.DEFAULT_SYSTEM_PROMPT,
                "model_name": settings.DEFAULT_MODEL_NAME,
                "temperature": 0.7,
                "max_tokens": 1024,
            },
        )

    async def _create_token_budget(self, client_id: uuid.UUID) -> None:
        """Asigna el presupuesto de tokens del free tier al cliente.

        Args:
            client_id: UUID del tenant.
        """
        budget_id = uuid.uuid4()
        await self.session.execute(
            text("""
                INSERT INTO token_budgets (
                    id, client_id, monthly_limit, tokens_used,
                    period_start, period_end, created_at
                ) VALUES (
                    :id, :client_id, :monthly_limit, 0,
                    DATE_TRUNC('month', NOW()),
                    DATE_TRUNC('month', NOW()) + INTERVAL '1 month',
                    NOW()
                )
            """),
            {
                "id": budget_id,
                "client_id": client_id,
                "monthly_limit": self.DEFAULT_TOKEN_BUDGET,
            },
        )

    def _build_default_settings(self, payload: OnboardingRequest) -> dict[str, Any]:
        """Construye el JSONB de settings por defecto del cliente.

        Args:
            payload: Datos del onboarding.

        Returns:
            Diccionario con la configuración inicial.
        """
        return {
            "business_profile": {
                "business_name": payload.business_name,
                "business_type": payload.business_type.value,
                "phone": payload.phone,
                "country": payload.country,
                "language": payload.language,
                "welcome_message": (
                    f"¡Hola! Bienvenido a {payload.business_name}. "
                    "¿En qué puedo ayudarte hoy?"
                ),
                "farewell_message": (
                    "¡Gracias por comunicarte con nosotros! "
                    "Que tengas un excelente día."
                ),
                "out_of_hours_message": (
                    "En este momento estamos fuera de nuestro horario de atención. "
                    "Déjanos tu mensaje y te responderemos lo antes posible."
                ),
            },
            "branding": {
                "primary_color": "#2563EB",
                "secondary_color": "#1E40AF",
                "logo_url": None,
            },
            "notifications": {
                "email_enabled": True,
                "webhook_enabled": False,
            },
        }

    @staticmethod
    def _generate_verification_token() -> str:
        """Genera un token único para verificación de email.

        Returns:
            Token UUID como cadena de texto.
        """
        return str(uuid.uuid4())

    @staticmethod
    def _serialize_json(data: dict[str, Any]) -> str:
        """Serializa un diccionario a JSON string para PostgreSQL.

        Args:
            data: Diccionario a serializar.

        Returns:
            Cadena JSON.
        """
        import json
        return json.dumps(data, ensure_ascii=False)
```

### 2.5 Tarea Celery para Verificación de Email

```python
# app/tasks/email_tasks.py
"""Tareas Celery para envío de correos electrónicos."""

from celery import shared_task

from app.core.config import settings
from app.services.email_service import EmailService


@shared_task(
    bind=True,
    name="tasks.send_verification_email",
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def send_verification_email(
    self,
    email: str,
    full_name: str,
    token: str,
    language: str = "es",
) -> dict[str, str]:
    """Envía correo de verificación al usuario recién registrado.

    Args:
        self: Instancia de la tarea Celery (bind=True).
        email: Dirección de correo del destinatario.
        full_name: Nombre completo del usuario.
        token: Token de verificación único.
        language: Código de idioma para la plantilla del correo.

    Returns:
        Diccionario con estado del envío.

    Raises:
        self.retry: Re-encola la tarea si falla el envío.
    """
    try:
        verification_url = (
            f"{settings.FRONTEND_BASE_URL}/verify-email?token={token}"
        )

        email_service = EmailService()
        email_service.send_template_email(
            to_email=email,
            subject=_get_subject(language),
            template_name="email_verification",
            context={
                "full_name": full_name,
                "verification_url": verification_url,
                "platform_name": settings.PLATFORM_NAME,
            },
            language=language,
        )

        return {"status": "sent", "email": email}

    except Exception as exc:
        raise self.retry(exc=exc)


def _get_subject(language: str) -> str:
    """Retorna el asunto del correo según el idioma.

    Args:
        language: Código ISO 639-1 del idioma.

    Returns:
        Asunto del correo de verificación.
    """
    subjects = {
        "es": "Verifica tu correo electrónico",
        "en": "Verify your email address",
        "pt": "Verifique seu endereço de e-mail",
    }
    return subjects.get(language, subjects["es"])


@shared_task(
    bind=True,
    name="tasks.send_suspension_notification",
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def send_suspension_notification(
    self,
    admin_emails: list[str],
    business_name: str,
    suspension_reason: str,
    alert_message: str,
) -> dict[str, str]:
    """Envía notificación de suspensión a los administradores del cliente.

    Args:
        self: Instancia de la tarea Celery (bind=True).
        admin_emails: Lista de correos de administradores.
        business_name: Nombre del negocio suspendido.
        suspension_reason: Razón de la suspensión.
        alert_message: Mensaje de alerta configurado.

    Returns:
        Diccionario con estado del envío.

    Raises:
        self.retry: Re-encola la tarea si falla el envío.
    """
    try:
        email_service = EmailService()
        for email in admin_emails:
            email_service.send_template_email(
                to_email=email,
                subject=f"Aviso de suspensión: {business_name}",
                template_name="client_suspension",
                context={
                    "business_name": business_name,
                    "suspension_reason": suspension_reason,
                    "alert_message": alert_message,
                    "platform_name": settings.PLATFORM_NAME,
                    "support_email": settings.SUPPORT_EMAIL,
                },
                language="es",
            )

        return {"status": "sent", "count": len(admin_emails)}

    except Exception as exc:
        raise self.retry(exc=exc)


@shared_task(
    bind=True,
    name="tasks.send_payment_alert",
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def send_payment_alert(
    self,
    admin_emails: list[str],
    business_name: str,
    days_remaining: int,
    alert_message_template: str,
) -> dict[str, str]:
    """Envía alerta de pago pendiente a los administradores del cliente.

    Args:
        self: Instancia de la tarea Celery (bind=True).
        admin_emails: Lista de correos de administradores.
        business_name: Nombre del negocio.
        days_remaining: Días restantes antes de la suspensión.
        alert_message_template: Plantilla del mensaje con variables.

    Returns:
        Diccionario con estado del envío.

    Raises:
        self.retry: Re-encola la tarea si falla el envío.
    """
    try:
        message = alert_message_template.format(
            business_name=business_name,
            days_remaining=days_remaining,
        )

        email_service = EmailService()
        for email in admin_emails:
            email_service.send_template_email(
                to_email=email,
                subject=f"Alerta de pago: {business_name} — {days_remaining} días restantes",
                template_name="payment_alert",
                context={
                    "business_name": business_name,
                    "days_remaining": days_remaining,
                    "alert_message": message,
                    "platform_name": settings.PLATFORM_NAME,
                    "support_email": settings.SUPPORT_EMAIL,
                },
                language="es",
            )

        return {"status": "sent", "count": len(admin_emails)}

    except Exception as exc:
        raise self.retry(exc=exc)
```

### 2.6 Endpoint de Verificación de Email

```python
# app/api/v1/endpoints/email_verification.py
"""Endpoint para verificación de correo electrónico."""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.exceptions import AppException

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


@router.get(
    "/verify-email",
    status_code=status.HTTP_200_OK,
    summary="Verificar correo electrónico",
)
async def verify_email(
    token: str = Query(..., description="Token de verificación enviado por email"),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, str]:
    """Verifica el correo electrónico del usuario mediante token.

    Args:
        token: Token de verificación UUID.
        session: Sesión async de SQLAlchemy.

    Returns:
        Mensaje de confirmación.

    Raises:
        AppException: 400 si el token es inválido o ya fue usado.
    """
    result = await session.execute(
        text("""
            UPDATE users
            SET email_verified = TRUE, updated_at = NOW()
            WHERE verification_token = :token
              AND email_verified = FALSE
            RETURNING id
        """),
        {"token": token},
    )
    await session.commit()

    if result.scalar_one_or_none() is None:
        raise AppException(
            status_code=400,
            detail="Token de verificación inválido o ya utilizado.",
            error_code="INVALID_VERIFICATION_TOKEN",
        )

    return {"message": "Correo electrónico verificado exitosamente."}
```

### 2.7 Rate Limiter

```python
# app/core/rate_limit.py
"""Decorador de rate limiting basado en Redis."""

import functools
from typing import Any, Callable

from fastapi import Request

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.redis_client import get_redis


def rate_limit(
    max_requests: int,
    window_seconds: int,
    key_func: str = "ip",
) -> Callable[..., Any]:
    """Decorador para aplicar rate limiting a endpoints.

    Utiliza Redis con ventana deslizante para controlar la tasa de
    solicitudes por IP o por otro criterio configurable.

    Args:
        max_requests: Número máximo de solicitudes en la ventana.
        window_seconds: Duración de la ventana en segundos.
        key_func: Función para generar la clave ('ip' o 'user').

    Returns:
        Decorador configurado con los parámetros dados.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            request: Request | None = kwargs.get("request")
            if request is None:
                for arg in args:
                    if isinstance(arg, Request):
                        request = arg
                        break

            if request is None:
                return await func(*args, **kwargs)

            if key_func == "ip":
                client_ip = request.client.host if request.client else "unknown"
                cache_key = f"rate_limit:{func.__name__}:{client_ip}"
            else:
                cache_key = f"rate_limit:{func.__name__}:global"

            redis = await get_redis()
            current_count = await redis.incr(cache_key)

            if current_count == 1:
                await redis.expire(cache_key, window_seconds)

            if current_count > max_requests:
                ttl = await redis.ttl(cache_key)
                raise AppException(
                    status_code=429,
                    detail=(
                        f"Demasiadas solicitudes. "
                        f"Intente nuevamente en {ttl} segundos."
                    ),
                    error_code="RATE_LIMIT_EXCEEDED",
                )

            return await func(*args, **kwargs)

        return wrapper

    return decorator
```

### 2.8 Registro del Router

```python
# En app/api/v1/router.py — agregar:

from app.api.v1.endpoints.onboarding import router as onboarding_router
from app.api.v1.endpoints.email_verification import (
    router as email_verification_router,
)

# Rutas públicas (sin autenticación)
api_router.include_router(onboarding_router)
api_router.include_router(email_verification_router)
```

---

## 3. Feature 2: Sección de Personalización del Negocio

### 3.1 Descripción General

API completa para que el administrador del tenant gestione:

- Perfil empresarial (datos, horarios, redes sociales).
- Branding (logo, colores).
- Mensajes personalizados del agente (bienvenida, despedida, fuera de horario).
- Base de conocimientos (documentos para RAG).

Todos los datos del perfil se almacenan en `clients.settings` (columna JSONB existente en el DDL). Los mensajes se inyectan dinámicamente al `system_prompt` del agente.

### 3.2 Esquemas Pydantic

```python
# app/schemas/business_profile.py
"""Esquemas Pydantic v2 para gestión de perfil empresarial."""

from datetime import time
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class SocialMedia(BaseModel):
    """Redes sociales del negocio.

    Attributes:
        facebook: URL del perfil de Facebook.
        instagram: Handle de Instagram (sin @).
        whatsapp: Número de WhatsApp con código de país.
        twitter: Handle de Twitter/X (sin @).
    """

    facebook: Optional[str] = None
    instagram: Optional[str] = None
    whatsapp: Optional[str] = None
    twitter: Optional[str] = None


class DaySchedule(BaseModel):
    """Horario de un día específico.

    Attributes:
        is_open: Si el negocio opera ese día.
        open_time: Hora de apertura (HH:MM).
        close_time: Hora de cierre (HH:MM).
    """

    is_open: bool = True
    open_time: Optional[str] = Field(
        default="08:00",
        pattern=r"^\d{2}:\d{2}$",
    )
    close_time: Optional[str] = Field(
        default="18:00",
        pattern=r"^\d{2}:\d{2}$",
    )


class OperatingHours(BaseModel):
    """Horario de operación semanal del negocio.

    Attributes:
        monday: Horario del lunes.
        tuesday: Horario del martes.
        wednesday: Horario del miércoles.
        thursday: Horario del jueves.
        friday: Horario del viernes.
        saturday: Horario del sábado.
        sunday: Horario del domingo.
    """

    monday: DaySchedule = Field(default_factory=DaySchedule)
    tuesday: DaySchedule = Field(default_factory=DaySchedule)
    wednesday: DaySchedule = Field(default_factory=DaySchedule)
    thursday: DaySchedule = Field(default_factory=DaySchedule)
    friday: DaySchedule = Field(default_factory=DaySchedule)
    saturday: DaySchedule = Field(
        default_factory=lambda: DaySchedule(is_open=False)
    )
    sunday: DaySchedule = Field(
        default_factory=lambda: DaySchedule(is_open=False)
    )


class BusinessProfileRequest(BaseModel):
    """Esquema para actualizar el perfil empresarial.

    Attributes:
        business_name: Nombre comercial del negocio.
        business_type: Categoría del negocio.
        description: Descripción breve del negocio.
        address: Dirección física.
        city: Ciudad.
        state: Departamento o estado.
        country: Código ISO del país.
        postal_code: Código postal.
        phone: Teléfono de contacto.
        email: Correo electrónico de contacto.
        website: URL del sitio web.
        social_media: Redes sociales.
        operating_hours: Horario de operación semanal.
        timezone: Zona horaria IANA (ej: America/Bogota).
        primary_color: Color primario en hexadecimal.
        secondary_color: Color secundario en hexadecimal.
        welcome_message: Mensaje de bienvenida del agente.
        farewell_message: Mensaje de despedida del agente.
        out_of_hours_message: Mensaje fuera de horario del agente.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    business_name: Optional[str] = Field(default=None, max_length=255)
    business_type: Optional[str] = Field(default=None, max_length=50)
    description: Optional[str] = Field(default=None, max_length=1000)
    address: Optional[str] = Field(default=None, max_length=500)
    city: Optional[str] = Field(default=None, max_length=100)
    state: Optional[str] = Field(default=None, max_length=100)
    country: Optional[str] = Field(default=None, max_length=2)
    postal_code: Optional[str] = Field(default=None, max_length=20)
    phone: Optional[str] = Field(default=None, max_length=20)
    email: Optional[str] = Field(default=None, max_length=255)
    website: Optional[str] = Field(default=None, max_length=500)
    social_media: Optional[SocialMedia] = None
    operating_hours: Optional[OperatingHours] = None
    timezone: Optional[str] = Field(
        default=None,
        max_length=50,
        examples=["America/Bogota"],
    )
    primary_color: Optional[str] = Field(
        default=None,
        pattern=r"^#[0-9A-Fa-f]{6}$",
        examples=["#2563EB"],
    )
    secondary_color: Optional[str] = Field(
        default=None,
        pattern=r"^#[0-9A-Fa-f]{6}$",
        examples=["#1E40AF"],
    )
    welcome_message: Optional[str] = Field(default=None, max_length=500)
    farewell_message: Optional[str] = Field(default=None, max_length=500)
    out_of_hours_message: Optional[str] = Field(default=None, max_length=500)


class BusinessProfileResponse(BaseModel):
    """Esquema de respuesta con perfil empresarial completo.

    Attributes:
        business_name: Nombre comercial del negocio.
        business_type: Categoría del negocio.
        description: Descripción breve del negocio.
        address: Dirección física.
        city: Ciudad.
        state: Departamento o estado.
        country: Código ISO del país.
        postal_code: Código postal.
        phone: Teléfono de contacto.
        email: Correo electrónico de contacto.
        website: URL del sitio web.
        social_media: Redes sociales.
        operating_hours: Horario de operación semanal.
        timezone: Zona horaria IANA.
        logo_url: URL del logo almacenado.
        primary_color: Color primario en hexadecimal.
        secondary_color: Color secundario en hexadecimal.
        welcome_message: Mensaje de bienvenida del agente.
        farewell_message: Mensaje de despedida del agente.
        out_of_hours_message: Mensaje fuera de horario del agente.
    """

    business_name: Optional[str] = None
    business_type: Optional[str] = None
    description: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    postal_code: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    social_media: Optional[SocialMedia] = None
    operating_hours: Optional[OperatingHours] = None
    timezone: Optional[str] = None
    logo_url: Optional[str] = None
    primary_color: Optional[str] = None
    secondary_color: Optional[str] = None
    welcome_message: Optional[str] = None
    farewell_message: Optional[str] = None
    out_of_hours_message: Optional[str] = None


class LogoUploadResponse(BaseModel):
    """Respuesta de carga de logo.

    Attributes:
        logo_url: URL pública del logo almacenado en Supabase Storage.
        message: Mensaje de confirmación.
    """

    logo_url: str
    message: str = "Logo actualizado exitosamente."
```

### 3.3 Esquemas de Base de Conocimientos

```python
# app/schemas/knowledge_base.py
"""Esquemas Pydantic v2 para gestión de base de conocimientos."""

from datetime import datetime
from enum import StrEnum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class DocumentStatus(StrEnum):
    """Estados posibles de un documento en el pipeline."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class DocumentType(StrEnum):
    """Tipos de archivo aceptados para la base de conocimientos."""

    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    CSV = "csv"


class DocumentUploadResponse(BaseModel):
    """Respuesta de carga de documento.

    Attributes:
        document_id: UUID del documento creado.
        filename: Nombre original del archivo.
        status: Estado actual del procesamiento.
        message: Mensaje informativo.
    """

    document_id: UUID
    filename: str
    status: DocumentStatus = DocumentStatus.PENDING
    message: str = "Documento cargado. El procesamiento iniciará en breve."


class DocumentListItem(BaseModel):
    """Elemento de la lista de documentos del tenant.

    Attributes:
        id: UUID del documento.
        filename: Nombre original del archivo.
        file_type: Tipo de archivo.
        file_size_bytes: Tamaño en bytes del archivo original.
        status: Estado actual del procesamiento.
        chunk_count: Número de chunks generados (si aplica).
        created_at: Fecha de carga.
        updated_at: Última actualización.
    """

    id: UUID
    filename: str
    file_type: DocumentType
    file_size_bytes: int
    status: DocumentStatus
    chunk_count: Optional[int] = 0
    created_at: datetime
    updated_at: datetime


class DocumentListResponse(BaseModel):
    """Respuesta paginada de lista de documentos.

    Attributes:
        documents: Lista de documentos.
        total: Total de documentos del tenant.
        page: Página actual.
        page_size: Tamaño de página.
    """

    documents: list[DocumentListItem]
    total: int
    page: int = 1
    page_size: int = 20
```

### 3.4 Endpoints de Perfil de Negocio

```python
# app/api/v1/endpoints/business_profile.py
"""Endpoints para gestión del perfil empresarial del tenant."""

import uuid

from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.dependencies import get_current_user, require_roles
from app.core.exceptions import AppException
from app.models.user import User
from app.schemas.business_profile import (
    BusinessProfileRequest,
    BusinessProfileResponse,
    LogoUploadResponse,
)
from app.services.business_profile_service import BusinessProfileService

router = APIRouter(
    prefix="/settings/business-profile",
    tags=["business-profile"],
    dependencies=[Depends(require_roles(["admin", "super_admin"]))],
)

ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/svg+xml"}
MAX_LOGO_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB


@router.get(
    "",
    response_model=BusinessProfileResponse,
    summary="Obtener perfil empresarial",
    description="Retorna el perfil empresarial completo del tenant actual.",
)
async def get_business_profile(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> BusinessProfileResponse:
    """Obtiene el perfil empresarial del tenant del usuario actual.

    Args:
        current_user: Usuario autenticado con información del tenant.
        session: Sesión async de SQLAlchemy.

    Returns:
        BusinessProfileResponse con datos del perfil.
    """
    service = BusinessProfileService(session)
    return await service.get_profile(current_user.client_id)


@router.put(
    "",
    response_model=BusinessProfileResponse,
    summary="Actualizar perfil empresarial",
    description="Actualiza parcial o totalmente el perfil empresarial del tenant.",
)
async def update_business_profile(
    payload: BusinessProfileRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> BusinessProfileResponse:
    """Actualiza el perfil empresarial del tenant.

    Solo se actualizan los campos proporcionados (merge parcial sobre
    el JSONB ``clients.settings``).

    Args:
        payload: Campos a actualizar.
        current_user: Usuario autenticado.
        session: Sesión async de SQLAlchemy.

    Returns:
        BusinessProfileResponse actualizado.
    """
    service = BusinessProfileService(session)
    return await service.update_profile(current_user.client_id, payload)


@router.post(
    "/logo",
    response_model=LogoUploadResponse,
    status_code=status.HTTP_200_OK,
    summary="Subir logo del negocio",
    description="Sube un logo para el negocio. Formatos: PNG, JPEG, WebP, SVG. Máx: 5 MB.",
)
async def upload_logo(
    file: UploadFile = File(..., description="Archivo de imagen del logo"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> LogoUploadResponse:
    """Sube el logo del negocio a Supabase Storage.

    Args:
        file: Archivo de imagen cargado.
        current_user: Usuario autenticado.
        session: Sesión async de SQLAlchemy.

    Returns:
        LogoUploadResponse con la URL del logo almacenado.

    Raises:
        AppException: 400 si el tipo de archivo no es permitido.
        AppException: 413 si el archivo supera el tamaño máximo.
    """
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise AppException(
            status_code=400,
            detail=(
                f"Tipo de archivo no permitido: {file.content_type}. "
                f"Tipos permitidos: {', '.join(ALLOWED_IMAGE_TYPES)}"
            ),
            error_code="INVALID_FILE_TYPE",
        )

    content = await file.read()
    if len(content) > MAX_LOGO_SIZE_BYTES:
        raise AppException(
            status_code=413,
            detail=f"El archivo supera el tamaño máximo de {MAX_LOGO_SIZE_BYTES // (1024*1024)} MB.",
            error_code="FILE_TOO_LARGE",
        )

    service = BusinessProfileService(session)
    return await service.upload_logo(
        client_id=current_user.client_id,
        file_content=content,
        filename=file.filename or "logo.png",
        content_type=file.content_type or "image/png",
    )
```

### 3.5 Endpoints de Base de Conocimientos

```python
# app/api/v1/endpoints/knowledge_base.py
"""Endpoints para gestión de la base de conocimientos del tenant."""

from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.dependencies import get_current_user, require_roles
from app.core.exceptions import AppException
from app.models.user import User
from app.schemas.knowledge_base import (
    DocumentListResponse,
    DocumentUploadResponse,
)
from app.services.knowledge_base_service import KnowledgeBaseService

router = APIRouter(
    prefix="/settings/knowledge-base",
    tags=["knowledge-base"],
    dependencies=[Depends(require_roles(["admin", "super_admin"]))],
)

ALLOWED_DOC_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/plain": "txt",
    "text/csv": "csv",
}
MAX_DOC_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB


@router.post(
    "/upload",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Subir documento a la base de conocimientos",
    description="Sube un documento para procesamiento RAG. Formatos: PDF, DOCX, TXT, CSV. Máx: 20 MB.",
)
async def upload_document(
    file: UploadFile = File(..., description="Documento a procesar"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> DocumentUploadResponse:
    """Sube un documento para procesamiento RAG.

    El documento se almacena en Supabase Storage y se encola una tarea
    Celery para su procesamiento (chunking + embedding). Esta funcionalidad
    se conecta al pipeline de documentos del Sprint 5.

    Args:
        file: Archivo del documento.
        current_user: Usuario autenticado.
        session: Sesión async de SQLAlchemy.

    Returns:
        DocumentUploadResponse con ID del documento y estado.

    Raises:
        AppException: 400 si el tipo de archivo no es permitido.
        AppException: 413 si el archivo supera el tamaño máximo.
    """
    if file.content_type not in ALLOWED_DOC_TYPES:
        raise AppException(
            status_code=400,
            detail=(
                f"Tipo de archivo no permitido: {file.content_type}. "
                f"Tipos permitidos: PDF, DOCX, TXT, CSV."
            ),
            error_code="INVALID_FILE_TYPE",
        )

    content = await file.read()
    if len(content) > MAX_DOC_SIZE_BYTES:
        raise AppException(
            status_code=413,
            detail=f"El archivo supera el tamaño máximo de {MAX_DOC_SIZE_BYTES // (1024*1024)} MB.",
            error_code="FILE_TOO_LARGE",
        )

    service = KnowledgeBaseService(session)
    return await service.upload_document(
        client_id=current_user.client_id,
        file_content=content,
        filename=file.filename or "document",
        file_type=ALLOWED_DOC_TYPES[file.content_type],
        content_type=file.content_type,
    )


@router.get(
    "",
    response_model=DocumentListResponse,
    summary="Listar documentos de la base de conocimientos",
    description="Retorna la lista paginada de documentos del tenant.",
)
async def list_documents(
    page: int = Query(default=1, ge=1, description="Número de página"),
    page_size: int = Query(default=20, ge=1, le=100, description="Tamaño de página"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> DocumentListResponse:
    """Lista todos los documentos de la base de conocimientos del tenant.

    Args:
        page: Número de página (1-based).
        page_size: Cantidad de resultados por página.
        current_user: Usuario autenticado.
        session: Sesión async de SQLAlchemy.

    Returns:
        DocumentListResponse con lista paginada.
    """
    service = KnowledgeBaseService(session)
    return await service.list_documents(
        client_id=current_user.client_id,
        page=page,
        page_size=page_size,
    )


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_200_OK,
    summary="Eliminar documento de la base de conocimientos",
    description="Elimina un documento y todos sus chunks del vector store.",
)
async def delete_document(
    document_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, str]:
    """Elimina un documento y sus chunks asociados.

    Args:
        document_id: UUID del documento a eliminar.
        current_user: Usuario autenticado.
        session: Sesión async de SQLAlchemy.

    Returns:
        Mensaje de confirmación.

    Raises:
        AppException: 404 si el documento no existe o no pertenece al tenant.
    """
    service = KnowledgeBaseService(session)
    await service.delete_document(
        client_id=current_user.client_id,
        document_id=document_id,
    )
    return {"message": "Documento eliminado exitosamente."}
```

### 3.6 Servicio de Perfil de Negocio

```python
# app/services/business_profile_service.py
"""Servicio de negocio para gestión del perfil empresarial."""

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppException
from app.core.storage import SupabaseStorageClient
from app.schemas.business_profile import (
    BusinessProfileRequest,
    BusinessProfileResponse,
    LogoUploadResponse,
)


class BusinessProfileService:
    """Gestiona las operaciones CRUD del perfil empresarial del tenant.

    Los datos del perfil se almacenan en la columna ``clients.settings``
    (JSONB) bajo la estructura:

    .. code-block:: json

        {
            "business_profile": { ... },
            "branding": { "primary_color": ..., "secondary_color": ..., "logo_url": ... }
        }

    Attributes:
        session: Sesión async de SQLAlchemy.
        storage: Cliente de Supabase Storage para carga de archivos.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Inicializa el servicio.

        Args:
            session: Sesión async de SQLAlchemy.
        """
        self.session = session
        self.storage = SupabaseStorageClient()

    async def get_profile(self, client_id: uuid.UUID) -> BusinessProfileResponse:
        """Obtiene el perfil empresarial del tenant.

        Args:
            client_id: UUID del tenant.

        Returns:
            BusinessProfileResponse con los datos del perfil.

        Raises:
            AppException: 404 si el cliente no existe.
        """
        await self.session.execute(
            text("SET LOCAL app.current_client_id = :client_id"),
            {"client_id": str(client_id)},
        )

        result = await self.session.execute(
            text("""
                SELECT settings
                FROM clients
                WHERE id = :client_id AND client_id = :client_id
            """),
            {"client_id": client_id},
        )
        row = result.scalar_one_or_none()

        if row is None:
            raise AppException(
                status_code=404,
                detail="Cliente no encontrado.",
                error_code="CLIENT_NOT_FOUND",
            )

        settings_data = row if isinstance(row, dict) else json.loads(row)
        return self._map_settings_to_response(settings_data)

    async def update_profile(
        self,
        client_id: uuid.UUID,
        payload: BusinessProfileRequest,
    ) -> BusinessProfileResponse:
        """Actualiza el perfil empresarial del tenant.

        Realiza un merge parcial sobre el JSONB de settings, preservando
        las claves no incluidas en el payload.

        Args:
            client_id: UUID del tenant.
            payload: Campos a actualizar.

        Returns:
            BusinessProfileResponse actualizado.
        """
        await self.session.execute(
            text("SET LOCAL app.current_client_id = :client_id"),
            {"client_id": str(client_id)},
        )

        update_data = payload.model_dump(exclude_none=True)
        if not update_data:
            return await self.get_profile(client_id)

        profile_fields = {}
        branding_fields = {}

        branding_keys = {"primary_color", "secondary_color"}
        for key, value in update_data.items():
            if key in branding_keys:
                branding_fields[key] = value
            elif key in ("social_media", "operating_hours") and isinstance(value, dict):
                profile_fields[key] = value
            else:
                profile_fields[key] = value

        merge_obj: dict[str, Any] = {}
        if profile_fields:
            merge_obj["business_profile"] = profile_fields
        if branding_fields:
            merge_obj["branding"] = branding_fields

        merge_json = json.dumps(merge_obj, ensure_ascii=False)

        await self.session.execute(
            text("""
                UPDATE clients
                SET settings = settings || :merge_data::jsonb,
                    updated_at = NOW()
                WHERE id = :client_id AND client_id = :client_id
            """),
            {"client_id": client_id, "merge_data": merge_json},
        )
        await self.session.commit()

        if payload.business_name is not None:
            await self.session.execute(
                text("""
                    UPDATE clients
                    SET business_name = :name, updated_at = NOW()
                    WHERE id = :client_id AND client_id = :client_id
                """),
                {"client_id": client_id, "name": payload.business_name},
            )
            await self.session.commit()

        return await self.get_profile(client_id)

    async def upload_logo(
        self,
        client_id: uuid.UUID,
        file_content: bytes,
        filename: str,
        content_type: str,
    ) -> LogoUploadResponse:
        """Sube el logo del negocio a Supabase Storage y actualiza settings.

        Args:
            client_id: UUID del tenant.
            file_content: Contenido binario del archivo.
            filename: Nombre original del archivo.
            content_type: MIME type del archivo.

        Returns:
            LogoUploadResponse con la URL del logo.
        """
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "png"
        storage_path = f"logos/{client_id}/logo.{ext}"

        logo_url = await self.storage.upload(
            bucket="business-assets",
            path=storage_path,
            content=file_content,
            content_type=content_type,
        )

        await self.session.execute(
            text("SET LOCAL app.current_client_id = :client_id"),
            {"client_id": str(client_id)},
        )

        logo_data = json.dumps(
            {"branding": {"logo_url": logo_url}},
            ensure_ascii=False,
        )
        await self.session.execute(
            text("""
                UPDATE clients
                SET settings = settings || :logo_data::jsonb,
                    updated_at = NOW()
                WHERE id = :client_id AND client_id = :client_id
            """),
            {"client_id": client_id, "logo_data": logo_data},
        )
        await self.session.commit()

        return LogoUploadResponse(logo_url=logo_url)

    @staticmethod
    def _map_settings_to_response(
        settings_data: dict[str, Any],
    ) -> BusinessProfileResponse:
        """Mapea el JSONB de settings al esquema de respuesta.

        Args:
            settings_data: Diccionario de settings del cliente.

        Returns:
            BusinessProfileResponse mapeado.
        """
        profile = settings_data.get("business_profile", {})
        branding = settings_data.get("branding", {})

        return BusinessProfileResponse(
            business_name=profile.get("business_name"),
            business_type=profile.get("business_type"),
            description=profile.get("description"),
            address=profile.get("address"),
            city=profile.get("city"),
            state=profile.get("state"),
            country=profile.get("country"),
            postal_code=profile.get("postal_code"),
            phone=profile.get("phone"),
            email=profile.get("email"),
            website=profile.get("website"),
            social_media=profile.get("social_media"),
            operating_hours=profile.get("operating_hours"),
            timezone=profile.get("timezone"),
            logo_url=branding.get("logo_url"),
            primary_color=branding.get("primary_color"),
            secondary_color=branding.get("secondary_color"),
            welcome_message=profile.get("welcome_message"),
            farewell_message=profile.get("farewell_message"),
            out_of_hours_message=profile.get("out_of_hours_message"),
        )
```

### 3.7 Servicio de Base de Conocimientos

```python
# app/services/knowledge_base_service.py
"""Servicio para gestión de la base de conocimientos del tenant."""

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.storage import SupabaseStorageClient
from app.schemas.knowledge_base import (
    DocumentListItem,
    DocumentListResponse,
    DocumentUploadResponse,
)
from app.tasks.document_tasks import process_document


class KnowledgeBaseService:
    """Gestiona documentos para el pipeline RAG del tenant.

    Se conecta al pipeline de documentos del Sprint 5, exponiendo
    la funcionalidad de carga, listado y eliminación de documentos
    desde la sección de personalización del negocio.

    Attributes:
        session: Sesión async de SQLAlchemy.
        storage: Cliente de Supabase Storage.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Inicializa el servicio.

        Args:
            session: Sesión async de SQLAlchemy.
        """
        self.session = session
        self.storage = SupabaseStorageClient()

    async def upload_document(
        self,
        client_id: uuid.UUID,
        file_content: bytes,
        filename: str,
        file_type: str,
        content_type: str,
    ) -> DocumentUploadResponse:
        """Sube un documento y encola su procesamiento RAG.

        Args:
            client_id: UUID del tenant.
            file_content: Contenido binario del archivo.
            filename: Nombre original del archivo.
            file_type: Tipo normalizado (pdf, docx, txt, csv).
            content_type: MIME type del archivo.

        Returns:
            DocumentUploadResponse con ID y estado del documento.
        """
        document_id = uuid.uuid4()
        storage_path = f"documents/{client_id}/{document_id}/{filename}"

        file_url = await self.storage.upload(
            bucket="knowledge-base",
            path=storage_path,
            content=file_content,
            content_type=content_type,
        )

        await self.session.execute(
            text("SET LOCAL app.current_client_id = :client_id"),
            {"client_id": str(client_id)},
        )

        await self.session.execute(
            text("""
                INSERT INTO documents (
                    id, client_id, filename, file_type,
                    file_size_bytes, file_url, status,
                    created_at, updated_at
                ) VALUES (
                    :id, :client_id, :filename, :file_type,
                    :file_size, :file_url, 'pending',
                    NOW(), NOW()
                )
            """),
            {
                "id": document_id,
                "client_id": client_id,
                "filename": filename,
                "file_type": file_type,
                "file_size": len(file_content),
                "file_url": file_url,
            },
        )
        await self.session.commit()

        process_document.delay(
            document_id=str(document_id),
            client_id=str(client_id),
            file_url=file_url,
            file_type=file_type,
        )

        return DocumentUploadResponse(
            document_id=document_id,
            filename=filename,
        )

    async def list_documents(
        self,
        client_id: uuid.UUID,
        page: int = 1,
        page_size: int = 20,
    ) -> DocumentListResponse:
        """Lista los documentos de la base de conocimientos del tenant.

        Args:
            client_id: UUID del tenant.
            page: Número de página (1-based).
            page_size: Cantidad de resultados por página.

        Returns:
            DocumentListResponse con lista paginada.
        """
        await self.session.execute(
            text("SET LOCAL app.current_client_id = :client_id"),
            {"client_id": str(client_id)},
        )

        offset = (page - 1) * page_size

        count_result = await self.session.execute(
            text("""
                SELECT COUNT(*) FROM documents
                WHERE client_id = :client_id
            """),
            {"client_id": client_id},
        )
        total = count_result.scalar_one()

        result = await self.session.execute(
            text("""
                SELECT
                    id, filename, file_type, file_size_bytes,
                    status, chunk_count, created_at, updated_at
                FROM documents
                WHERE client_id = :client_id
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
            """),
            {
                "client_id": client_id,
                "limit": page_size,
                "offset": offset,
            },
        )

        documents = [
            DocumentListItem(
                id=row.id,
                filename=row.filename,
                file_type=row.file_type,
                file_size_bytes=row.file_size_bytes,
                status=row.status,
                chunk_count=row.chunk_count,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in result.fetchall()
        ]

        return DocumentListResponse(
            documents=documents,
            total=total,
            page=page,
            page_size=page_size,
        )

    async def delete_document(
        self,
        client_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> None:
        """Elimina un documento y sus chunks asociados.

        Args:
            client_id: UUID del tenant.
            document_id: UUID del documento a eliminar.

        Raises:
            AppException: 404 si el documento no existe o no pertenece al tenant.
        """
        await self.session.execute(
            text("SET LOCAL app.current_client_id = :client_id"),
            {"client_id": str(client_id)},
        )

        result = await self.session.execute(
            text("""
                SELECT file_url FROM documents
                WHERE id = :document_id AND client_id = :client_id
            """),
            {"document_id": document_id, "client_id": client_id},
        )
        row = result.scalar_one_or_none()

        if row is None:
            raise AppException(
                status_code=404,
                detail="Documento no encontrado.",
                error_code="DOCUMENT_NOT_FOUND",
            )

        await self.session.execute(
            text("""
                DELETE FROM document_chunks
                WHERE document_id = :document_id AND client_id = :client_id
            """),
            {"document_id": document_id, "client_id": client_id},
        )

        await self.session.execute(
            text("""
                DELETE FROM documents
                WHERE id = :document_id AND client_id = :client_id
            """),
            {"document_id": document_id, "client_id": client_id},
        )

        await self.session.commit()

        try:
            await self.storage.delete(bucket="knowledge-base", path=row)
        except Exception:
            pass  # Log pero no falla la operación
```

### 3.8 Registro de Routers

```python
# En app/api/v1/router.py — agregar:

from app.api.v1.endpoints.business_profile import router as business_profile_router
from app.api.v1.endpoints.knowledge_base import router as knowledge_base_router

# Rutas protegidas (requieren autenticación + RBAC)
api_router.include_router(business_profile_router)
api_router.include_router(knowledge_base_router)
```

---

## 4. Feature 3: Gestión de Clientes (Desactivación y Alertas de Pago)

### 4.1 Descripción General

Panel de super administración para:

- Listar todos los clientes con filtrado por estado.
- Ver detalles de un cliente con estadísticas de uso.
- Activar/desactivar clientes con razón de suspensión y mensaje de alerta.
- Configurar alertas de pago con días de anticipación y suspensión automática.
- Tarea periódica Celery Beat para verificación diaria de pagos.

### 4.2 Esquemas Pydantic

```python
# app/schemas/client_management.py
"""Esquemas Pydantic v2 para gestión de clientes por super admin."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class SuspensionReason(StrEnum):
    """Razones de suspensión de un cliente."""

    PAYMENT_OVERDUE = "payment_overdue"
    TERMS_VIOLATION = "terms_violation"
    ABUSE = "abuse"
    MANUAL = "manual"
    OTHER = "other"


class ClientStatusRequest(BaseModel):
    """Esquema para activar/desactivar un cliente.

    Attributes:
        is_active: Estado deseado del cliente.
        suspension_reason: Razón de la suspensión (requerida si is_active=False).
        alert_message: Mensaje de alerta para usuarios finales.
    """

    is_active: bool
    suspension_reason: Optional[SuspensionReason] = None
    alert_message: Optional[str] = Field(
        default=None,
        max_length=1000,
        examples=[
            "Su servicio será suspendido por falta de pago. "
            "Comuníquese con soporte para más información."
        ],
    )

    @field_validator("suspension_reason")
    @classmethod
    def validate_reason_on_deactivation(
        cls, v: Optional[SuspensionReason], info: Any
    ) -> Optional[SuspensionReason]:
        """Valida que la razón de suspensión se proporcione al desactivar."""
        if info.data.get("is_active") is False and v is None:
            raise ValueError(
                "suspension_reason es requerido cuando is_active es False."
            )
        return v


class ClientStatusResponse(BaseModel):
    """Respuesta de cambio de estado de un cliente.

    Attributes:
        client_id: UUID del cliente.
        is_active: Estado actual del cliente.
        suspension_reason: Razón de la suspensión.
        suspended_at: Fecha de suspensión.
        message: Mensaje de confirmación.
    """

    client_id: UUID
    is_active: bool
    suspension_reason: Optional[str] = None
    suspended_at: Optional[datetime] = None
    message: str


class PaymentAlertConfig(BaseModel):
    """Configuración de alertas de pago para un cliente.

    Attributes:
        alert_days_before_suspension: Lista de días antes de la
            suspensión en los que se enviarán alertas.
        alert_message_template: Plantilla del mensaje de alerta
            con variables {business_name} y {days_remaining}.
        suspension_date: Fecha programada para la suspensión automática.
    """

    alert_days_before_suspension: list[int] = Field(
        default=[7, 3, 1],
        examples=[[7, 3, 1]],
    )
    alert_message_template: str = Field(
        default=(
            "Estimado {business_name}, su servicio será suspendido "
            "en {days_remaining} día(s) por falta de pago. "
            "Por favor, regularice su situación."
        ),
        max_length=1000,
    )
    suspension_date: Optional[datetime] = None

    @field_validator("alert_days_before_suspension")
    @classmethod
    def validate_alert_days(cls, v: list[int]) -> list[int]:
        """Valida que los días sean positivos y estén ordenados."""
        if not all(d > 0 for d in v):
            raise ValueError("Todos los días deben ser positivos.")
        return sorted(v, reverse=True)


class ClientListFilter(StrEnum):
    """Filtros para listar clientes."""

    ALL = "all"
    ACTIVE = "active"
    INACTIVE = "inactive"


class ClientListItem(BaseModel):
    """Elemento de la lista de clientes.

    Attributes:
        id: UUID del cliente.
        business_name: Nombre del negocio.
        business_type: Tipo de negocio.
        is_active: Estado del cliente.
        suspension_reason: Razón de suspensión (si aplica).
        suspended_at: Fecha de suspensión (si aplica).
        created_at: Fecha de creación.
        users_count: Cantidad de usuarios del tenant.
        conversations_count: Total de conversaciones.
        tokens_used: Tokens consumidos en el período actual.
    """

    id: UUID
    business_name: str
    business_type: Optional[str] = None
    is_active: bool
    suspension_reason: Optional[str] = None
    suspended_at: Optional[datetime] = None
    created_at: datetime
    users_count: int = 0
    conversations_count: int = 0
    tokens_used: int = 0


class ClientListResponse(BaseModel):
    """Respuesta paginada de lista de clientes.

    Attributes:
        clients: Lista de clientes.
        total: Total de clientes que coinciden con el filtro.
        page: Página actual.
        page_size: Tamaño de página.
    """

    clients: list[ClientListItem]
    total: int
    page: int = 1
    page_size: int = 20


class ClientDetailResponse(BaseModel):
    """Respuesta detallada de un cliente con estadísticas.

    Attributes:
        id: UUID del cliente.
        business_name: Nombre del negocio.
        business_type: Tipo de negocio.
        is_active: Estado del cliente.
        suspension_reason: Razón de suspensión.
        alert_message: Mensaje de alerta configurado.
        suspended_at: Fecha de suspensión.
        suspension_date: Fecha programada de suspensión.
        payment_alert_config: Configuración de alertas de pago.
        settings: JSONB completo de settings.
        onboarding_completed_at: Fecha de completitud del onboarding.
        created_at: Fecha de creación.
        updated_at: Última actualización.
        usage_stats: Estadísticas de uso.
    """

    id: UUID
    business_name: str
    business_type: Optional[str] = None
    is_active: bool
    suspension_reason: Optional[str] = None
    alert_message: Optional[str] = None
    suspended_at: Optional[datetime] = None
    suspension_date: Optional[datetime] = None
    payment_alert_config: Optional[dict[str, Any]] = None
    settings: Optional[dict[str, Any]] = None
    onboarding_completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    usage_stats: dict[str, Any] = Field(default_factory=dict)
```

### 4.3 Endpoints de Gestión de Clientes

```python
# app/api/v1/endpoints/client_management.py
"""Endpoints de administración de clientes (super_admin)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.dependencies import get_current_user, require_roles
from app.models.user import User
from app.schemas.client_management import (
    ClientDetailResponse,
    ClientListFilter,
    ClientListResponse,
    ClientStatusRequest,
    ClientStatusResponse,
    PaymentAlertConfig,
)
from app.services.client_management_service import ClientManagementService

router = APIRouter(
    prefix="/admin/clients",
    tags=["client-management"],
    dependencies=[Depends(require_roles(["super_admin"]))],
)


@router.get(
    "",
    response_model=ClientListResponse,
    summary="Listar todos los clientes",
    description="Lista paginada de todos los clientes con filtrado por estado. Solo super_admin.",
)
async def list_clients(
    filter_status: ClientListFilter = Query(
        default=ClientListFilter.ALL,
        alias="status",
        description="Filtrar por estado del cliente",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(
        default=None,
        max_length=255,
        description="Buscar por nombre del negocio",
    ),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> ClientListResponse:
    """Lista todos los clientes registrados en la plataforma.

    Args:
        filter_status: Filtro por estado (all, active, inactive).
        page: Número de página.
        page_size: Tamaño de página.
        search: Texto de búsqueda por nombre de negocio.
        current_user: Super admin autenticado.
        session: Sesión async de SQLAlchemy.

    Returns:
        ClientListResponse con lista paginada.
    """
    service = ClientManagementService(session)
    return await service.list_clients(
        filter_status=filter_status,
        page=page,
        page_size=page_size,
        search=search,
    )


@router.get(
    "/{client_id}",
    response_model=ClientDetailResponse,
    summary="Obtener detalle de un cliente",
    description="Retorna los datos completos de un cliente con estadísticas de uso. Solo super_admin.",
)
async def get_client_detail(
    client_id: UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> ClientDetailResponse:
    """Obtiene los datos detallados de un cliente con estadísticas.

    Args:
        client_id: UUID del cliente.
        current_user: Super admin autenticado.
        session: Sesión async de SQLAlchemy.

    Returns:
        ClientDetailResponse con datos y estadísticas.

    Raises:
        AppException: 404 si el cliente no existe.
    """
    service = ClientManagementService(session)
    return await service.get_client_detail(client_id)


@router.patch(
    "/{client_id}/status",
    response_model=ClientStatusResponse,
    summary="Cambiar estado de un cliente",
    description="Activa o desactiva un cliente. Al desactivar, archiva conversaciones y notifica admins.",
)
async def update_client_status(
    client_id: UUID,
    payload: ClientStatusRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> ClientStatusResponse:
    """Activa o desactiva un cliente del sistema.

    Al desactivar:
    - ``clients.is_active`` se pone en False.
    - Todas las conversaciones se archivan.
    - Se configura ``alert_message`` para respuestas entrantes.
    - Se bloquean mensajes salientes (excepto alertas).
    - Se notifica a los administradores del tenant por email.

    Args:
        client_id: UUID del cliente.
        payload: Datos del cambio de estado.
        current_user: Super admin autenticado.
        session: Sesión async de SQLAlchemy.

    Returns:
        ClientStatusResponse con el nuevo estado.

    Raises:
        AppException: 404 si el cliente no existe.
    """
    service = ClientManagementService(session)
    return await service.update_client_status(client_id, payload)


@router.put(
    "/{client_id}/payment-alerts",
    response_model=dict[str, str],
    summary="Configurar alertas de pago",
    description="Configura alertas de pago y suspensión automática para un cliente.",
)
async def update_payment_alerts(
    client_id: UUID,
    payload: PaymentAlertConfig,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, str]:
    """Configura las alertas de pago para un cliente.

    Args:
        client_id: UUID del cliente.
        payload: Configuración de alertas.
        current_user: Super admin autenticado.
        session: Sesión async de SQLAlchemy.

    Returns:
        Mensaje de confirmación.

    Raises:
        AppException: 404 si el cliente no existe.
    """
    service = ClientManagementService(session)
    await service.update_payment_alerts(client_id, payload)
    return {"message": "Configuración de alertas actualizada exitosamente."}
```

### 4.4 Servicio de Gestión de Clientes

```python
# app/services/client_management_service.py
"""Servicio de negocio para gestión de clientes por super admin."""

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.schemas.client_management import (
    ClientDetailResponse,
    ClientListFilter,
    ClientListItem,
    ClientListResponse,
    ClientStatusRequest,
    ClientStatusResponse,
    PaymentAlertConfig,
)
from app.tasks.email_tasks import send_suspension_notification


class ClientManagementService:
    """Gestiona operaciones administrativas sobre clientes (tenants).

    Attributes:
        session: Sesión async de SQLAlchemy.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Inicializa el servicio.

        Args:
            session: Sesión async de SQLAlchemy.
        """
        self.session = session

    async def list_clients(
        self,
        filter_status: ClientListFilter = ClientListFilter.ALL,
        page: int = 1,
        page_size: int = 20,
        search: str | None = None,
    ) -> ClientListResponse:
        """Lista los clientes registrados con filtrado y paginación.

        Args:
            filter_status: Filtro por estado (all, active, inactive).
            page: Número de página (1-based).
            page_size: Cantidad de resultados por página.
            search: Texto de búsqueda por nombre de negocio.

        Returns:
            ClientListResponse con lista paginada de clientes.
        """
        conditions = []
        params: dict[str, Any] = {
            "limit": page_size,
            "offset": (page - 1) * page_size,
        }

        if filter_status == ClientListFilter.ACTIVE:
            conditions.append("c.is_active = TRUE")
        elif filter_status == ClientListFilter.INACTIVE:
            conditions.append("c.is_active = FALSE")

        if search:
            conditions.append("c.business_name ILIKE :search")
            params["search"] = f"%{search}%"

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        count_result = await self.session.execute(
            text(f"SELECT COUNT(*) FROM clients c {where_clause}"),
            params,
        )
        total = count_result.scalar_one()

        result = await self.session.execute(
            text(f"""
                SELECT
                    c.id, c.business_name, c.business_type,
                    c.is_active, c.suspension_reason, c.suspended_at,
                    c.created_at,
                    COALESCE(u.users_count, 0) AS users_count,
                    COALESCE(conv.conversations_count, 0) AS conversations_count,
                    COALESCE(tb.tokens_used, 0) AS tokens_used
                FROM clients c
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS users_count
                    FROM users WHERE client_id = c.id
                ) u ON TRUE
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS conversations_count
                    FROM conversations WHERE client_id = c.id
                ) conv ON TRUE
                LEFT JOIN LATERAL (
                    SELECT COALESCE(SUM(tokens_used), 0) AS tokens_used
                    FROM token_budgets WHERE client_id = c.id
                ) tb ON TRUE
                {where_clause}
                ORDER BY c.created_at DESC
                LIMIT :limit OFFSET :offset
            """),
            params,
        )

        clients = [
            ClientListItem(
                id=row.id,
                business_name=row.business_name,
                business_type=row.business_type,
                is_active=row.is_active,
                suspension_reason=row.suspension_reason,
                suspended_at=row.suspended_at,
                created_at=row.created_at,
                users_count=row.users_count,
                conversations_count=row.conversations_count,
                tokens_used=row.tokens_used,
            )
            for row in result.fetchall()
        ]

        return ClientListResponse(
            clients=clients,
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get_client_detail(
        self, client_id: uuid.UUID
    ) -> ClientDetailResponse:
        """Obtiene los datos detallados de un cliente con estadísticas de uso.

        Args:
            client_id: UUID del cliente.

        Returns:
            ClientDetailResponse con datos completos y métricas.

        Raises:
            AppException: 404 si el cliente no existe.
        """
        result = await self.session.execute(
            text("""
                SELECT
                    c.id, c.business_name, c.business_type,
                    c.is_active, c.suspension_reason, c.alert_message,
                    c.suspended_at, c.suspension_date, c.payment_alert_config,
                    c.settings, c.onboarding_completed_at,
                    c.created_at, c.updated_at
                FROM clients c
                WHERE c.id = :client_id
            """),
            {"client_id": client_id},
        )
        row = result.fetchone()

        if row is None:
            raise AppException(
                status_code=404,
                detail="Cliente no encontrado.",
                error_code="CLIENT_NOT_FOUND",
            )

        usage_stats = await self._get_usage_stats(client_id)

        payment_config = row.payment_alert_config
        if isinstance(payment_config, str):
            payment_config = json.loads(payment_config)

        settings_data = row.settings
        if isinstance(settings_data, str):
            settings_data = json.loads(settings_data)

        return ClientDetailResponse(
            id=row.id,
            business_name=row.business_name,
            business_type=row.business_type,
            is_active=row.is_active,
            suspension_reason=row.suspension_reason,
            alert_message=row.alert_message,
            suspended_at=row.suspended_at,
            suspension_date=row.suspension_date,
            payment_alert_config=payment_config,
            settings=settings_data,
            onboarding_completed_at=row.onboarding_completed_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
            usage_stats=usage_stats,
        )

    async def update_client_status(
        self,
        client_id: uuid.UUID,
        payload: ClientStatusRequest,
    ) -> ClientStatusResponse:
        """Activa o desactiva un cliente.

        Al desactivar:
            1. ``clients.is_active`` = False.
            2. Todas las conversaciones se archivan.
            3. Se configura el ``alert_message``.
            4. Se notifica a los administradores.

        Al reactivar:
            1. ``clients.is_active`` = True.
            2. Se limpian los campos de suspensión.

        Args:
            client_id: UUID del cliente.
            payload: Datos del cambio de estado.

        Returns:
            ClientStatusResponse con el nuevo estado.

        Raises:
            AppException: 404 si el cliente no existe.
        """
        existing = await self.session.execute(
            text("""
                SELECT id, business_name, is_active
                FROM clients WHERE id = :client_id
            """),
            {"client_id": client_id},
        )
        client_row = existing.fetchone()

        if client_row is None:
            raise AppException(
                status_code=404,
                detail="Cliente no encontrado.",
                error_code="CLIENT_NOT_FOUND",
            )

        if payload.is_active:
            return await self._reactivate_client(client_id, client_row.business_name)
        else:
            return await self._deactivate_client(
                client_id, client_row.business_name, payload
            )

    async def _deactivate_client(
        self,
        client_id: uuid.UUID,
        business_name: str,
        payload: ClientStatusRequest,
    ) -> ClientStatusResponse:
        """Ejecuta la desactivación completa de un cliente.

        Args:
            client_id: UUID del cliente.
            business_name: Nombre del negocio.
            payload: Datos de la suspensión.

        Returns:
            ClientStatusResponse con datos de suspensión.
        """
        now = datetime.now(timezone.utc)

        # 1. Actualizar estado del cliente
        await self.session.execute(
            text("""
                UPDATE clients
                SET is_active = FALSE,
                    suspension_reason = :reason,
                    alert_message = :alert_msg,
                    suspended_at = :suspended_at,
                    updated_at = NOW()
                WHERE id = :client_id
            """),
            {
                "client_id": client_id,
                "reason": payload.suspension_reason.value if payload.suspension_reason else None,
                "alert_msg": payload.alert_message,
                "suspended_at": now,
            },
        )

        # 2. Archivar todas las conversaciones activas
        await self.session.execute(
            text("""
                UPDATE conversations
                SET status = 'archived', updated_at = NOW()
                WHERE client_id = :client_id AND status != 'archived'
            """),
            {"client_id": client_id},
        )

        await self.session.commit()

        # 3. Notificar a los administradores del tenant
        admin_result = await self.session.execute(
            text("""
                SELECT email FROM users
                WHERE client_id = :client_id AND role IN ('admin', 'super_admin')
                  AND is_active = TRUE
            """),
            {"client_id": client_id},
        )
        admin_emails = [row.email for row in admin_result.fetchall()]

        if admin_emails:
            send_suspension_notification.delay(
                admin_emails=admin_emails,
                business_name=business_name,
                suspension_reason=(
                    payload.suspension_reason.value
                    if payload.suspension_reason
                    else "manual"
                ),
                alert_message=payload.alert_message or "",
            )

        return ClientStatusResponse(
            client_id=client_id,
            is_active=False,
            suspension_reason=(
                payload.suspension_reason.value
                if payload.suspension_reason
                else None
            ),
            suspended_at=now,
            message=f"Cliente '{business_name}' desactivado exitosamente.",
        )

    async def _reactivate_client(
        self,
        client_id: uuid.UUID,
        business_name: str,
    ) -> ClientStatusResponse:
        """Ejecuta la reactivación de un cliente suspendido.

        Args:
            client_id: UUID del cliente.
            business_name: Nombre del negocio.

        Returns:
            ClientStatusResponse con estado activo.
        """
        await self.session.execute(
            text("""
                UPDATE clients
                SET is_active = TRUE,
                    suspension_reason = NULL,
                    alert_message = NULL,
                    suspended_at = NULL,
                    suspension_date = NULL,
                    updated_at = NOW()
                WHERE id = :client_id
            """),
            {"client_id": client_id},
        )
        await self.session.commit()

        return ClientStatusResponse(
            client_id=client_id,
            is_active=True,
            message=f"Cliente '{business_name}' reactivado exitosamente.",
        )

    async def update_payment_alerts(
        self,
        client_id: uuid.UUID,
        config: PaymentAlertConfig,
    ) -> None:
        """Actualiza la configuración de alertas de pago de un cliente.

        Args:
            client_id: UUID del cliente.
            config: Configuración de alertas.

        Raises:
            AppException: 404 si el cliente no existe.
        """
        existing = await self.session.execute(
            text("SELECT id FROM clients WHERE id = :client_id"),
            {"client_id": client_id},
        )
        if existing.scalar_one_or_none() is None:
            raise AppException(
                status_code=404,
                detail="Cliente no encontrado.",
                error_code="CLIENT_NOT_FOUND",
            )

        config_json = json.dumps(
            {
                "alert_days_before_suspension": config.alert_days_before_suspension,
                "alert_message_template": config.alert_message_template,
            },
            ensure_ascii=False,
        )

        await self.session.execute(
            text("""
                UPDATE clients
                SET payment_alert_config = :config::jsonb,
                    suspension_date = :suspension_date,
                    updated_at = NOW()
                WHERE id = :client_id
            """),
            {
                "client_id": client_id,
                "config": config_json,
                "suspension_date": config.suspension_date,
            },
        )
        await self.session.commit()

    async def _get_usage_stats(self, client_id: uuid.UUID) -> dict[str, Any]:
        """Obtiene estadísticas de uso de un cliente.

        Args:
            client_id: UUID del cliente.

        Returns:
            Diccionario con métricas de uso.
        """
        result = await self.session.execute(
            text("""
                SELECT
                    (SELECT COUNT(*) FROM users WHERE client_id = :cid) AS total_users,
                    (SELECT COUNT(*) FROM users WHERE client_id = :cid AND is_active = TRUE) AS active_users,
                    (SELECT COUNT(*) FROM conversations WHERE client_id = :cid) AS total_conversations,
                    (SELECT COUNT(*) FROM conversations WHERE client_id = :cid AND status = 'active') AS active_conversations,
                    (SELECT COUNT(*) FROM messages WHERE client_id = :cid) AS total_messages,
                    (SELECT COUNT(*) FROM documents WHERE client_id = :cid) AS total_documents,
                    (SELECT COALESCE(SUM(tokens_used), 0) FROM token_budgets WHERE client_id = :cid) AS tokens_used,
                    (SELECT COALESCE(MAX(monthly_limit), 0) FROM token_budgets WHERE client_id = :cid) AS token_limit
            """),
            {"cid": client_id},
        )
        row = result.fetchone()

        return {
            "total_users": row.total_users,
            "active_users": row.active_users,
            "total_conversations": row.total_conversations,
            "active_conversations": row.active_conversations,
            "total_messages": row.total_messages,
            "total_documents": row.total_documents,
            "tokens_used": row.tokens_used,
            "token_limit": row.token_limit,
            "token_usage_percentage": (
                round((row.tokens_used / row.token_limit) * 100, 2)
                if row.token_limit > 0
                else 0.0
            ),
        }
```

### 4.5 Tarea Celery Beat: Verificación de Alertas de Pago

```python
# app/tasks/payment_tasks.py
"""Tareas Celery para gestión de alertas de pago y suspensión automática."""

from datetime import datetime, timezone

from celery import shared_task
from sqlalchemy import text

from app.core.database import get_sync_session_factory
from app.tasks.email_tasks import send_payment_alert


@shared_task(
    name="tasks.check_payment_alerts",
    bind=True,
    max_retries=1,
    acks_late=True,
)
def check_payment_alerts(self) -> dict[str, int]:
    """Tarea periódica (diaria) para verificar alertas de pago.

    Recorre todos los clientes activos con ``suspension_date`` configurada,
    calcula los días restantes, y:
    - Envía alertas en los días configurados en ``payment_alert_config``.
    - Auto-suspende clientes cuya ``suspension_date`` ha llegado.

    Returns:
        Diccionario con contadores de acciones realizadas.
    """
    session_factory = get_sync_session_factory()
    alerts_sent = 0
    clients_suspended = 0

    with session_factory() as session:
        result = session.execute(
            text("""
                SELECT
                    c.id, c.business_name, c.suspension_date,
                    c.payment_alert_config
                FROM clients c
                WHERE c.is_active = TRUE
                  AND c.suspension_date IS NOT NULL
                ORDER BY c.suspension_date ASC
            """)
        )
        clients = result.fetchall()

        now = datetime.now(timezone.utc)

        for client in clients:
            days_remaining = (client.suspension_date - now).days

            # Auto-suspensión si la fecha ya pasó
            if days_remaining <= 0:
                _auto_suspend_client(session, client.id, client.business_name)
                clients_suspended += 1
                continue

            # Verificar si corresponde enviar alerta
            config = client.payment_alert_config or {}
            alert_days = config.get("alert_days_before_suspension", [7, 3, 1])
            template = config.get(
                "alert_message_template",
                (
                    "Estimado {business_name}, su servicio será suspendido "
                    "en {days_remaining} día(s) por falta de pago."
                ),
            )

            if days_remaining in alert_days:
                admin_emails = _get_admin_emails(session, client.id)
                if admin_emails:
                    send_payment_alert.delay(
                        admin_emails=admin_emails,
                        business_name=client.business_name,
                        days_remaining=days_remaining,
                        alert_message_template=template,
                    )
                    alerts_sent += 1

        session.commit()

    return {
        "alerts_sent": alerts_sent,
        "clients_suspended": clients_suspended,
    }


def _auto_suspend_client(session: Any, client_id: Any, business_name: str) -> None:
    """Suspende automáticamente un cliente por vencimiento de pago.

    Args:
        session: Sesión síncrona de SQLAlchemy.
        client_id: UUID del cliente.
        business_name: Nombre del negocio.
    """
    now = datetime.now(timezone.utc)

    session.execute(
        text("""
            UPDATE clients
            SET is_active = FALSE,
                suspension_reason = 'payment_overdue',
                alert_message = :alert_msg,
                suspended_at = :now,
                updated_at = :now
            WHERE id = :client_id
        """),
        {
            "client_id": client_id,
            "alert_msg": (
                f"El servicio de {business_name} ha sido suspendido "
                "por falta de pago. Comuníquese con soporte."
            ),
            "now": now,
        },
    )

    session.execute(
        text("""
            UPDATE conversations
            SET status = 'archived', updated_at = :now
            WHERE client_id = :client_id AND status != 'archived'
        """),
        {"client_id": client_id, "now": now},
    )

    admin_emails = _get_admin_emails(session, client_id)
    if admin_emails:
        from app.tasks.email_tasks import send_suspension_notification

        send_suspension_notification.delay(
            admin_emails=admin_emails,
            business_name=business_name,
            suspension_reason="payment_overdue",
            alert_message=(
                f"El servicio de {business_name} ha sido suspendido "
                "automáticamente por falta de pago."
            ),
        )


def _get_admin_emails(session: Any, client_id: Any) -> list[str]:
    """Obtiene los emails de administradores de un cliente.

    Args:
        session: Sesión de SQLAlchemy.
        client_id: UUID del cliente.

    Returns:
        Lista de emails de administradores activos.
    """
    result = session.execute(
        text("""
            SELECT email FROM users
            WHERE client_id = :client_id
              AND role IN ('admin', 'super_admin')
              AND is_active = TRUE
        """),
        {"client_id": client_id},
    )
    return [row.email for row in result.fetchall()]
```

### 4.6 Configuración Celery Beat

```python
# En app/core/celery_config.py — agregar al beat_schedule:

from celery.schedules import crontab

beat_schedule = {
    # ... tareas existentes ...
    "check-payment-alerts": {
        "task": "tasks.check_payment_alerts",
        "schedule": crontab(hour=6, minute=0),  # Ejecutar diario a las 6:00 UTC
        "options": {"queue": "default"},
    },
}
```

### 4.7 Middleware de Verificación de Cliente Activo

```python
# app/middleware/client_active_check.py
"""Middleware para verificar que el cliente (tenant) esté activo."""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException


async def verify_client_is_active(
    client_id: Any,
    session: AsyncSession,
) -> None:
    """Verifica que el cliente esté activo antes de procesar una solicitud.

    Si el cliente está inactivo, lanza una excepción con el mensaje
    de alerta configurado.

    Args:
        client_id: UUID del tenant.
        session: Sesión async de SQLAlchemy.

    Raises:
        AppException: 403 si el cliente está desactivado.
    """
    result = await session.execute(
        text("""
            SELECT is_active, alert_message
            FROM clients
            WHERE id = :client_id
        """),
        {"client_id": client_id},
    )
    row = result.fetchone()

    if row is None:
        raise AppException(
            status_code=404,
            detail="Cliente no encontrado.",
            error_code="CLIENT_NOT_FOUND",
        )

    if not row.is_active:
        alert_msg = row.alert_message or (
            "Su servicio ha sido suspendido. "
            "Comuníquese con soporte para más información."
        )
        raise AppException(
            status_code=403,
            detail=alert_msg,
            error_code="CLIENT_SUSPENDED",
        )
```

### 4.8 Registro del Router

```python
# En app/api/v1/router.py — agregar:

from app.api.v1.endpoints.client_management import (
    router as client_management_router,
)

# Rutas de super admin
api_router.include_router(client_management_router)
```

---

## 5. Migraciones SQL

### 5.1 Migración: Campos de Onboarding y Verificación de Email

```sql
-- migrations/versions/003_01_add_onboarding_fields.sql
-- Sprint 3 Addendum: Onboarding y verificación de email
-- Fecha: 2026-09-05

BEGIN;

-- Agregar campo de verificación de email a users
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS verification_token TEXT;

-- Índice para búsqueda rápida por token de verificación
CREATE INDEX IF NOT EXISTS idx_users_verification_token
    ON users (verification_token)
    WHERE verification_token IS NOT NULL;

-- Agregar timestamp de completitud de onboarding a clients
ALTER TABLE clients
    ADD COLUMN IF NOT EXISTS onboarding_completed_at TIMESTAMPTZ;

COMMIT;
```

### 5.2 Migración: Campos de Gestión y Suspensión de Clientes

```sql
-- migrations/versions/003_02_add_client_management_fields.sql
-- Sprint 3 Addendum: Gestión de clientes, suspensión y alertas de pago
-- Fecha: 2026-09-05

BEGIN;

-- Campos de suspensión
ALTER TABLE clients
    ADD COLUMN IF NOT EXISTS suspension_reason TEXT;

ALTER TABLE clients
    ADD COLUMN IF NOT EXISTS alert_message TEXT;

ALTER TABLE clients
    ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ;

-- Campos de alertas de pago
ALTER TABLE clients
    ADD COLUMN IF NOT EXISTS suspension_date TIMESTAMPTZ;

ALTER TABLE clients
    ADD COLUMN IF NOT EXISTS payment_alert_config JSONB DEFAULT '{}'::jsonb;

-- Índice para la tarea periódica de alertas de pago
CREATE INDEX IF NOT EXISTS idx_clients_suspension_date
    ON clients (suspension_date)
    WHERE is_active = TRUE AND suspension_date IS NOT NULL;

-- Índice parcial para clientes inactivos
CREATE INDEX IF NOT EXISTS idx_clients_inactive
    ON clients (id)
    WHERE is_active = FALSE;

-- Asegurar que business_type existe en clients (si no fue creado antes)
ALTER TABLE clients
    ADD COLUMN IF NOT EXISTS business_type TEXT;

COMMIT;
```

### 5.3 Migración: Tabla de Presupuesto de Tokens

```sql
-- migrations/versions/003_03_add_token_budgets.sql
-- Sprint 3 Addendum: Presupuesto de tokens (si no existe del DDL base)
-- Fecha: 2026-09-05

BEGIN;

CREATE TABLE IF NOT EXISTS token_budgets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    monthly_limit INTEGER NOT NULL DEFAULT 50000,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    period_start TIMESTAMPTZ NOT NULL DEFAULT DATE_TRUNC('month', NOW()),
    period_end TIMESTAMPTZ NOT NULL DEFAULT DATE_TRUNC('month', NOW()) + INTERVAL '1 month',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- RLS
ALTER TABLE token_budgets ENABLE ROW LEVEL SECURITY;

CREATE POLICY token_budgets_isolation ON token_budgets
    USING (client_id = current_setting('app.current_client_id')::uuid);

-- Índice para búsqueda por tenant y período
CREATE INDEX IF NOT EXISTS idx_token_budgets_client_period
    ON token_budgets (client_id, period_start, period_end);

COMMIT;
```

### 5.4 Aplicación con pgBouncer (Patrón Transaccional)

> **Importante:** Todas las consultas que requieran RLS deben usar `SET LOCAL` (nunca `SET`) porque pgBouncer en modo transaccional comparte conexiones. `SET LOCAL` aplica el valor solo dentro de la transacción actual, garantizando aislamiento entre tenants.

```python
# Patrón para todas las queries con RLS:
await session.execute(
    text("SET LOCAL app.current_client_id = :client_id"),
    {"client_id": str(client_id)},
)
# ... queries que se benefician de RLS ...
```

---

## 6. Tests

### 6.1 Tests Unitarios — Servicio de Onboarding

```python
# tests/unit/services/test_onboarding_service.py
"""Tests unitarios para el servicio de onboarding."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.schemas.onboarding import BusinessType, OnboardingRequest
from app.services.onboarding_service import OnboardingService


@pytest.fixture
def mock_session() -> AsyncMock:
    """Fixture que provee una sesión mock de SQLAlchemy."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.begin = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock()
    session.begin.return_value.__aexit__ = AsyncMock()
    return session


@pytest.fixture
def valid_onboarding_request() -> OnboardingRequest:
    """Fixture con un request de onboarding válido."""
    return OnboardingRequest(
        business_name="Test Restaurant SAS",
        business_type=BusinessType.RESTAURANT,
        admin_email="admin@testrestaurant.com",
        admin_password="SecurePass123",
        admin_full_name="Juan Pérez",
        phone="+573001234567",
        country="CO",
        language="es",
        terms_accepted=True,
    )


class TestOnboardingService:
    """Tests para OnboardingService."""

    @pytest.mark.asyncio
    async def test_register_new_client_success(
        self,
        mock_session: AsyncMock,
        valid_onboarding_request: OnboardingRequest,
    ) -> None:
        """Verifica que el registro exitoso retorna credenciales válidas."""
        # Arrange: email no existe
        result_mock = AsyncMock()
        result_mock.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = result_mock

        service = OnboardingService(mock_session)

        with patch(
            "app.services.onboarding_service.send_verification_email"
        ) as mock_email:
            mock_email.delay = MagicMock()

            with patch(
                "app.services.onboarding_service.create_access_token",
                return_value="fake_access_token",
            ), patch(
                "app.services.onboarding_service.create_refresh_token",
                return_value="fake_refresh_token",
            ):
                # Act
                response = await service.register_new_client(
                    valid_onboarding_request
                )

        # Assert
        assert response.access_token == "fake_access_token"
        assert response.refresh_token == "fake_refresh_token"
        assert response.client_id is not None
        assert response.user_id is not None
        mock_email.delay.assert_called_once()

    @pytest.mark.asyncio
    async def test_register_duplicate_email_raises_409(
        self,
        mock_session: AsyncMock,
        valid_onboarding_request: OnboardingRequest,
    ) -> None:
        """Verifica que un email duplicado lanza AppException 409."""
        # Arrange: email ya existe
        result_mock = AsyncMock()
        result_mock.scalar_one_or_none.return_value = uuid4()
        mock_session.execute.return_value = result_mock

        service = OnboardingService(mock_session)

        # Act & Assert
        from app.core.exceptions import AppException

        with pytest.raises(AppException) as exc_info:
            await service.register_new_client(valid_onboarding_request)

        assert exc_info.value.status_code == 409
        assert exc_info.value.error_code == "EMAIL_ALREADY_EXISTS"

    @pytest.mark.asyncio
    async def test_build_default_settings_structure(
        self,
        mock_session: AsyncMock,
        valid_onboarding_request: OnboardingRequest,
    ) -> None:
        """Verifica la estructura del JSONB de settings por defecto."""
        service = OnboardingService(mock_session)
        settings = service._build_default_settings(valid_onboarding_request)

        assert "business_profile" in settings
        assert "branding" in settings
        assert "notifications" in settings

        profile = settings["business_profile"]
        assert profile["business_name"] == "Test Restaurant SAS"
        assert profile["business_type"] == "restaurant"
        assert profile["country"] == "CO"
        assert profile["language"] == "es"
        assert "welcome_message" in profile
        assert "farewell_message" in profile
        assert "out_of_hours_message" in profile

        branding = settings["branding"]
        assert branding["primary_color"] == "#2563EB"
        assert branding["logo_url"] is None


class TestOnboardingRequestValidation:
    """Tests para validación del esquema OnboardingRequest."""

    def test_valid_request(self) -> None:
        """Verifica que un request válido pasa las validaciones."""
        request = OnboardingRequest(
            business_name="Mi Negocio",
            business_type=BusinessType.SERVICES,
            admin_email="test@example.com",
            admin_password="ValidPass1",
            admin_full_name="Test User",
            terms_accepted=True,
        )
        assert request.country == "CO"
        assert request.language == "es"

    def test_password_requires_uppercase(self) -> None:
        """Verifica que la contraseña requiera al menos una mayúscula."""
        with pytest.raises(ValueError, match="mayúscula"):
            OnboardingRequest(
                business_name="Test",
                business_type=BusinessType.OTHER,
                admin_email="test@example.com",
                admin_password="nouppercase1",
                admin_full_name="Test",
                terms_accepted=True,
            )

    def test_password_requires_lowercase(self) -> None:
        """Verifica que la contraseña requiera al menos una minúscula."""
        with pytest.raises(ValueError, match="minúscula"):
            OnboardingRequest(
                business_name="Test",
                business_type=BusinessType.OTHER,
                admin_email="test@example.com",
                admin_password="NOLOWERCASE1",
                admin_full_name="Test",
                terms_accepted=True,
            )

    def test_password_requires_number(self) -> None:
        """Verifica que la contraseña requiera al menos un número."""
        with pytest.raises(ValueError, match="número"):
            OnboardingRequest(
                business_name="Test",
                business_type=BusinessType.OTHER,
                admin_email="test@example.com",
                admin_password="NoNumberHere",
                admin_full_name="Test",
                terms_accepted=True,
            )

    def test_terms_must_be_accepted(self) -> None:
        """Verifica que terms_accepted debe ser True."""
        with pytest.raises(ValueError, match="términos"):
            OnboardingRequest(
                business_name="Test",
                business_type=BusinessType.OTHER,
                admin_email="test@example.com",
                admin_password="ValidPass1",
                admin_full_name="Test",
                terms_accepted=False,
            )

    def test_country_defaults_to_co(self) -> None:
        """Verifica que el país por defecto sea CO."""
        request = OnboardingRequest(
            business_name="Test",
            business_type=BusinessType.OTHER,
            admin_email="test@example.com",
            admin_password="ValidPass1",
            admin_full_name="Test",
            terms_accepted=True,
        )
        assert request.country == "CO"

    def test_language_defaults_to_es(self) -> None:
        """Verifica que el idioma por defecto sea es."""
        request = OnboardingRequest(
            business_name="Test",
            business_type=BusinessType.OTHER,
            admin_email="test@example.com",
            admin_password="ValidPass1",
            admin_full_name="Test",
            terms_accepted=True,
        )
        assert request.language == "es"
```

### 6.2 Tests Unitarios — Servicio de Gestión de Clientes

```python
# tests/unit/services/test_client_management_service.py
"""Tests unitarios para el servicio de gestión de clientes."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.schemas.client_management import (
    ClientStatusRequest,
    PaymentAlertConfig,
    SuspensionReason,
)
from app.services.client_management_service import ClientManagementService


@pytest.fixture
def mock_session() -> AsyncMock:
    """Fixture que provee una sesión mock de SQLAlchemy."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    return session


class TestClientManagementService:
    """Tests para ClientManagementService."""

    @pytest.mark.asyncio
    async def test_deactivate_client_success(
        self, mock_session: AsyncMock
    ) -> None:
        """Verifica la desactivación exitosa de un cliente."""
        client_id = uuid4()
        # Mock: cliente existe
        client_row = MagicMock()
        client_row.id = client_id
        client_row.business_name = "Test Business"
        client_row.is_active = True

        result_mock = AsyncMock()
        result_mock.fetchone.return_value = client_row

        admin_result = AsyncMock()
        admin_email_row = MagicMock()
        admin_email_row.email = "admin@test.com"
        admin_result.fetchall.return_value = [admin_email_row]

        mock_session.execute.side_effect = [
            result_mock,      # SELECT cliente
            AsyncMock(),       # UPDATE clients
            AsyncMock(),       # UPDATE conversations
            admin_result,      # SELECT admin emails
        ]

        payload = ClientStatusRequest(
            is_active=False,
            suspension_reason=SuspensionReason.PAYMENT_OVERDUE,
            alert_message="Servicio suspendido por falta de pago.",
        )

        service = ClientManagementService(mock_session)

        with patch(
            "app.services.client_management_service.send_suspension_notification"
        ) as mock_notify:
            mock_notify.delay = MagicMock()
            response = await service.update_client_status(client_id, payload)

        assert response.is_active is False
        assert response.suspension_reason == "payment_overdue"
        assert response.suspended_at is not None
        mock_notify.delay.assert_called_once()

    @pytest.mark.asyncio
    async def test_reactivate_client_success(
        self, mock_session: AsyncMock
    ) -> None:
        """Verifica la reactivación exitosa de un cliente."""
        client_id = uuid4()
        client_row = MagicMock()
        client_row.id = client_id
        client_row.business_name = "Test Business"
        client_row.is_active = False

        result_mock = AsyncMock()
        result_mock.fetchone.return_value = client_row

        mock_session.execute.side_effect = [
            result_mock,  # SELECT cliente
            AsyncMock(),   # UPDATE clients
        ]

        payload = ClientStatusRequest(is_active=True)
        service = ClientManagementService(mock_session)
        response = await service.update_client_status(client_id, payload)

        assert response.is_active is True
        assert response.suspension_reason is None

    @pytest.mark.asyncio
    async def test_update_client_status_not_found(
        self, mock_session: AsyncMock
    ) -> None:
        """Verifica que un cliente inexistente lanza 404."""
        client_id = uuid4()
        result_mock = AsyncMock()
        result_mock.fetchone.return_value = None
        mock_session.execute.return_value = result_mock

        payload = ClientStatusRequest(is_active=False, suspension_reason=SuspensionReason.MANUAL)
        service = ClientManagementService(mock_session)

        from app.core.exceptions import AppException

        with pytest.raises(AppException) as exc_info:
            await service.update_client_status(client_id, payload)

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_update_payment_alerts_success(
        self, mock_session: AsyncMock
    ) -> None:
        """Verifica la actualización de configuración de alertas de pago."""
        client_id = uuid4()
        result_mock = AsyncMock()
        result_mock.scalar_one_or_none.return_value = client_id
        mock_session.execute.side_effect = [result_mock, AsyncMock()]

        config = PaymentAlertConfig(
            alert_days_before_suspension=[7, 3, 1],
            alert_message_template=(
                "Estimado {business_name}, quedan {days_remaining} días."
            ),
            suspension_date=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )

        service = ClientManagementService(mock_session)
        await service.update_payment_alerts(client_id, config)

        mock_session.commit.assert_called_once()


class TestPaymentAlertConfigValidation:
    """Tests para validación de PaymentAlertConfig."""

    def test_alert_days_sorted_descending(self) -> None:
        """Verifica que los días de alerta se ordenen de mayor a menor."""
        config = PaymentAlertConfig(
            alert_days_before_suspension=[1, 7, 3]
        )
        assert config.alert_days_before_suspension == [7, 3, 1]

    def test_alert_days_must_be_positive(self) -> None:
        """Verifica que los días de alerta sean positivos."""
        with pytest.raises(ValueError, match="positivos"):
            PaymentAlertConfig(
                alert_days_before_suspension=[0, -1, 3]
            )

    def test_default_template_has_variables(self) -> None:
        """Verifica que la plantilla por defecto contenga las variables."""
        config = PaymentAlertConfig()
        assert "{business_name}" in config.alert_message_template
        assert "{days_remaining}" in config.alert_message_template


class TestClientStatusRequestValidation:
    """Tests para validación de ClientStatusRequest."""

    def test_deactivation_requires_reason(self) -> None:
        """Verifica que desactivar requiera suspension_reason."""
        with pytest.raises(ValueError, match="suspension_reason"):
            ClientStatusRequest(is_active=False)

    def test_activation_does_not_require_reason(self) -> None:
        """Verifica que activar no requiera suspension_reason."""
        request = ClientStatusRequest(is_active=True)
        assert request.suspension_reason is None
```

### 6.3 Test de Integración — Flujo de Onboarding

```python
# tests/integration/test_onboarding_flow.py
"""Tests de integración para el flujo completo de onboarding."""

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
def anyio_backend() -> str:
    """Backend de anyio para tests async."""
    return "asyncio"


@pytest.fixture
async def client() -> AsyncClient:
    """Fixture de cliente HTTP async para tests de integración."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestOnboardingFlow:
    """Tests de integración para el flujo de onboarding completo."""

    @pytest.mark.asyncio
    async def test_full_onboarding_flow(self, client: AsyncClient) -> None:
        """Verifica el flujo completo: registro -> verificación de email.

        Pasos:
        1. POST /api/v1/onboarding/register con datos válidos.
        2. Verificar respuesta 201 con credenciales.
        3. Verificar que el email de verificación fue encolado.
        4. GET /api/v1/onboarding/verify-email con token.
        5. Verificar que el usuario quede verificado.
        """
        with patch(
            "app.services.onboarding_service.send_verification_email"
        ) as mock_email:
            mock_email.delay.return_value = None

            # Paso 1: Registro
            payload = {
                "business_name": "Integration Test SAS",
                "business_type": "restaurant",
                "admin_email": "integration@test.com",
                "admin_password": "IntegrationTest1",
                "admin_full_name": "Integration Tester",
                "phone": "+573009999999",
                "country": "CO",
                "language": "es",
                "terms_accepted": True,
            }

            response = await client.post(
                "/api/v1/onboarding/register",
                json=payload,
            )

            # Paso 2: Verificar respuesta
            assert response.status_code == 201
            data = response.json()
            assert "client_id" in data
            assert "user_id" in data
            assert "access_token" in data
            assert "refresh_token" in data

            # Paso 3: Verificar que se encoló el email
            mock_email.delay.assert_called_once()
            call_kwargs = mock_email.delay.call_args
            assert call_kwargs.kwargs["email"] == "integration@test.com"

    @pytest.mark.asyncio
    async def test_duplicate_email_returns_409(
        self, client: AsyncClient
    ) -> None:
        """Verifica que registrarse con email duplicado retorna 409."""
        with patch(
            "app.services.onboarding_service.send_verification_email"
        ) as mock_email:
            mock_email.delay.return_value = None

            payload = {
                "business_name": "First Business",
                "business_type": "services",
                "admin_email": "duplicate@test.com",
                "admin_password": "ValidPass1",
                "admin_full_name": "First User",
                "terms_accepted": True,
            }

            # Primer registro
            response1 = await client.post(
                "/api/v1/onboarding/register",
                json=payload,
            )
            assert response1.status_code == 201

            # Segundo registro con mismo email
            payload["business_name"] = "Second Business"
            response2 = await client.post(
                "/api/v1/onboarding/register",
                json=payload,
            )
            assert response2.status_code == 409
            assert response2.json()["error_code"] == "EMAIL_ALREADY_EXISTS"

    @pytest.mark.asyncio
    async def test_invalid_password_returns_422(
        self, client: AsyncClient
    ) -> None:
        """Verifica que una contraseña inválida retorna 422."""
        payload = {
            "business_name": "Test",
            "business_type": "other",
            "admin_email": "weak@test.com",
            "admin_password": "weak",
            "admin_full_name": "Test",
            "terms_accepted": True,
        }

        response = await client.post(
            "/api/v1/onboarding/register",
            json=payload,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_terms_not_accepted_returns_422(
        self, client: AsyncClient
    ) -> None:
        """Verifica que no aceptar términos retorna 422."""
        payload = {
            "business_name": "Test",
            "business_type": "other",
            "admin_email": "noterms@test.com",
            "admin_password": "ValidPass1",
            "admin_full_name": "Test",
            "terms_accepted": False,
        }

        response = await client.post(
            "/api/v1/onboarding/register",
            json=payload,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_rate_limit_after_5_requests(
        self, client: AsyncClient
    ) -> None:
        """Verifica que el rate limit bloquee después de 5 solicitudes."""
        with patch(
            "app.services.onboarding_service.send_verification_email"
        ) as mock_email, patch(
            "app.core.rate_limit.get_redis"
        ) as mock_redis:
            mock_email.delay.return_value = None

            # Simular que Redis retorna count > 5
            redis_mock = mock_redis.return_value
            redis_mock.incr.return_value = 6
            redis_mock.ttl.return_value = 3200

            payload = {
                "business_name": "Rate Limited",
                "business_type": "other",
                "admin_email": "ratelimit@test.com",
                "admin_password": "ValidPass1",
                "admin_full_name": "Test",
                "terms_accepted": True,
            }

            response = await client.post(
                "/api/v1/onboarding/register",
                json=payload,
            )
            assert response.status_code == 429

    @pytest.mark.asyncio
    async def test_missing_required_fields_returns_422(
        self, client: AsyncClient
    ) -> None:
        """Verifica que campos faltantes retornan 422."""
        payload = {
            "business_name": "Test",
            # Falta business_type, admin_email, etc.
        }

        response = await client.post(
            "/api/v1/onboarding/register",
            json=payload,
        )
        assert response.status_code == 422
```

---

## 7. Criterios de Aceptación

| # | Feature | Criterio | Método de Verificación |
|---|---------|----------|------------------------|
| 1 | Onboarding | `POST /api/v1/onboarding/register` crea client + user + agent_config + token_budget en una sola transacción | Test de integración: verificar registros en BD |
| 2 | Onboarding | El endpoint es público (no requiere autenticación) | Test: llamar sin token Bearer y recibir 201 |
| 3 | Onboarding | Rate limit de 5 req/IP/hora bloquea la 6ta solicitud con 429 | Test de integración con Redis mock |
| 4 | Onboarding | Email duplicado retorna 409 con error_code `EMAIL_ALREADY_EXISTS` | Test unitario + integración |
| 5 | Onboarding | Contraseña sin mayúscula, minúscula o número retorna 422 | Tests de validación Pydantic |
| 6 | Onboarding | `terms_accepted=false` retorna 422 | Test de validación Pydantic |
| 7 | Onboarding | Se dispara tarea Celery `send_verification_email` con token único | Test: mock de Celery, verificar `.delay()` |
| 8 | Onboarding | `GET /api/v1/onboarding/verify-email?token=...` marca `email_verified=True` | Test de integración |
| 9 | Onboarding | La respuesta incluye `access_token` y `refresh_token` válidos | Test: decodificar JWT y verificar claims |
| 10 | Perfil | `GET /api/v1/settings/business-profile` retorna el perfil del tenant autenticado | Test con usuario autenticado |
| 11 | Perfil | `PUT /api/v1/settings/business-profile` actualiza parcialmente `clients.settings` JSONB | Test: enviar solo 2 campos, verificar merge |
| 12 | Perfil | `POST /api/v1/settings/business-profile/logo` acepta PNG, JPEG, WebP, SVG hasta 5 MB | Test: subir imagen válida, verificar URL |
| 13 | Perfil | Logo inválido (tipo de archivo o tamaño) retorna 400 o 413 | Test: subir archivo .exe o >5 MB |
| 14 | Perfil | Solo roles `admin` y `super_admin` acceden a los endpoints de perfil | Test: llamar con rol `agent` y recibir 403 |
| 15 | Perfil | `welcome_message`, `farewell_message` y `out_of_hours_message` se inyectan al system_prompt | Test de integración del agente |
| 16 | KB | `POST /api/v1/settings/knowledge-base/upload` acepta PDF, DOCX, TXT, CSV hasta 20 MB | Test: subir cada tipo, verificar registro |
| 17 | KB | Se encola tarea Celery `process_document` tras carga exitosa | Test: mock de Celery, verificar `.delay()` |
| 18 | KB | `GET /api/v1/settings/knowledge-base` retorna lista paginada con total | Test con múltiples documentos |
| 19 | KB | `DELETE /api/v1/settings/knowledge-base/{id}` elimina documento y chunks | Test: verificar borrado en ambas tablas |
| 20 | KB | Documento de otro tenant retorna 404 | Test: intentar eliminar doc de otro client_id |
| 21 | Admin | `GET /api/v1/admin/clients` lista clientes con filtro por estado | Test: crear clientes activos/inactivos, filtrar |
| 22 | Admin | `GET /api/v1/admin/clients/{id}` retorna detalle con `usage_stats` | Test: verificar métricas calculadas |
| 23 | Admin | `PATCH .../status` con `is_active=false` archiva conversaciones | Test: verificar status de conversaciones |
| 24 | Admin | Desactivación envía notificación email a administradores del tenant | Test: mock de Celery, verificar emails |
| 25 | Admin | Cliente desactivado responde con `alert_message` a mensajes entrantes | Test de integración del middleware |
| 26 | Admin | `PATCH .../status` con `is_active=true` limpia campos de suspensión | Test: verificar NULL en suspension_reason, suspended_at |
| 27 | Admin | `PUT .../payment-alerts` configura días de alerta y fecha de suspensión | Test: verificar `payment_alert_config` JSONB |
| 28 | Admin | Celery Beat `check_payment_alerts` envía alertas en días configurados | Test unitario con clientes mock |
| 29 | Admin | Celery Beat auto-suspende clientes con `suspension_date` vencida | Test unitario de `_auto_suspend_client` |
| 30 | Admin | Solo `super_admin` accede a endpoints de administración | Test: llamar con rol `admin` y recibir 403 |
| 31 | General | Todas las queries usan `SET LOCAL` para RLS con pgBouncer | Code review / grep en codebase |
| 32 | General | Todas las funciones tienen type hints y Google-style docstrings | Code review / linter |
| 33 | General | Migraciones SQL se aplican sin errores en PostgreSQL 15+ | Test de migración en entorno CI |

---

## 8. Notas de Implementación

### 8.1 Orden de Implementación Sugerido

1. **Migraciones SQL** (003_01, 003_02, 003_03) — Ejecutar primero para tener el esquema listo.
2. **Esquemas Pydantic** — Crear todos los esquemas de validación.
3. **Rate Limiter** — Implementar el decorador de rate limiting con Redis.
4. **Servicio de Onboarding** — Implementar el flujo completo con tests unitarios.
5. **Endpoints de Onboarding** — Registrar rutas públicas.
6. **Tareas Celery** — Email de verificación y notificaciones.
7. **Servicio de Perfil de Negocio** — CRUD sobre `clients.settings`.
8. **Endpoints de Perfil** — Incluir carga de logo.
9. **Servicio de Base de Conocimientos** — Integrar con pipeline Sprint 5.
10. **Servicio de Gestión de Clientes** — Activación/desactivación.
11. **Tarea Periódica de Pagos** — Celery Beat.
12. **Middleware de cliente activo** — Integrar en flujo de mensajes.
13. **Tests de integración** — Flujo end-to-end.

### 8.2 Variables de Entorno Requeridas

```env
# Onboarding
FRONTEND_BASE_URL=https://app.miplatforma.com
PLATFORM_NAME=MiPlataforma
SUPPORT_EMAIL=soporte@miplatforma.com
DEFAULT_MODEL_NAME=gpt-4o-mini

# CAPTCHA (opcional)
CAPTCHA_ENABLED=false
CAPTCHA_SECRET_KEY=

# Supabase Storage
SUPABASE_STORAGE_URL=http://localhost:8000/storage/v1
SUPABASE_SERVICE_KEY=
```

### 8.3 Dependencias Nuevas

```
# No se requieren dependencias nuevas. Todas las librerías ya están
# en el stack del proyecto:
# - FastAPI (endpoints, validación)
# - Pydantic v2 (esquemas)
# - SQLAlchemy 2.0 async (queries)
# - Celery + Redis (tareas asíncronas, rate limiting)
# - httpx (cliente HTTP para Supabase Storage, ya existente)
```

### 8.4 Consideraciones de Seguridad

- **Rate Limiting:** El endpoint de onboarding está limitado a 5 solicitudes por IP por hora para prevenir abuso. Se implementa con Redis y ventana deslizante.
- **CAPTCHA:** Soporte opcional y configurable para validación CAPTCHA (Google reCAPTCHA v3 o hCaptcha). Se activa vía variable de entorno `CAPTCHA_ENABLED`.
- **Verificación de Email:** El usuario puede autenticarse inmediatamente, pero funcionalidades críticas pueden requerir `email_verified=True` (configurable por endpoint).
- **Hashing de Contraseñas:** Se utiliza bcrypt via `passlib` (ya existente en el proyecto).
- **Inyección SQL:** Todas las queries usan parámetros vinculados (`:param`) para prevenir inyección SQL.
- **RLS:** Todas las tablas mantienen la política de aislamiento por `client_id`. Las queries de super_admin omiten el `SET LOCAL` cuando necesitan acceder a todos los tenants.
- **Almacenamiento de Archivos:** Logos y documentos se almacenan en Supabase Storage con paths aislados por `client_id`, garantizando que un tenant no pueda acceder a archivos de otro.

### 8.5 Diagrama de Flujo del Onboarding

```
Cliente (Browser)                    API                         Base de Datos         Celery/Redis
       |                              |                              |                     |
       |-- POST /onboarding/register->|                              |                     |
       |                              |-- Rate limit check --------->|                     |
       |                              |                              |<-- Redis INCR ------|
       |                              |-- Check email unique ------->|                     |
       |                              |                              |<-- SELECT users ----|
       |                              |-- BEGIN TRANSACTION -------->|                     |
       |                              |   INSERT clients ----------->|                     |
       |                              |   INSERT users ------------->|                     |
       |                              |   INSERT agent_configs ----->|                     |
       |                              |   INSERT token_budgets ----->|                     |
       |                              |-- COMMIT ------------------->|                     |
       |                              |-- Generate JWT ------------->|                     |
       |                              |-- Dispatch email task -------|-------------------->|
       |                              |                              |     send_verification_email
       |<-- 201 OnboardingResponse ---|                              |                     |
       |                              |                              |                     |
```

### 8.6 Diagrama de Flujo de Suspensión

```
Super Admin                     API                         Base de Datos        Celery
    |                            |                              |                  |
    |-- PATCH /status is_active=F->|                            |                  |
    |                            |-- UPDATE clients is_active=F->|                |
    |                            |-- UPDATE conversations ------>|                |
    |                            |   SET status='archived'       |                |
    |                            |-- COMMIT -------------------->|                |
    |                            |-- SELECT admin emails ------->|                |
    |                            |-- Dispatch notification ------|---------------->|
    |<-- 200 ClientStatusResponse-|                              |                |
    |                            |                              |                  |
    |                            |                              |                  |
    |     [Mensaje entrante]     |                              |                  |
    |                            |-- verify_client_is_active --->|                |
    |                            |   SELECT is_active, alert_msg |                |
    |<-- 403 alert_message ------|                              |                  |
```
