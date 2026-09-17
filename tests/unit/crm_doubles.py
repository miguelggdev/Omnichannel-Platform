"""Dobles compartidos por los tests unitarios del CRM (Sprint 7).

Extienden los de `agent_doubles.py` (Sprint 6) con lo que los endpoints de
contactos, conversaciones, etiquetas y notas usan y los nodos del grafo no:
`session.scalar()` para los `count(*)`, `session.refresh()` para los
`server_default`, `session.delete()` y el `rowcount` de un UPDATE/DELETE masivo.

Nada de esto toca la base: los tests corren sin `--run-db`. El recorrido contra
PostgreSQL real con RLS vive en `tests/integration/test_crm_api.py`.
"""

import datetime as dt
import uuid
from typing import Any

from tests.unit.agent_doubles import FakeResult, FakeSession

# Momento fijo con el que se rellenan los timestamps que en PostgreSQL pondria
# un server_default.
AHORA = dt.datetime(2026, 9, 16, 12, 0, tzinfo=dt.timezone.utc)


class CrmResult(FakeResult):
    """Resultado de `execute()` que ademas expone `rowcount`."""

    def __init__(self, valor: Any = None, rowcount: int = 0) -> None:
        """Guarda el valor prefijado y las filas afectadas.

        Args:
            valor: Escalar o lista que devuelven los accesores.
            rowcount: Filas afectadas por un UPDATE o DELETE.
        """
        super().__init__(valor)
        self.rowcount = rowcount


class CrmSession(FakeSession):
    """Sesion falsa para los endpoints del CRM.

    Attributes:
        escalares: Cola de valores que devuelven las llamadas a `scalar()`
            (los `count(*)` de las listas paginadas).
        rowcounts: Cola de filas afectadas que devuelven las llamadas a
            `execute()` con un UPDATE o DELETE.
        deleted: Objetos pasados a `delete()`.
        refreshed: Objetos pasados a `refresh()`.
    """

    def __init__(
        self,
        resultados: list[Any] | None = None,
        objetos: dict[Any, Any] | None = None,
        escalares: list[Any] | None = None,
        rowcounts: list[int] | None = None,
    ) -> None:
        """Prepara la sesion falsa.

        Args:
            resultados: Valores que devuelven las llamadas sucesivas a `execute()`.
            objetos: Objetos que resuelve `get()`, indexados por su pk.
            escalares: Valores que devuelven las llamadas sucesivas a `scalar()`.
            rowcounts: Filas afectadas por las llamadas sucesivas a `execute()`.
        """
        super().__init__(resultados=resultados, objetos=objetos)
        self.escalares = list(escalares or [])
        self.rowcounts = list(rowcounts or [])
        self.deleted: list[Any] = []
        self.refreshed: list[Any] = []

    async def execute(self, stmt: Any = None, params: Any = None) -> CrmResult:
        """Registra la sentencia y devuelve el siguiente resultado de la cola."""
        self.executed.append(stmt)
        valor = self.resultados.pop(0) if self.resultados else None
        filas = self.rowcounts.pop(0) if self.rowcounts else 0
        return CrmResult(valor, rowcount=filas)

    async def scalar(self, stmt: Any = None, params: Any = None) -> Any:
        """Devuelve el siguiente escalar de la cola (0 si se agoto)."""
        self.executed.append(stmt)
        return self.escalares.pop(0) if self.escalares else 0

    async def delete(self, obj: Any) -> None:
        """Registra el objeto borrado."""
        self.deleted.append(obj)

    async def refresh(self, obj: Any) -> None:
        """Rellena los campos que en PostgreSQL pondria un server_default."""
        self.refreshed.append(obj)
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        for campo in ("created_at", "updated_at"):
            if getattr(obj, campo, None) is None:
                setattr(obj, campo, AHORA)


class FakeContact:
    """Sustituto de Contact con lo que leen los endpoints."""

    def __init__(self, **kwargs: Any) -> None:
        """Construye el contacto con valores por defecto razonables."""
        self.id = kwargs.get("id") or uuid.uuid4()
        self.client_id = kwargs.get("client_id") or uuid.uuid4()
        self.first_name = kwargs.get("first_name", "Ada")
        self.last_name = kwargs.get("last_name", "Lovelace")
        self.display_name = kwargs.get("display_name", "Ada L.")
        self.merged_into_id = kwargs.get("merged_into_id")
        self.metadata_ = kwargs.get("metadata_", {})
        self.is_gdpr_deleted = kwargs.get("is_gdpr_deleted", False)
        self.gdpr_deleted_at = kwargs.get("gdpr_deleted_at")
        self.created_at = kwargs.get("created_at", AHORA)
        self.updated_at = kwargs.get("updated_at", AHORA)


class FakeConversation:
    """Sustituto de Conversation con lo que leen los endpoints."""

    def __init__(self, **kwargs: Any) -> None:
        """Construye la conversacion con valores por defecto razonables."""
        self.id = kwargs.get("id") or uuid.uuid4()
        self.client_id = kwargs.get("client_id") or uuid.uuid4()
        self.contact_id = kwargs.get("contact_id") or uuid.uuid4()
        self.channel = kwargs.get("channel", "whatsapp")
        self.status = kwargs.get("status", "bot_active")
        self.assigned_user_id = kwargs.get("assigned_user_id")
        self.subject = kwargs.get("subject")
        self.metadata_ = kwargs.get("metadata_", {})
        self.last_message_at = kwargs.get("last_message_at", AHORA)
        self.resolved_at = kwargs.get("resolved_at")
        self.created_at = kwargs.get("created_at", AHORA)
        self.updated_at = kwargs.get("updated_at", AHORA)


class FakeMessage:
    """Sustituto de Message con lo que lee el detalle de conversacion."""

    def __init__(self, **kwargs: Any) -> None:
        """Construye el mensaje con valores por defecto razonables."""
        self.id = kwargs.get("id") or uuid.uuid4()
        self.conversation_id = kwargs.get("conversation_id") or uuid.uuid4()
        self.metadata_ = kwargs.get("metadata_", {})
        self.direction = kwargs.get("direction", "inbound")
        self.message_type = kwargs.get("message_type", "text")
        self.content = kwargs.get("content", "hola")
        self.media_url = kwargs.get("media_url")
        self.sender_type = kwargs.get("sender_type", "contact")
        self.sender_id = kwargs.get("sender_id")
        self.created_at = kwargs.get("created_at", AHORA)


class FakeTag:
    """Sustituto de Tag."""

    def __init__(self, **kwargs: Any) -> None:
        """Construye la etiqueta con valores por defecto razonables."""
        self.id = kwargs.get("id") or uuid.uuid4()
        self.client_id = kwargs.get("client_id") or uuid.uuid4()
        self.name = kwargs.get("name", "vip")
        self.color = kwargs.get("color", "#FF5733")
        self.created_at = kwargs.get("created_at", AHORA)


class FakeNote:
    """Sustituto de InternalNote."""

    def __init__(self, **kwargs: Any) -> None:
        """Construye la nota con valores por defecto razonables."""
        self.id = kwargs.get("id") or uuid.uuid4()
        self.contact_id = kwargs.get("contact_id") or uuid.uuid4()
        self.author_id = kwargs.get("author_id") or uuid.uuid4()
        self.content = kwargs.get("content", "llamar el lunes")
        self.created_at = kwargs.get("created_at", AHORA)


class FakeUser:
    """Sustituto de User para la asignacion de conversaciones."""

    def __init__(self, **kwargs: Any) -> None:
        """Construye el usuario con valores por defecto razonables."""
        self.id = kwargs.get("id") or uuid.uuid4()
        self.client_id = kwargs.get("client_id") or uuid.uuid4()
        self.first_name = kwargs.get("first_name", "Grace")
        self.last_name = kwargs.get("last_name", "Hopper")
        self.is_active = kwargs.get("is_active", True)


class FakeAgentLog:
    """Sustituto de AgentActionLog (modelo de Dev A, todavia sin entregar)."""

    def __init__(self, **kwargs: Any) -> None:
        """Construye el registro con valores por defecto razonables."""
        self.id = kwargs.get("id") or uuid.uuid4()
        self.conversation_id = kwargs.get("conversation_id") or uuid.uuid4()
        self.message_id = kwargs.get("message_id")
        self.node_name = kwargs.get("node_name", "rag_query")
        self.action_type = kwargs.get("action_type", "query")
        self.input_summary = kwargs.get("input_summary")
        self.output_summary = kwargs.get("output_summary")
        self.details = kwargs.get("details", {})
        self.duration_ms = kwargs.get("duration_ms", 120)
        self.tokens_used = kwargs.get("tokens_used", 0)
        self.model_used = kwargs.get("model_used")
        self.status = kwargs.get("status", "success")
        self.error_message = kwargs.get("error_message")
        self.created_at = kwargs.get("created_at", AHORA)
