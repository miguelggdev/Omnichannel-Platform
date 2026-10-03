"""Preferencias del usuario: idioma de la interfaz y tema (Sprint 14b, ADR-077).

    GET /api/v1/settings/preferences   preferencias del usuario autenticado
    PUT /api/v1/settings/preferences   cambia las que se envien

Son de **cada usuario**, no del tenant (`users.settings`, migracion 021): en un
mismo negocio cada persona puede usar la plataforma en su idioma. Cualquier rol
autenticado puede leer y cambiar las suyas, y solo las suyas: el usuario y el
tenant salen del token, nunca de la peticion.
"""

import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.exceptions import NOT_FOUND, VALIDATION_ERROR, AppException
from app.models.user import User
from app.schemas.preferences import UserPreferencesResponse, UserPreferencesUpdate
from app.services.i18n import DEFAULT_LANGUAGE, normalizar_idioma

logger = logging.getLogger(__name__)

router = APIRouter()

#: Todos los roles: las preferencias son de quien inicia sesion.
_ROLES = ("super_admin", "admin", "supervisor", "agent", "medical")

DEFAULT_THEME = "system"
_THEMES = ("light", "dark", "system")

#: Mezcla el parche dentro de `users.settings` sin tocar otras claves.
_ACTUALIZAR = text(
    """
    UPDATE users
    SET settings = COALESCE(settings, '{}'::jsonb) || CAST(:parche AS jsonb)
    WHERE id = :user_id AND client_id = :client_id
    RETURNING id
    """
)


def _a_respuesta(settings: dict[str, Any] | None) -> UserPreferencesResponse:
    """Normaliza lo guardado en el JSONB, con los defaults para lo que falte o sea basura."""
    ajustes = settings if isinstance(settings, dict) else {}
    tema = ajustes.get("theme")
    return UserPreferencesResponse(
        ui_language=normalizar_idioma(ajustes.get("ui_language")) or DEFAULT_LANGUAGE,
        theme=tema if tema in _THEMES else DEFAULT_THEME,
    )


@router.get("", response_model=UserPreferencesResponse)
async def get_user_preferences(
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> UserPreferencesResponse:
    """Devuelve las preferencias del usuario autenticado.

    Args:
        user: Usuario autenticado.

    Returns:
        Idioma de la interfaz y tema, con los defaults (`es`, `system`) si no eligio.

    Raises:
        AppException: 404 si el usuario del token ya no existe.
    """
    client_id: UUID = user["client_id"]
    async with tenant_session(client_id) as session:
        fila = (
            await session.execute(
                select(User.settings).where(
                    User.id == UUID(str(user["user_id"])), User.client_id == client_id
                )
            )
        ).first()
    if fila is None:
        raise AppException(status_code=404, error_code=NOT_FOUND, message="Usuario no encontrado")
    return _a_respuesta(fila[0])


@router.put("")
async def update_user_preferences(
    data: UserPreferencesUpdate,
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> dict[str, dict[str, str]]:
    """Cambia las preferencias del usuario autenticado.

    Args:
        data: Preferencias a cambiar; las ausentes no se tocan.
        user: Usuario autenticado.

    Returns:
        `{"updated": {...}}` con lo que se guardo.

    Raises:
        AppException: 400 si no se envio ninguna preferencia; 404 si el usuario
            del token ya no existe.
    """
    client_id: UUID = user["client_id"]
    cambios = data.model_dump(exclude_none=True)
    if not cambios:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message="Indica al menos una preferencia: ui_language o theme",
        )

    async with tenant_session(client_id) as session:
        resultado = await session.execute(
            _ACTUALIZAR,
            {
                "parche": json.dumps(cambios),
                "user_id": str(user["user_id"]),
                "client_id": str(client_id),
            },
        )
        if resultado.first() is None:
            raise AppException(
                status_code=404, error_code=NOT_FOUND, message="Usuario no encontrado"
            )

    logger.info("Preferencias del usuario %s actualizadas: %s", user["user_id"], sorted(cambios))
    return {"updated": cambios}
