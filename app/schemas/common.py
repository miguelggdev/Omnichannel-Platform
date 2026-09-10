"""Schemas comunes — Paginación, errores, respuestas genéricas."""

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PaginationParams(BaseModel):
    """Parámetros de paginación para queries.

    Attributes:
        page: Número de página (1-indexed).
        page_size: Cantidad de items por página.
    """

    page: int = Field(default=1, ge=1, description="Número de página")
    page_size: int = Field(default=20, ge=1, le=100, description="Items por página")


class PaginatedResponse(BaseModel, Generic[T]):
    """Respuesta paginada genérica.

    Attributes:
        items: Lista de items de la página actual.
        total: Total de items en la colección.
        page: Página actual.
        page_size: Tamaño de página.
        total_pages: Número total de páginas.
    """

    items: list[T]
    total: int
    page: int
    page_size: int
    total_pages: int


class ErrorResponse(BaseModel):
    """Respuesta de error estándar.

    Attributes:
        error_code: Código de error interno.
        message: Mensaje descriptivo seguro (sin tracebacks).
        details: Detalles opcionales adicionales.
    """

    error_code: str
    message: str
    details: dict[str, Any] | None = None
