"""Tests del limitador de frecuencia (`app/core/rate_limit.py`, ADR-088).

Redis se sustituye por un doble que imita una transaccion `MULTI`: `SET NX EX` + `INCR`.
"""

import pytest

from app.core import rate_limit
from app.core.exceptions import AppException
from tests.unit.rate_limit_doubles import FakeRedis


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """Redis sano sustituido en el modulo."""
    doble = FakeRedis()
    monkeypatch.setattr(rate_limit, "get_redis", lambda: doble)
    return doble


class TestContador:
    async def test_la_clave_nace_con_ttl_en_la_misma_transaccion(self, redis: FakeRedis) -> None:
        """Nunca hay contador sin caducidad (con INCR + EXPIRE separados si podia haberlo)."""
        assert await rate_limit.dentro_del_limite("x:1", 5, 60)
        assert redis.ttl["ratelimit:x:1"] == 60
        assert redis.datos["ratelimit:x:1"] == 1

    async def test_el_ttl_no_se_reinicia_con_cada_evento(self, redis: FakeRedis) -> None:
        """`SET NX` no pisa la ventana existente: si no, un flujo constante nunca caduca."""
        await rate_limit.dentro_del_limite("x:1", 5, 60)
        redis.ttl["ratelimit:x:1"] = 7
        await rate_limit.dentro_del_limite("x:1", 5, 60)
        assert redis.ttl["ratelimit:x:1"] == 7
        assert redis.datos["ratelimit:x:1"] == 2


class TestDentroDelLimite:
    async def test_corta_al_pasar_el_limite(self, redis: FakeRedis) -> None:
        resultados = [await rate_limit.dentro_del_limite("ia:1", 3, 60) for _ in range(5)]
        assert resultados == [True, True, True, False, False]

    async def test_claves_distintas_no_comparten_contador(self, redis: FakeRedis) -> None:
        for _ in range(3):
            await rate_limit.dentro_del_limite("ia:1", 3, 60)
        assert await rate_limit.dentro_del_limite("ia:2", 3, 60)

    async def test_falla_abierto_si_redis_no_responde(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rate_limit, "get_redis", lambda: FakeRedis(caido=True))
        assert await rate_limit.dentro_del_limite("ia:1", 1, 60)


class TestHit:
    async def test_429_al_pasar_el_limite(self, redis: FakeRedis) -> None:
        await rate_limit.hit("login:1.2.3.4", 2, 60)
        await rate_limit.hit("login:1.2.3.4", 2, 60)
        with pytest.raises(AppException) as exc:
            await rate_limit.hit("login:1.2.3.4", 2, 60)
        assert exc.value.status_code == 429
        assert exc.value.error_code == rate_limit.RATE_LIMITED

    async def test_falla_cerrado_con_503_si_redis_no_responde(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rate_limit, "get_redis", lambda: FakeRedis(caido=True))
        with pytest.raises(AppException) as exc:
            await rate_limit.hit("login:1.2.3.4", 2, 60)
        assert exc.value.status_code == 503
