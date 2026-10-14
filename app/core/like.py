"""Patrones `LIKE`/`ILIKE` a partir de texto del usuario.

Sin escapar, `%` y `_` del texto son comodines: `search=%` lista todo y `search=_%_%_%...`
obliga a PostgreSQL a probar combinaciones caras en cada fila. Se escapan y la consulta usa
`escape="\\"`.
"""

#: Caracter de escape que deben pasar las consultas: `col.ilike(patron, escape=ESCAPE)`.
ESCAPE = "\\"


def escapar_like(texto: str) -> str:
    """Neutraliza `\\`, `%` y `_` para usar un texto dentro de un `LIKE`.

    Args:
        texto: Texto del usuario.

    Returns:
        El texto con los comodines escapados.
    """
    return texto.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def patron_contiene(texto: str) -> str:
    """Patron "contiene" (`%texto%`) con el texto escapado.

    Args:
        texto: Texto del usuario.

    Returns:
        El patron para `ilike(..., escape=ESCAPE)`.
    """
    return f"%{escapar_like(texto.strip())}%"
