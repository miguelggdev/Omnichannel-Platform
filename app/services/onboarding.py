"""Alta de un tenant nuevo desde el registro publico (Sprint 15, Fase 2).

Todo el alta ocurre en una sola transaccion con el contexto del tenant recien
generado: `Client`, administrador, agente por defecto y presupuesto del plan free.
Si algo falla (p. ej. email repetido) no queda nada a medias.
"""

import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.exceptions import CONFLICT, AppException
from app.core.security import create_access_token, create_refresh_token, hash_password
from app.models.agent_config import AgentConfig
from app.models.client import Client
from app.models.token_budget import TokenBudget
from app.models.user import User
from app.schemas.onboarding import OnboardingRequest, OnboardingResponse
from app.services.tenant_cloner import _slugify

logger = logging.getLogger(__name__)

_PROMPT_POR_DEFECTO = (
    "Eres el asistente virtual de {negocio}. Responde con amabilidad y precisión, "
    "usando solo la información de la base de conocimiento. Si no sabes algo o el "
    "cliente pide hablar con una persona, ofrece pasarlo con el equipo."
)


def _separar_nombre(nombre_completo: str) -> tuple[str, str]:
    """Divide un nombre completo en nombre y apellido.

    Args:
        nombre_completo: Lo que escribio el usuario.

    Returns:
        `(nombre, apellido)`; el apellido es `-` si solo hay una palabra, porque la
        columna es obligatoria.
    """
    partes = nombre_completo.split(None, 1)
    return partes[0], (partes[1] if len(partes) > 1 else "-")


async def register_new_client(payload: OnboardingRequest) -> OnboardingResponse:
    """Crea el tenant, su administrador, su agente y su presupuesto free.

    Args:
        payload: Datos validados del formulario.

    Returns:
        Los ids y la sesion del administrador.

    Raises:
        AppException: 409 si el email ya esta registrado.
    """
    settings = get_settings()
    client_id = uuid4()
    user_id = uuid4()
    nombre, apellido = _separar_nombre(payload.admin_full_name)
    # bcrypt es CPU-bound: fuera del event loop, como en el login.
    password_hash = await asyncio.to_thread(hash_password, payload.admin_password)

    try:
        async with tenant_session(client_id) as session:
            session.add(
                Client(
                    id=client_id,
                    name=payload.business_name,
                    slug=f"{_slugify(payload.business_name)}-{client_id.hex[:8]}",
                    plan="free",
                    settings={
                        "business_type": payload.business_type.value,
                        "country": payload.country,
                        "onboarding": "self_service",
                    },
                    is_active=True,
                )
            )
            # `users.client_id` referencia a `clients.id`: el cliente va primero.
            await session.flush()
            session.add(
                User(
                    id=user_id,
                    client_id=client_id,
                    email=payload.admin_email.lower(),
                    password_hash=password_hash,
                    first_name=nombre,
                    last_name=apellido,
                    role="admin",
                    settings={"ui_language": payload.language, "email_verified": False},
                )
            )
            session.add(
                AgentConfig(
                    client_id=client_id,
                    name="Asistente",
                    system_prompt=_PROMPT_POR_DEFECTO.format(negocio=payload.business_name),
                    model="gpt-4o-mini",
                    is_active=True,
                )
            )
            session.add(
                TokenBudget(
                    client_id=client_id,
                    month=datetime.now(timezone.utc).strftime("%Y-%m"),
                    total_budget=settings.ONBOARDING_FREE_TOKEN_BUDGET,
                    model_default="gpt-4o-mini",
                )
            )
    except IntegrityError as exc:
        # El slug lleva un fragmento del uuid: la unica colision realista es el email.
        if "email" not in str(exc.orig).lower():
            raise
        raise AppException(
            status_code=409,
            error_code=CONFLICT,
            message="Ya existe una cuenta con ese email",
        ) from exc

    logger.info("Tenant %s creado por onboarding (usuario %s)", client_id, user_id)
    token_data = {
        "user_id": str(user_id),
        "client_id": str(client_id),
        "email": payload.admin_email.lower(),
        "role": "admin",
    }
    return OnboardingResponse(
        client_id=client_id,
        user_id=user_id,
        access_token=create_access_token(token_data),
        refresh_token=create_refresh_token(token_data),
    )


async def mark_email_verified(client_id: UUID, user_id: UUID, email: str) -> bool:
    """Marca como verificado el email de un usuario.

    Args:
        client_id: Tenant del usuario.
        user_id: Usuario.
        email: Email que llevaba el token; debe seguir siendo el actual.

    Returns:
        `True` si quedo verificado; `False` si el usuario no existe o cambio de email.
    """
    async with tenant_session(client_id) as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user is None or user.email.lower() != email.lower():
            return False
        # Se asigna un dict nuevo: SQLAlchemy no detecta cambios dentro de un JSONB mutable.
        user.settings = {**user.settings, "email_verified": True}
    return True
