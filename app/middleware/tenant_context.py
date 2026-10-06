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


def _token_invalido() -> JSONResponse:
    """Respuesta 401 uniforme para un token que no sirve como access token."""
    return JSONResponse(
        status_code=401,
        content={"error_code": "INVALID_TOKEN", "message": "Token inválido"},
    )


# Rutas que NO requieren autenticación
PUBLIC_PATHS: set[str] = {
    "/internal/health",
    # Lo scrapea Prometheus por la red interna de docker-compose, sin JWT
    # (Sprint 8). No sale por Traefik: ver app/api/internal/metrics.py.
    "/internal/metrics",
    "/api/docs",
    "/api/redoc",
    "/api/openapi.json",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    # Auto-registro y verificacion de email (Sprint 15): no hay sesion todavia.
    "/api/v1/onboarding/register",
    "/api/v1/onboarding/verify-email",
    # Link de un clic en el email de la encuesta CSAT (Sprint 11, Dev B): quien
    # hace click no tiene JWT. La seguridad la da `survey_id`, no la sesion —
    # ver el docstring de `app/api/v1/csat.py`.
    "/api/v1/csat/respond",
}

# Prefijos de rutas con autenticación propia (firma, no JWT)
WEBHOOK_PATHS_PREFIX = "/api/v1/webhooks/"
# Webhooks de Twilio Voice (Sprint 13): firmados con `X-Twilio-Signature`. Solo
# este prefijo; el resto de `/api/v1/voice` (llamadas salientes, listado) va con JWT.
VOICE_WEBHOOK_PATHS_PREFIX = "/api/v1/voice/twilio/"
# Captura publica de leads (Sprint 16): la autenticacion es el token de la fuente, que va en
# la URL; quien rellena un formulario web no tiene JWT.
CAPTURE_PATHS_PREFIX = "/api/v1/capture/"


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
        if path in PUBLIC_PATHS or path.startswith(
            (WEBHOOK_PATHS_PREFIX, VOICE_WEBHOOK_PATHS_PREFIX, CAPTURE_PATHS_PREFIX)
        ):
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

        # Solo un access token autentica una peticion. Sin esta comprobacion un refresh
        # token (7 dias, que nunca se revalida contra `is_active`) valia como Bearer en
        # cualquier endpoint: desactivar a un usuario o suspender a un tenant no cortaba el
        # acceso hasta que el refresh caducara. Lo mismo vale para cualquier otro tipo de
        # token firmado con el mismo secreto (verificacion de email, etc.).
        if payload.get("type") != "access":
            return _token_invalido()

        # Un token firmado pero sin las claims esperadas es un 401, no un 500 por KeyError.
        try:
            client_id = UUID(payload["client_id"])
            user_id = UUID(payload["user_id"])
            role = payload["role"]
        except (KeyError, ValueError, TypeError, AttributeError):
            return _token_invalido()
        if not isinstance(role, str):
            return _token_invalido()

        # Inyectar contexto en request.state
        request.state.client_id = client_id
        request.state.user_id = user_id
        request.state.user_role = role
        request.state.user_email = payload.get("email", "")

        return await call_next(request)
