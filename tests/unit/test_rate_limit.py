"""Tests del limitador de frecuencia (`app/core/rate_limit.py`, ADR-088).

Redis se sustituye por un doble que imita una transaccion `MULTI`: `SET NX EX` + `INCR`.
"""

from typing import Any

import pytest

from app.core import rate_limit
from app.core.exceptions import AppException


class FakePipeline:
    """Pipeline transaccional minimo sobre el diccionario de `FakeRedis`."""

    def __init__(self, redis: "FakeRedis") -> None:
        """Prepara la cola de comandos.

        Args:
            redis: Doble al que se aplican los comandos al ejecutar.
        """
        self.redis = redis
        self.cola: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def __aenter__(self) -> "FakePipeline":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def set(self, *args: Any, **kwargs: Any) -> None:
        """Encola un `SET`."""
        self.cola.append(("set", args, kwargs))

    def incr(self, *args: Any) -> None:
        """Encola un `INCR`."""
        self.cola.append(("incr", args, {}))

    async def execute(self) -> list[Any]:
        """Aplica la cola de golpe, como `EXEC`."""
        if self.redis.caido:
            raise ConnectionError("redis caido")
        salida: list[Any] = []
        for comando, args, kwargs in self.cola:
            if comando == "set":
                clave, valor = args
                if kwargs.get("nx") and clave in self.redis.datos:
                    salida.append(None)
                    continue
                self.redis.datos[clave] = int(valor)
                self.redis.ttl[clave] = kwargs["ex"]
                salida.append(True)
            else:
                (clave,) = args
                self.redis.datos[clave] = self.redis.datos.get(clave, 0) + 1
                salida.append(self.redis.datos[clave])
        return salida


class FakeRedis:
    """Redis en memoria con TTL registrado (no caduca: basta con ver que se fija)."""

    def __init__(self, caido: bool = False) -> None:
        """Prepara el almacen.

        Args:
            caido: Si `True`, toda ejecucion falla como un Redis caido.
        """
        self.datos: dict[str, int] = {}
        self.ttl: dict[str, int] = {}
        self.caido = caido

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        """Devuelve un pipeline; el limitador siempre lo pide transaccional."""
        assert transaction is True
        return FakePipeline(self)


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
