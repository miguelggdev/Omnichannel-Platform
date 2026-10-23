"""Excepciones personalizadas y handler global.

NUNCA exponer tracebacks al cliente. Todos los errores se envuelven en
AppException con un error_code legible y un mensaje seguro.
"""

import logging
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# Códigos de error estándar
MISSING_TOKEN = "MISSING_TOKEN"  # noqa: S105  # nosec B105
TOKEN_EXPIRED = "TOKEN_EXPIRED"  # noqa: S105  # nosec B105
INVALID_TOKEN = "INVALID_TOKEN"  # noqa: S105  # nosec B105
FORBIDDEN = "FORBIDDEN"
NOT_FOUND = "NOT_FOUND"
DUPLICATE = "DUPLICATE"
VALIDATION_ERROR = "VALIDATION_ERROR"
CONFLICT = "CONFLICT"
DELIVERY_FAILED = "DELIVERY_FAILED"
MODULE_DISABLED = "MODULE_DISABLED"
INTERNAL_ERROR = "INTERNAL_ERROR"


class AppException(Exception):  # noqa: N818
    """Excepción base de la aplicación.

    Args:
        status_code: HTTP status code.
        error_code: Código de error interno (e.g., MISSING_TOKEN).
        message: Mensaje seguro para el cliente (sin tracebacks).
        details: Detalles opcionales adicionales.
    """

    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.details = details
        super().__init__(message)


class JWTExpiredError(AppException):
    """Token JWT expirado."""

    def __init__(self, message: str = "Token expirado") -> None:
        super().__init__(
            status_code=401,
            error_code=TOKEN_EXPIRED,
            message=message,
        )


class JWTInvalidError(AppException):
    """Token JWT inválido o corrupto."""

    def __init__(self, message: str = "Token inválido") -> None:
        super().__init__(
            status_code=401,
            error_code=INVALID_TOKEN,
            message=message,
        )


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    """Handler global para AppException.

    Args:
        request: Request de FastAPI.
        exc: Instancia de AppException.

    Returns:
        JSONResponse con error_code y message. NUNCA expone tracebacks.
    """
    content: dict[str, Any] = {
        "error_code": exc.error_code,
        "message": exc.message,
    }
    if exc.details:
        content["details"] = exc.details

    return JSONResponse(status_code=exc.status_code, content=content)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handler para excepciones no controladas.

    Loguea el traceback internamente pero NUNCA lo expone al cliente.

    Args:
        request: Request de FastAPI.
        exc: Excepción no controlada.

    Returns:
        JSONResponse genérico con INTERNAL_ERROR.
    """
    logger.exception("Error interno no controlado: %s", exc)
    return JSONResponse(
        status_code=500,
        content={
            "error_code": INTERNAL_ERROR,
            "message": "Error interno del servidor",
        },
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Handler para errores de validacion (422) que no repite lo que se envio.

    El de FastAPI devuelve cada error con su `input`: un login con un email mal formado
    devolvia la contrasena, y un cuerpo de un webhook o de un formulario devolvia datos
    personales en la respuesta (y en cualquier log o proxy que la guarde). Se conservan
    `loc`, `msg` y `type`, que es lo que necesita el frontend para pintar el error.

    Args:
        request: Request de FastAPI.
        exc: Error de validacion.

    Returns:
        JSONResponse 422 con `error_code`, `message` y `detail` sin `input` ni `ctx`.
    """
    detalle = [
        {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")), "type": e.get("type", "")}
        for e in exc.errors()
    ]
    # Mismo resumen que hacia el frontend con `detail` ("campo: motivo"), para que `message`
    # siga siendo util: `errorMessage()` lo prefiere cuando existe.
    resumen = "; ".join(
        f"{'.'.join(str(p) for p in d['loc'] if p != 'body')}: {d['msg']}".lstrip(": ")
        for d in detalle
    )
    return JSONResponse(
        status_code=422,
        content={
            "error_code": VALIDATION_ERROR,
            "message": resumen or "Datos de entrada no validos",
            "detail": detalle,
        },
    )
