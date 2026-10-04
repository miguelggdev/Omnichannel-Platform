"""Schemas de las preferencias de usuario (Sprint 14b, ADR-077)."""

from typing import Literal

from pydantic import BaseModel

UiLanguage = Literal["es", "en", "pt", "it", "de", "fr"]
Theme = Literal["light", "dark", "system"]


class UserPreferencesUpdate(BaseModel):
    """Cuerpo de `PUT /api/v1/settings/preferences`.

    Solo se guardan los campos enviados; el resto de las preferencias no cambia.

    Attributes:
        ui_language: Idioma de la interfaz.
        theme: Tema visual.
    """

    ui_language: UiLanguage | None = None
    theme: Theme | None = None


class UserPreferencesResponse(BaseModel):
    """Preferencias efectivas de un usuario.

    Attributes:
        ui_language: Idioma de la interfaz (`es` si no eligio uno).
        theme: Tema visual (`system` si no eligio uno).
    """

    ui_language: UiLanguage
    theme: Theme
