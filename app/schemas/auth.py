"""Schemas de autenticación — Login, tokens, refresh."""

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    """Request de login con email y password.

    Attributes:
        email: Email del usuario.
        password: Password en texto plano.
    """

    email: EmailStr
    password: str = Field(min_length=8, description="Password del usuario")


class TokenResponse(BaseModel):
    """Respuesta con tokens JWT.

    Attributes:
        access_token: Token de acceso JWT.
        refresh_token: Token de refresco JWT.
        token_type: Tipo de token (siempre 'bearer').
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    """Request para renovar access token.

    Attributes:
        refresh_token: Token de refresco JWT válido.
    """

    refresh_token: str
