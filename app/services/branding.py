"""Logo subido de un tenant: de quien es la ruta y que no se copia al clonar (ADR-088).

El logo vive en Storage bajo `{client_id}/branding/...` y su ruta en
`clients.theme_config["logo_path"]`. Las plantillas de tenant y el sandbox copiaban
`theme_config` entero: el tenant nuevo apuntaba al archivo del original, y al descargar o
**borrar** "su" logo leia o borraba el de otro tenant. Ahora no se copia, y la API solo usa
rutas que empiecen por el prefijo del propio tenant.
"""

from typing import Any
from uuid import UUID

#: Claves de `theme_config` que describen el archivo subido (no una URL externa).
CLAVES_LOGO_SUBIDO = ("logo_path", "logo_content_type")


def sin_logo_subido(tema: dict[str, Any] | None) -> dict[str, Any]:
    """Copia de `theme_config` sin el archivo de logo, para clonar a otro tenant.

    Args:
        tema: `theme_config` del tenant de origen.

    Returns:
        Copia sin `logo_path` ni `logo_content_type` (la `logo_url` externa se conserva).
    """
    return {k: v for k, v in (tema or {}).items() if k not in CLAVES_LOGO_SUBIDO}


def ruta_de_logo_propia(ruta: Any, client_id: UUID) -> str | None:
    """La ruta del logo si es del propio tenant; `None` si no.

    Args:
        ruta: Valor de `theme_config["logo_path"]`.
        client_id: Tenant que la usa.

    Returns:
        La ruta, solo si esta bajo `{client_id}/branding/` y no tiene `..`.
    """
    if not isinstance(ruta, str) or ".." in ruta:
        return None
    return ruta if ruta.startswith(f"{client_id}/branding/") else None
