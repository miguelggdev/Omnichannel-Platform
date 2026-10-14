"""Fixtures comunes de los tests unitarios."""

import pytest

from tests.unit.rate_limit_doubles import FakeRedis


@pytest.fixture(autouse=True)
def _limitador_en_memoria(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """Los limites de `app/core/rate_limit.py` cuentan en memoria, no en un Redis real.

    Sin esto, con un Redis local los contadores sobreviven entre tests y ejecuciones (la IP
    de `TestClient` es siempre `testclient`) y los tests dependen del orden; sin Redis, como
    en CI, el limitador falla abierto y nunca se ejercita. Un test que necesite el doble lo
    vuelve a parchear y se queda con su referencia.
    """
    doble = FakeRedis()
    monkeypatch.setattr("app.core.rate_limit.get_redis", lambda: doble)
    # La rotacion de refresh tokens (ADR-089) marca cada uso en el mismo Redis.
    monkeypatch.setattr("app.core.session_cookie.get_redis", lambda: doble)
    return doble
