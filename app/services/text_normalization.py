"""Normalizacion de texto para comparar terminos escritos por personas (Sprint 17).

"Tecnología", "TECNOLOGIA" y "tecnologia " son el mismo sector para el ICP. Se compara sobre
minusculas sin acentos y con los separadores reducidos a un espacio.
"""

import re
import unicodedata

_NO_ALFANUMERICO = re.compile(r"[^a-z0-9]+")


def normalizar_texto(texto: str) -> str:
    """Minusculas, sin acentos y con cualquier separador reducido a un espacio.

    Args:
        texto: Texto libre ("Director/a de Tecnología").

    Returns:
        El texto comparable ("director a de tecnologia"); vacio si no tenia letras ni digitos.
    """
    sin_acentos = unicodedata.normalize("NFKD", texto.lower())
    ascii_ = "".join(c for c in sin_acentos if not unicodedata.combining(c))
    return _NO_ALFANUMERICO.sub(" ", ascii_).strip()


def tokens(texto: str) -> tuple[str, ...]:
    """Palabras del texto normalizado.

    Args:
        texto: Texto libre.

    Returns:
        Las palabras, en orden.
    """
    normalizado = normalizar_texto(texto)
    return tuple(normalizado.split()) if normalizado else ()
