"""Score total del lead a partir de sus tres componentes y los pesos del tenant (ADR-082).

El score total es `(fit*w_fit + behavioral*w_behavioral + ai*w_ai) / (w_fit + w_behavioral + w_ai)`,
redondeado. Los pesos viven en `clients.lead_scoring_weights` (por defecto 40/30/30) y se
dividen por su suma, asi que no tienen que sumar exactamente 100.

Es una funcion pura, no una columna `GENERATED` de la base (como pedia el spec): los pesos son
configurables por tenant y una columna generada no puede leer otra tabla.
"""

from collections.abc import Mapping
from typing import Any

DEFAULT_WEIGHTS: dict[str, int] = {"fit": 40, "behavioral": 30, "ai": 30}
_CLAVES = ("fit", "behavioral", "ai")
SCORE_MAX = 100


class InvalidWeightsError(ValueError):
    """Los pesos de scoring no son utilizables (faltan claves, son negativos o suman 0)."""


def normalizar_pesos(pesos: Mapping[str, Any] | None) -> dict[str, int]:
    """Valida los pesos de un tenant o devuelve los de por defecto si no hay.

    Args:
        pesos: `clients.lead_scoring_weights`. `None` o vacio equivale al valor por defecto.

    Returns:
        `{"fit": n, "behavioral": n, "ai": n}` con enteros no negativos y suma positiva.

    Raises:
        InvalidWeightsError: Si falta alguna clave, un peso no es un entero no negativo (un
            `bool` tampoco cuenta) o los tres suman cero.
    """
    if not pesos:
        return dict(DEFAULT_WEIGHTS)
    resultado: dict[str, int] = {}
    for clave in _CLAVES:
        valor = pesos.get(clave)
        if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
            raise InvalidWeightsError(f"El peso {clave!r} debe ser un entero >= 0")
        resultado[clave] = valor
    if sum(resultado.values()) == 0:
        raise InvalidWeightsError("Los pesos no pueden sumar 0")
    return resultado


def compute_total_score(
    fit: int, behavioral: int, ai: int, pesos: Mapping[str, Any] | None = None
) -> int:
    """Combina los tres scores con los pesos del tenant.

    Args:
        fit: Encaje con el ICP, 0-100.
        behavioral: Interaccion, 0-100.
        ai: Valoracion de la IA, 0-100.
        pesos: Pesos del tenant; `None` usa 40/30/30.

    Returns:
        El score total, entero entre 0 y 100 (la mitad se redondea hacia arriba).

    Raises:
        ValueError: Si algun score esta fuera de 0-100.
        InvalidWeightsError: Si los pesos no son validos.
    """
    for nombre, valor in (("fit", fit), ("behavioral", behavioral), ("ai", ai)):
        if not 0 <= valor <= SCORE_MAX:
            raise ValueError(f"El score {nombre} debe estar entre 0 y {SCORE_MAX}, no {valor}")
    w = normalizar_pesos(pesos)
    total = sum(w.values())
    # Aritmetica entera: sin flotantes no hay `round()` bancario ni .4999999.
    ponderado = fit * w["fit"] + behavioral * w["behavioral"] + ai * w["ai"]
    return (2 * ponderado + total) // (2 * total)
