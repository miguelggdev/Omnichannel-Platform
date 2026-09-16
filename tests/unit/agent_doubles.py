"""Dobles compartidos por los tests unitarios de los nodos del grafo (Sprint 6).

Los cinco archivos de tests de nodos necesitan lo mismo: una sesion de base
falsa, un Redis falso, un modelo de chat falso y un estado de conversacion de
partida. Vive aqui para no repetirlo cinco veces.

Ninguno de estos dobles toca red ni base de datos: los tests de nodos corren sin
`--run-db`. El recorrido contra PostgreSQL real vive en
`tests/integration/test_graph_flow.py`.
"""

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.agents.nodes._tenant import AgentSettings


class FakeResult:
    """Resultado de `session.execute()` con un valor prefijado."""

    def __init__(self, valor: Any = None) -> None:
        """Guarda el valor que devolveran los accesores.

        Args:
            valor: Escalar o lista que el test quiere que devuelva la consulta.
        """
        self._valor = valor

    def scalar_one_or_none(self) -> Any:
        """Devuelve el escalar prefijado."""
        return self._valor

    def scalars(self) -> "FakeResult":
        """Permite encadenar `.scalars().all()`."""
        return self

    def all(self) -> list[Any]:
        """Devuelve la lista prefijada, o una lista vacia."""
        return list(self._valor) if isinstance(self._valor, list) else []

    def fetchall(self) -> list[Any]:
        """Alias de `all()` para las consultas en SQL crudo."""
        return self.all()


class FakeSession:
    """AsyncSession minima: devuelve resultados prefijados y registra escrituras.

    Attributes:
        resultados: Cola de valores que devuelven las llamadas a `execute()`.
        objetos: Mapa `pk -> objeto` que resuelve `session.get()`.
        added: Objetos pasados a `add()`.
        executed: Sentencias que recibio `execute()`.
        flushes: Cuantas veces se llamo a `flush()`.
    """

    def __init__(
        self,
        resultados: list[Any] | None = None,
        objetos: dict[Any, Any] | None = None,
    ) -> None:
        """Prepara la sesion falsa.

        Args:
            resultados: Valores que devolveran las llamadas sucesivas a `execute()`.
            objetos: Objetos que puede resolver `get()`, indexados por su pk.
        """
        self.resultados = list(resultados or [])
        self.objetos = dict(objetos or {})
        self.added: list[Any] = []
        self.executed: list[Any] = []
        self.flushes = 0

    async def execute(self, stmt: Any = None, params: Any = None) -> FakeResult:
        """Registra la sentencia y devuelve el siguiente resultado de la cola."""
        self.executed.append(stmt)
        valor = self.resultados.pop(0) if self.resultados else None
        return FakeResult(valor)

    async def get(self, model: Any, pk: Any) -> Any:
        """Resuelve un objeto por su clave primaria."""
        return self.objetos.get(pk)

    def add(self, obj: Any) -> None:
        """Registra un objeto agregado a la sesion."""
        self.added.append(obj)

    async def flush(self) -> None:
        """Simula el flush asignando ids a los objetos que no lo tienen."""
        self.flushes += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self) -> None:
        """No hace nada: `tenant_session` commitea al cerrar el contexto."""

    def agregados_de(self, tipo: type) -> list[Any]:
        """Devuelve los objetos agregados de un tipo concreto.

        Args:
            tipo: Clase del modelo que se busca.

        Returns:
            Los objetos de ese tipo pasados a `add()`.
        """
        return [obj for obj in self.added if isinstance(obj, tipo)]


def fake_tenant_session(sesion: FakeSession) -> Callable[..., Any]:
    """Construye un sustituto de `tenant_session` que cede una sesion falsa.

    Args:
        sesion: Sesion que recibiran todos los `async with`.

    Returns:
        Context manager asincrono con la misma firma que `tenant_session`.
    """

    @asynccontextmanager
    async def _cm(client_id: Any) -> AsyncIterator[FakeSession]:
        yield sesion

    return _cm


def parchear_tenant_session(
    monkeypatch: pytest.MonkeyPatch, modulo: Any, sesion: FakeSession
) -> FakeSession:
    """Sustituye `tenant_session` en un modulo por una sesion falsa.

    Args:
        monkeypatch: Fixture de pytest.
        modulo: Modulo que importo `tenant_session`.
        sesion: Sesion falsa a ceder.

    Returns:
        La misma sesion, por comodidad al encadenar.
    """
    monkeypatch.setattr(modulo, "tenant_session", fake_tenant_session(sesion))
    return sesion


def parchear_agent_settings(
    monkeypatch: pytest.MonkeyPatch, modulo: Any, settings: AgentSettings
) -> None:
    """Sustituye `get_agent_settings` en un modulo por una configuracion fija.

    Args:
        monkeypatch: Fixture de pytest.
        modulo: Modulo que importo `get_agent_settings`.
        settings: Configuracion que devolvera.
    """

    async def _get(client_id: Any) -> AgentSettings:
        return settings

    monkeypatch.setattr(modulo, "get_agent_settings", _get)


class FakeRedis:
    """Redis asincrono en memoria, con modo "caido" para probar la degradacion.

    Attributes:
        store: Claves guardadas.
        borradas: Claves que se pidieron borrar.
        falla: Si True, toda operacion lanza.
    """

    def __init__(self, falla: bool = False) -> None:
        """Prepara el doble.

        Args:
            falla: Si True, cada operacion lanza `ConnectionError`.
        """
        self.store: dict[str, str] = {}
        self.borradas: list[str] = []
        self.falla = falla

    async def get(self, key: str) -> str | None:
        """Lee una clave."""
        if self.falla:
            raise ConnectionError("redis caido")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        """Escribe una clave."""
        if self.falla:
            raise ConnectionError("redis caido")
        self.store[key] = value
        return True

    async def delete(self, key: str) -> int:
        """Borra una clave."""
        if self.falla:
            raise ConnectionError("redis caido")
        self.borradas.append(key)
        return int(self.store.pop(key, None) is not None)


class RespuestaLLM:
    """`AIMessage` minimo: contenido y consumo de tokens.

    Attributes:
        content: Texto de la respuesta.
        usage_metadata: Consumo en el formato normalizado de LangChain.
    """

    def __init__(self, content: str = "", input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Prepara la respuesta.

        Args:
            content: Texto que devuelve el modelo.
            input_tokens: Tokens de entrada a reportar.
            output_tokens: Tokens generados a reportar.
        """
        self.content = content
        self.usage_metadata = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }


class FakeChatModel:
    """Modelo de chat que devuelve una respuesta fija y registra lo que recibio.

    Attributes:
        respuesta: Lo que devuelve `ainvoke()`.
        llamadas: Mensajes con los que se invoco.
        structured_con: Argumentos de `with_structured_output()`.
    """

    def __init__(self, respuesta: Any) -> None:
        """Prepara el doble.

        Args:
            respuesta: Valor que devolvera `ainvoke()`.
        """
        self.respuesta = respuesta
        self.llamadas: list[Any] = []
        self.structured_con: Any = None

    def with_structured_output(self, schema: Any, include_raw: bool = False) -> "FakeChatModel":
        """Registra la peticion de salida estructurada y se devuelve a si mismo."""
        self.structured_con = (schema, include_raw)
        return self

    async def ainvoke(self, messages: Any) -> Any:
        """Devuelve la respuesta fija, registrando los mensajes recibidos."""
        self.llamadas.append(messages)
        return self.respuesta


def parchear_chat_model(
    monkeypatch: pytest.MonkeyPatch, modulo: Any, modelo: FakeChatModel
) -> list[tuple[str, float]]:
    """Sustituye `get_chat_model` en un modulo por un doble.

    Args:
        monkeypatch: Fixture de pytest.
        modulo: Modulo que importo `get_chat_model`.
        modelo: Doble que se devolvera.

    Returns:
        Lista donde se van registrando los `(modelo, temperatura)` pedidos.
    """
    pedidos: list[tuple[str, float]] = []

    def _get(model: str, temperature: float = 0.0) -> FakeChatModel:
        pedidos.append((model, temperature))
        return modelo

    monkeypatch.setattr(modulo, "get_chat_model", _get)
    return pedidos


def estado(**overrides: Any) -> dict[str, Any]:
    """Construye un `ConversationState` de partida para los tests.

    Args:
        **overrides: Campos a sobreescribir.

    Returns:
        Estado con ids validos y un mensaje de texto.
    """
    base: dict[str, Any] = {
        "client_id": str(uuid.uuid4()),
        "conversation_id": str(uuid.uuid4()),
        "contact_id": str(uuid.uuid4()),
        "channel": "whatsapp",
        "message": {"text": "Cual es el horario de atencion?"},
        "intent": None,
        "intent_confidence": None,
        "rag_context": None,
        "rag_confidence": None,
        "response_text": None,
        "budget_status": "ok",
        "model_to_use": "gpt-4o",
        "budget_usage_pct": 0.0,
        "requires_handoff": False,
        "handoff_reason": None,
        "training_mode": False,
        "approved_examples": None,
        "error": None,
    }
    base.update(overrides)
    return base
