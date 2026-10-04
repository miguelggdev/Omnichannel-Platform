"""Variacion porcentual de la analitica: el caso que no se puede dividir."""

import pytest

from app.api.v1.analytics import _tendencia


@pytest.mark.parametrize(
    ("actual", "anterior", "esperado"),
    [
        (3, 1, 200.0),
        (1, 4, -75.0),
        (5, 5, 0.0),
        (0, 4, -100.0),
        (7, 0, None),  # periodo anterior vacio: no hay base para comparar
        (0, 0, None),
    ],
)
def test_tendencia(actual: int, anterior: int, esperado: float | None) -> None:
    assert _tendencia(actual, anterior) == esperado
