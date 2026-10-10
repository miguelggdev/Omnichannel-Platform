"""Doble de Redis para `app/core/rate_limit.py`: transaccion `SET NX EX` + `INCR`, `GET`, `DEL`."""

from typing import Any


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

    async def get(self, clave: str) -> str | None:
        """`GET` con el valor como texto, como con `decode_responses=True`."""
        if self.caido:
            raise ConnectionError("redis caido")
        return str(self.datos[clave]) if clave in self.datos else None

    async def delete(self, clave: str) -> int:
        """`DEL` de una clave."""
        if self.caido:
            raise ConnectionError("redis caido")
        return 1 if self.datos.pop(clave, None) is not None else 0
