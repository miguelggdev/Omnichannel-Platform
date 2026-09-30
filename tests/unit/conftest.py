"""Fixtures compartidas de los tests unitarios."""

from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def llamada_sin_autenticar(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Ninguna llamada esta autenticada por PIN, salvo que el test diga lo contrario.

    `contacto_autenticado()` consulta Redis para las llamadas de voz; en los tests
    unitarios eso no debe salir a la red. Es autouse porque cualquier test que
    pase por un nodo o una tool de un agente con canal `voice` lo tocaria.

    Returns:
        El doble de `profesional_de_la_llamada`, para que un test lo reconfigure.
    """
    doble = AsyncMock(return_value=None)
    monkeypatch.setattr("app.services.channel_identity.profesional_de_la_llamada", doble)
    return doble
