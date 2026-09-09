"""Middleware de contexto de tenant — JWT → request.state.

Extrae el JWT del header Authorization, valida el token y
inyecta client_id, user_id y role en request.state para
que los endpoints y dependencies lo consuman.

Rutas públicas (health, docs, auth) se dejan pasar sin token.
"""

import logging
from uuid import UUID

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.exceptions import JWTExpiredError, JWTInvalidError
from app.core.security import decode_jwt

logger = logging.getLogger(__name__)

# Rutas que NO requieren autenticación
PUBLIC_PATHS: set[str] = {
    "/internal/health",
    "/api/docs",
    "/api/redoc",
    "/api/openapi.json",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
}

# Prefijos de rutas con autenticación propia (firma, no JWT)
WEBHOOK_PATHS_PREFIX = "/api/v1/webhooks/"


class TenantContextMiddleware(BaseHTTPMiddleware):
    """Middleware que extrae JWT y configura el contexto del tenant.

    Para cada request:
    1. Deja pasar rutas públicas y webhooks sin validar JWT.
    2. Extrae el token del header Authorization: Bearer <token>.
    3. Decodifica y valida el JWT.
    4. Inyecta client_id, user_id y user_role en request.state.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Procesa cada request para extraer y validar el JWT.

        Args:
            request: Request entrante.
            call_next: Siguiente handler en la cadena.

        Returns:
            Response del endpoint o error JSON si el JWT es inválido.
        """
        path = request.url.path

        # Rutas públicas: no requieren JWT
        if path in PUBLIC_PATHS or path.startswith(WEBHOOK_PATHS_PREFIX):
            return await call_next(request)

        # Extraer header Authorization
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={
                    "error_code": "MISSING_TOKEN",
                    "message": "Token de autenticación requerido",
                },
            )

        token = auth_header.split(" ", 1)[1]

        try:
            payload = decode_jwt(token)
        except JWTExpiredError:
            return JSONResponse(
                status_code=401,
                content={
                    "error_code": "TOKEN_EXPIRED",
                    "message": "Token expirado",
                },
            )
        except JWTInvalidError:
            return JSONResponse(
                status_code=401,
                content={
                    "error_code": "INVALID_TOKEN",
                    "message": "Token inválido",
                },
            )

        # Inyectar contexto en request.state
        request.state.client_id = UUID(payload["client_id"])
        request.state.user_id = UUID(payload["user_id"])
        request.state.user_role = payload["role"]
        request.state.user_email = payload.get("email", "")

        return await call_next(request)
