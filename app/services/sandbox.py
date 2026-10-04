"""Sandbox por tenant: configuracion de prueba, publicacion atomica y rollback (ADR-078).

Contrato: `specs/sprint-14-sandbox-i18n.md` §1-2. El sandbox de un tenant es **otro
tenant** (`clients.is_sandbox`), no una columna `environment` en cada tabla: asi las
politicas RLS —que solo miran `client_id`— y los ~30 lectores de `agent_configs`
quedan como estan. Lo que el spec resuelve con `SET LOCAL app.current_environment`
se resuelve aqui con el aislamiento por tenant que ya existe.

**El truco que hace posible la publicacion atomica:** una sola transaccion puede
cambiar de tenant a mitad (`set_config('app.current_client_id', ..., true)` es
local a la transaccion). Se lee el sandbox, se cambia a produccion y se escribe; RLS
se cumple en cada paso y no hace falta ningun rol que la salte. Si algo falla, no
cambio nada: ni en produccion ni en el historial.

Que se copia al sandbox: `agent_configs`, `quick_replies`, `documents` y
`document_chunks` (con sus embeddings, para probar RAG de verdad). Los archivos de
Storage **no** se copian: `documents.file_url` apunta a los de produccion, y como el
sandbox no tiene usuarios ni expone borrado de documentos, nadie puede tocarlos
desde ahi. No se copian `service_types` ni `tags`: el agendamiento del sandbox
responde sin catalogo de servicios.

**Que NO viaja nunca entre los dos:** las claves de `agent_configs.config` que son del
tenant y no de la configuracion (`CLAVES_DEL_TENANT`). `clinical` y `marketing`
guardan ids de contactos de *ese* tenant; publicar los del sandbox dejaria a
produccion sin profesionales autorizados. `feature_flags` se gobierna por su propia
API. Publicar y revertir conservan siempre las de produccion.

Que se publica: `agent_configs` y `quick_replies`. Los documentos del sandbox son una
copia para probar, no una vía de edicion de la base de conocimiento.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import tenant_session
from app.models.agent_config import AgentConfig
from app.models.base import Base
from app.models.client import Client
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.message import Message
from app.models.quick_reply import QuickReply
from app.models.sandbox import ConfigHistory, TenantSandbox
from app.models.token_budget import TokenBudget

logger = logging.getLogger(__name__)

#: Claves de `agent_configs.config` propias del tenant: no viajan entre sandbox y produccion.
CLAVES_DEL_TENANT: tuple[str, ...] = ("clinical", "marketing", "feature_flags")

#: De esas, las que NO se llevan al crear el sandbox (ids de contactos del tenant real).
#: `feature_flags` si se copia: el sandbox se comporta como produccion.
_CLAVES_QUE_NO_SE_CLONAN: tuple[str, ...] = ("clinical", "marketing")

_CAMPOS_AGENTE: tuple[str, ...] = (
    "name",
    "system_prompt",
    "welcome_message",
    "model",
    "temperature",
    "max_tokens",
    "training_mode",
    "similarity_threshold",
    "handoff_message",
    "config",
    "is_active",
)
_CAMPOS_RESPUESTA: tuple[str, ...] = ("shortcut", "title", "content", "category", "created_by")

#: Campos de `agent_configs` que `actualizar_config_sandbox()` deja cambiar.
CAMPOS_EDITABLES: frozenset[str] = frozenset(_CAMPOS_AGENTE)

#: De esos, los que admiten `null` (para borrar el texto); el resto son NOT NULL.
_CAMPOS_ANULABLES: frozenset[str] = frozenset(
    {"system_prompt", "welcome_message", "handoff_message"}
)

#: Canal de las conversaciones de prueba (`SandboxProvider`: no envia nada).
CANAL_DE_PRUEBA = "sandbox"
_ESTADO_CERRADO = "resolved"
_ESTADOS_DE_UN_HUMANO = ("human_active", "waiting_human")

#: Tablas que una conversacion de prueba puede llenar, hijas antes que padres. Las
#: que se clonan (`agent_configs`, `quick_replies`, `documents`, `document_chunks`) se
#: rehacen aparte. **No se tocan `token_budgets` ni `token_usage_logs`**: el consumo
#: del mes tiene que sobrevivir al reset, o `POST /reset` en bucle saltaria el tope
#: `SANDBOX_TOKEN_BUDGET` y gastaria tokens del LLM sin limite. `audit_logs` queda:
#: es el rastro y su propio trigger lo reescribiria con cada borrado. Un test falla
#: si aparece una tabla nueva.
_TABLAS_DE_PRUEBA: tuple[str, ...] = (
    "agent_action_logs",
    "pending_responses",
    "approved_responses",
    "satisfaction_surveys",
    "internal_notes",
    "appointments",
    "invoices",
    "outgoing_webhook_logs",
    "messages",
    "contact_tags",
    "conversations",
    "contact_identifiers",
    "contacts",
    "webhook_dedup",
)
_TABLAS_CLONADAS: tuple[str, ...] = (
    "document_chunks",
    "documents",
    "quick_replies",
    "agent_configs",
)


class SandboxError(Exception):
    """Base de los errores previstos del sandbox."""


class SandboxYaExisteError(SandboxError):
    """El tenant ya tiene un sandbox (uno por tenant)."""


class SandboxNoExisteError(SandboxError):
    """El tenant no tiene sandbox todavia."""


class SandboxDeSandboxError(SandboxError):
    """Un sandbox no puede tener su propio sandbox."""


class SinConfiguracionError(SandboxError):
    """Falta el agente que publicar, o en el sandbox no hay ninguno."""


class VersionInexistenteError(SandboxError):
    """No hay una instantanea de esa version (o no hay ninguna) a la que volver."""


class CampoNoAnulableError(SandboxError):
    """Un cambio pone a `null` un campo que la base no admite vacio.

    Attributes:
        campos: Los campos que venian en `null`.
    """

    def __init__(self, campos: list[str]) -> None:
        """Guarda los campos que venian en `null`.

        Args:
            campos: Los campos obligatorios que venian vacios.
        """
        super().__init__(", ".join(campos))
        self.campos = campos


class ClaveDelTenantError(SandboxError):
    """El `config` intenta tocar claves propias del tenant.

    Attributes:
        claves: Las claves no permitidas que venian en el cambio.
    """

    def __init__(self, claves: list[str]) -> None:
        """Guarda las claves que no se pudieron aceptar.

        Args:
            claves: Las claves propias del tenant que venian en el `config`.
        """
        super().__init__(", ".join(claves))
        self.claves = claves


# ─── Utilidades de transaccion ───────────────────────────────────────────────


async def _en_tenant(session: AsyncSession, client_id: UUID) -> None:
    """Cambia, dentro de la misma transaccion, el tenant que ve RLS.

    Args:
        session: Sesion abierta con `tenant_session()`.
        client_id: Tenant al que se pasa a leer y escribir.
    """
    await session.execute(
        text("SELECT set_config('app.current_client_id', :cid, true)"), {"cid": str(client_id)}
    )


async def _bloquear(session: AsyncSession, client_id: UUID) -> None:
    """Serializa las operaciones del sandbox de un tenant (crear, reset, publicar...).

    Args:
        session: Sesion con la transaccion abierta; el candado se suelta al cerrarla.
        client_id: Tenant de produccion.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:clave))"), {"clave": f"sandbox:{client_id}"}
    )


def _a_json(valor: Any) -> Any:
    """Convierte UUID y fechas en lo que admite un JSONB.

    Args:
        valor: Un valor de una fila de configuracion.

    Returns:
        `str` para un UUID, ISO 8601 para una fecha, y el mismo valor en cualquier otro caso.
    """
    if isinstance(valor, UUID):
        return str(valor)
    if isinstance(valor, datetime):
        return valor.isoformat()
    return valor


def _mes_actual() -> str:
    """Mes en curso (`YYYY-MM`, UTC), igual que `token_budget.py`."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


# ─── Lectura y escritura de la configuracion ─────────────────────────────────


async def _leer_agentes(session: AsyncSession, client_id: UUID) -> list[dict[str, Any]]:
    """Filas de `agent_configs` del tenant en contexto, en su orden de creacion.

    Args:
        session: Sesion cuyo contexto RLS ya es `client_id`.
        client_id: Tenant en contexto.

    Returns:
        Una lista de diccionarios con `_CAMPOS_AGENTE` y `created_at`.
    """
    filas = (
        (
            await session.execute(
                select(AgentConfig)
                .where(AgentConfig.client_id == client_id)
                .order_by(AgentConfig.created_at.asc(), AgentConfig.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {**{campo: getattr(f, campo) for campo in _CAMPOS_AGENTE}, "created_at": f.created_at}
        for f in filas
    ]


async def _leer_respuestas(session: AsyncSession, client_id: UUID) -> list[dict[str, Any]]:
    """Filas de `quick_replies` del tenant en contexto.

    Args:
        session: Sesion cuyo contexto RLS ya es `client_id`.
        client_id: Tenant en contexto.

    Returns:
        Una lista de diccionarios con `_CAMPOS_RESPUESTA`, ordenada por atajo.
    """
    filas = (
        (
            await session.execute(
                select(QuickReply)
                .where(QuickReply.client_id == client_id)
                .order_by(QuickReply.shortcut.asc())
            )
        )
        .scalars()
        .all()
    )
    return [{campo: getattr(f, campo) for campo in _CAMPOS_RESPUESTA} for f in filas]


def _config_visible(config: dict[str, Any] | None) -> dict[str, Any]:
    """El `config` sin las claves propias del tenant, que no se editan en el sandbox.

    Asi lo que devuelve la API se puede reenviar tal cual en un `PUT` sin que este lo
    rechace (`feature_flags` se copia al sandbox para que se comporte como produccion,
    pero no es editable ahi).
    """
    return {k: v for k, v in (config or {}).items() if k not in CLAVES_DEL_TENANT}


def _agente_principal(agentes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """El agente que lee `get_agent_settings()`: el primero activo por antiguedad.

    Args:
        agentes: Filas devueltas por `_leer_agentes()`, ya en orden de creacion.

    Returns:
        El agente principal, o `None` si no hay ninguno activo.
    """
    return next((a for a in agentes if a["is_active"]), None)


def _config_publicable(
    origen: dict[str, Any] | None, destino: dict[str, Any] | None
) -> dict[str, Any]:
    """Mezcla un `config` de origen con las claves del tenant que ya tiene el destino.

    Args:
        origen: `config` que se quiere llevar (del sandbox, o de una instantanea).
        destino: `config` actual del agente de produccion, de donde salen las claves
            propias del tenant.

    Returns:
        El `config` de origen sin las `CLAVES_DEL_TENANT`, mas las que ya tenia el destino.
    """
    resultado = {k: v for k, v in (origen or {}).items() if k not in CLAVES_DEL_TENANT}
    for clave in CLAVES_DEL_TENANT:
        if destino and clave in destino:
            resultado[clave] = destino[clave]
    return resultado


async def _reemplazar_agentes(
    session: AsyncSession,
    client_id: UUID,
    filas: list[dict[str, Any]],
    config_del_tenant: dict[str, Any] | None,
) -> None:
    """Borra los agentes del tenant en contexto y escribe `filas` en su lugar.

    Nada referencia `agent_configs.id`, asi que un borrado y reinsercion es seguro.
    Se conserva `created_at` de cada fila: es lo que decide cual agente lee el grafo.
    """
    await session.execute(delete(AgentConfig).where(AgentConfig.client_id == client_id))
    for fila in filas:
        campos = {c: fila[c] for c in _CAMPOS_AGENTE}
        campos["config"] = _config_publicable(fila["config"], config_del_tenant)
        session.add(AgentConfig(client_id=client_id, created_at=fila["created_at"], **campos))
    await session.flush()


async def _reemplazar_respuestas(
    session: AsyncSession, client_id: UUID, filas: list[dict[str, Any]]
) -> None:
    """Borra las respuestas rapidas del tenant en contexto y escribe `filas` en su lugar.

    Args:
        session: Sesion cuyo contexto RLS ya es `client_id`.
        client_id: Tenant en contexto.
        filas: Respuestas rapidas a dejar, con `_CAMPOS_RESPUESTA`.
    """
    await session.execute(delete(QuickReply).where(QuickReply.client_id == client_id))
    for fila in filas:
        session.add(QuickReply(client_id=client_id, **{c: fila[c] for c in _CAMPOS_RESPUESTA}))
    await session.flush()


def _agentes_a_json(filas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Serializa filas de agente para guardarlas en un JSONB (UUID y fechas a texto).

    Args:
        filas: Filas devueltas por `_leer_agentes()`.

    Returns:
        Las mismas filas con valores admitidos por JSON.
    """
    return [{k: _a_json(v) for k, v in fila.items()} for fila in filas]


def _agentes_de_json(filas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reconstruye las filas de agente de una instantanea del historial.

    Args:
        filas: Lo que guardo `_agentes_a_json()`.

    Returns:
        Las filas con `created_at` otra vez como `datetime`.
    """
    return [{**f, "created_at": datetime.fromisoformat(f["created_at"])} for f in filas]


def _respuestas_de_json(filas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reconstruye las respuestas rapidas de una instantanea del historial.

    Args:
        filas: Lo que guardo `_guardar_instantanea()` para `quick_replies`.

    Returns:
        Las filas con `created_by` otra vez como `UUID` (o `None`).
    """
    return [
        {**f, "created_by": UUID(f["created_by"]) if f.get("created_by") else None} for f in filas
    ]


# ─── Copia de produccion al sandbox ──────────────────────────────────────────


async def _copiar_documentos(session: AsyncSession, origen: UUID, destino: UUID) -> None:
    """Copia `documents` y `document_chunks` (con embeddings) de un tenant a otro.

    Un documento por vez, cambiando de tenant entre la lectura y la escritura: la
    memoria no crece con el tamano de la base de conocimiento.
    """
    await _en_tenant(session, origen)
    ids = (
        (await session.execute(select(Document.id).where(Document.client_id == origen)))
        .scalars()
        .all()
    )
    for doc_id in ids:
        await _en_tenant(session, origen)
        doc = (await session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
        chunks = (
            (
                await session.execute(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == doc_id)
                    .order_by(DocumentChunk.chunk_index.asc())
                )
            )
            .scalars()
            .all()
        )
        datos_doc = {
            "title": doc.title,
            "file_url": doc.file_url,
            "file_type": doc.file_type,
            "file_size": doc.file_size,
            "content": doc.content,
            "chunk_count": doc.chunk_count,
            "status": doc.status,
            "metadata_": {**(doc.metadata_ or {}), "sandbox_copy_of": str(doc.id)},
        }
        datos_chunks = [
            {
                "chunk_index": c.chunk_index,
                "content": c.content,
                "embedding": c.embedding,
                "token_count": c.token_count,
                "metadata_": c.metadata_,
            }
            for c in chunks
        ]
        await _en_tenant(session, destino)
        nuevo = Document(client_id=destino, **datos_doc)
        session.add(nuevo)
        await session.flush()
        if datos_chunks:
            # Un solo INSERT con todos los chunks del documento, no uno por chunk.
            await session.execute(
                insert(DocumentChunk),
                [{"client_id": destino, "document_id": nuevo.id, **d} for d in datos_chunks],
            )
        session.expunge_all()


async def _copiar_produccion(
    session: AsyncSession, origen: UUID, destino: UUID, cliente: Client | None
) -> None:
    """Rehace en `destino` la configuracion y la base de conocimiento de `origen`.

    Deja la sesion en contexto de `destino`; el llamador vuelve a `origen` si lo necesita.

    Args:
        session: Sesion abierta con `tenant_session()`.
        origen: Tenant de produccion.
        destino: Tenant sandbox.
        cliente: La fila `clients` de produccion, para crear la del sandbox; `None`
            si el sandbox ya existe (reset).
    """
    await _en_tenant(session, origen)
    agentes = await _leer_agentes(session, origen)
    respuestas = await _leer_respuestas(session, origen)
    datos_cliente: dict[str, Any] | None = None
    if cliente is not None:
        datos_cliente = {
            "name": f"{cliente.name} (sandbox)"[:255],
            "slug": f"{cliente.slug}-sbx-{uuid4().hex[:8]}"[:100],
            "plan": cliente.plan,
            "settings": cliente.settings,
            "theme_config": cliente.theme_config,
            "lead_management_enabled": cliente.lead_management_enabled,
        }

    await _en_tenant(session, destino)
    if datos_cliente is not None:
        session.add(Client(id=destino, is_active=True, is_sandbox=True, **datos_cliente))
        await session.flush()
    for fila in agentes:
        campos = {c: fila[c] for c in _CAMPOS_AGENTE}
        campos["config"] = {
            k: v for k, v in (fila["config"] or {}).items() if k not in _CLAVES_QUE_NO_SE_CLONAN
        }
        session.add(AgentConfig(client_id=destino, created_at=fila["created_at"], **campos))
    for fila in respuestas:
        session.add(QuickReply(client_id=destino, **{c: fila[c] for c in _CAMPOS_RESPUESTA}))
    await _asegurar_presupuesto(session, destino)
    await session.flush()

    await _copiar_documentos(session, origen, destino)
    await _en_tenant(session, destino)


async def _asegurar_presupuesto(session: AsyncSession, sandbox_id: UUID) -> None:
    """Garantiza la fila de presupuesto del mes del sandbox, con tope `SANDBOX_TOKEN_BUDGET`.

    Solo crea la fila si falta: nunca la reinicia, para que el consumo del mes sobreviva
    a un reset (ver `_TABLAS_DE_PRUEBA`).

    Args:
        session: Sesion con el contexto RLS del sandbox.
        sandbox_id: Tenant sandbox.
    """
    existente = (
        await session.execute(
            select(TokenBudget.id).where(
                TokenBudget.client_id == sandbox_id, TokenBudget.month == _mes_actual()
            )
        )
    ).scalar_one_or_none()
    if existente is None:
        ajustes = get_settings()
        session.add(
            TokenBudget(
                client_id=sandbox_id,
                month=_mes_actual(),
                total_budget=ajustes.SANDBOX_TOKEN_BUDGET,
                model_default=ajustes.OPENAI_CHAT_MODEL,
            )
        )
        await session.flush()


# ─── Ciclo de vida ───────────────────────────────────────────────────────────


async def _sandbox_de(session: AsyncSession, client_id: UUID) -> TenantSandbox | None:
    """La fila `tenant_sandboxes` del tenant (contexto de produccion).

    Args:
        session: Sesion con el contexto RLS de `client_id`.
        client_id: Tenant de produccion.

    Returns:
        La fila, o `None` si el tenant no tiene sandbox.
    """
    return (
        await session.execute(select(TenantSandbox).where(TenantSandbox.client_id == client_id))
    ).scalar_one_or_none()


async def crear_sandbox(client_id: UUID, created_by: UUID | None = None) -> UUID:
    """Crea el sandbox del tenant: un tenant clonado con su configuracion y su KB.

    Todo en una transaccion: si algo falla no queda un tenant a medias.

    Args:
        client_id: Tenant de produccion.
        created_by: Usuario que lo crea.

    Returns:
        El `client_id` del sandbox.

    Raises:
        SandboxYaExisteError: Si el tenant ya tiene sandbox.
        SandboxDeSandboxError: Si el tenant es, a su vez, un sandbox.
    """
    sandbox_id = uuid4()
    async with tenant_session(client_id, created_by) as session:
        await _bloquear(session, client_id)
        if await _sandbox_de(session, client_id) is not None:
            raise SandboxYaExisteError(str(client_id))
        cliente = (await session.execute(select(Client).where(Client.id == client_id))).scalar_one()
        if cliente.is_sandbox:
            raise SandboxDeSandboxError(str(client_id))
        # `cliente` se lee ahora: `_copiar_produccion` vacia la identidad de la sesion.
        await _copiar_produccion(session, client_id, sandbox_id, cliente)
        await _en_tenant(session, client_id)
        session.add(
            TenantSandbox(client_id=client_id, sandbox_client_id=sandbox_id, created_by=created_by)
        )
        await session.flush()
    logger.info("Sandbox %s creado para el tenant %s", sandbox_id, client_id)
    return sandbox_id


async def obtener_estado(client_id: UUID) -> dict[str, Any]:
    """Estado del sandbox del tenant.

    Args:
        client_id: Tenant de produccion.

    Returns:
        `{"exists": False}`, o los datos del sandbox y cuantas versiones hay en el historial.
    """
    async with tenant_session(client_id) as session:
        sandbox = await _sandbox_de(session, client_id)
        if sandbox is None:
            return {"exists": False}
        versiones = (
            await session.execute(
                select(func.count(func.distinct(ConfigHistory.version))).where(
                    ConfigHistory.client_id == client_id
                )
            )
        ).scalar_one()
        return {
            "exists": True,
            "sandbox_client_id": sandbox.sandbox_client_id,
            "created_at": sandbox.created_at,
            "reset_at": sandbox.reset_at,
            "last_published_at": sandbox.last_published_at,
            "versions": int(versiones),
        }


async def _requerir_sandbox(session: AsyncSession, client_id: UUID) -> UUID:
    """El `client_id` del sandbox del tenant, o `SandboxNoExisteError`.

    Args:
        session: Sesion con el contexto RLS de `client_id`.
        client_id: Tenant de produccion.

    Returns:
        El `client_id` del tenant sandbox.

    Raises:
        SandboxNoExisteError: Si el tenant no tiene sandbox.
    """
    sandbox = await _sandbox_de(session, client_id)
    if sandbox is None:
        raise SandboxNoExisteError(str(client_id))
    return sandbox.sandbox_client_id


async def reiniciar_sandbox(client_id: UUID) -> None:
    """Rehace el sandbox desde produccion, descartando sus cambios y sus pruebas.

    Args:
        client_id: Tenant de produccion.

    Raises:
        SandboxNoExisteError: Si el tenant no tiene sandbox.
    """
    async with tenant_session(client_id) as session:
        await _bloquear(session, client_id)
        sandbox_id = await _requerir_sandbox(session, client_id)
        await _en_tenant(session, sandbox_id)
        conversaciones = [
            str(c)
            for c in (
                await session.execute(
                    select(Conversation.id).where(Conversation.client_id == sandbox_id)
                )
            )
            .scalars()
            .all()
        ]
        for nombre in (*_TABLAS_DE_PRUEBA, *_TABLAS_CLONADAS):
            # La tabla sale de los metadatos de SQLAlchemy, no de un texto SQL armado
            # a mano: no hay nada que inyectar, y un nombre que no existe falla con claridad.
            tabla = Base.metadata.tables[nombre]
            await session.execute(delete(tabla).where(tabla.c.client_id == sandbox_id))
        await _copiar_produccion(session, client_id, sandbox_id, None)
        await _en_tenant(session, client_id)
        await session.execute(
            update(TenantSandbox)
            .where(TenantSandbox.client_id == client_id)
            .values(reset_at=func.now())
        )
    await _purgar_checkpoints(sandbox_id, conversaciones)
    logger.info("Sandbox %s del tenant %s reiniciado", sandbox_id, client_id)


async def _purgar_checkpoints(sandbox_id: UUID, conversaciones: list[str]) -> None:
    """Borra los checkpoints de LangGraph de las conversaciones de prueba descartadas.

    Esas tablas no llevan `client_id` ni RLS, asi que el borrado del tenant no las
    alcanza. Todas las conversaciones van en una sola transaccion. Es limpieza: si
    falla se registra y el reset ya hecho se mantiene.

    Args:
        sandbox_id: Tenant sandbox.
        conversaciones: Conversaciones de prueba que se descartaron.
    """
    if not conversaciones:
        return
    from app.tasks.ai_processor import _PURGAR_CHECKPOINTS

    try:
        async with tenant_session(sandbox_id) as session:
            for conversacion in conversaciones:
                for sentencia in _PURGAR_CHECKPOINTS:
                    await session.execute(sentencia, {"thread": f"{sandbox_id}:{conversacion}"})
    except Exception:
        logger.exception("No se pudieron purgar los checkpoints del sandbox %s", sandbox_id)


# ─── Configuracion del sandbox ───────────────────────────────────────────────


async def leer_config_sandbox(client_id: UUID) -> dict[str, Any]:
    """El agente del sandbox, tal como lo leeria el grafo.

    Args:
        client_id: Tenant de produccion.

    Returns:
        Los campos editables del agente principal del sandbox.

    Raises:
        SandboxNoExisteError: Si el tenant no tiene sandbox.
        SinConfiguracionError: Si el sandbox no tiene ningun agente activo.
    """
    async with tenant_session(client_id) as session:
        sandbox_id = await _requerir_sandbox(session, client_id)
        await _en_tenant(session, sandbox_id)
        agente = _agente_principal(await _leer_agentes(session, sandbox_id))
    if agente is None:
        raise SinConfiguracionError("El sandbox no tiene un agente activo")
    visible = {k: v for k, v in agente.items() if k != "created_at"}
    visible["config"] = _config_visible(visible["config"])
    return visible


async def actualizar_config_sandbox(client_id: UUID, cambios: dict[str, Any]) -> dict[str, Any]:
    """Cambia campos del agente del sandbox; `config` se mezcla, no se reemplaza.

    Args:
        client_id: Tenant de produccion.
        cambios: Campos de `CAMPOS_EDITABLES` con su nuevo valor.

    Returns:
        El agente del sandbox ya actualizado.

    Raises:
        SandboxNoExisteError: Si el tenant no tiene sandbox.
        SinConfiguracionError: Si el sandbox no tiene ningun agente activo.
        ClaveDelTenantError: Si `config` trae claves propias del tenant.
        CampoNoAnulableError: Si algun campo obligatorio viene en `null`.
    """
    nulos = sorted(
        c
        for c, valor in cambios.items()
        if c in CAMPOS_EDITABLES and c != "config" and valor is None and c not in _CAMPOS_ANULABLES
    )
    if nulos:
        raise CampoNoAnulableError(nulos)

    nuevo_config = cambios.get("config")
    if nuevo_config is not None:
        prohibidas = sorted(k for k in nuevo_config if k in CLAVES_DEL_TENANT)
        if prohibidas:
            raise ClaveDelTenantError(prohibidas)

    async with tenant_session(client_id) as session:
        sandbox_id = await _requerir_sandbox(session, client_id)
        await _en_tenant(session, sandbox_id)
        agente = (
            await session.execute(
                select(AgentConfig)
                .where(AgentConfig.client_id == sandbox_id, AgentConfig.is_active.is_(True))
                .order_by(AgentConfig.created_at.asc(), AgentConfig.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if agente is None:
            raise SinConfiguracionError("El sandbox no tiene un agente activo")
        for campo, valor in cambios.items():
            if campo not in CAMPOS_EDITABLES:
                continue
            if campo == "config":
                valor = {**(agente.config or {}), **(valor or {})}
            setattr(agente, campo, valor)
        await session.flush()
        actualizado = {c: getattr(agente, c) for c in _CAMPOS_AGENTE}
        actualizado["config"] = _config_visible(actualizado["config"])
        return actualizado


# ─── Publicacion y rollback ──────────────────────────────────────────────────


async def _siguiente_version(session: AsyncSession, client_id: UUID) -> int:
    """Numero de la proxima version del historial del tenant (bajo el candado del sandbox).

    Args:
        session: Sesion con el contexto RLS de `client_id`.
        client_id: Tenant de produccion.

    Returns:
        La version mas alta guardada mas uno (1 si no hay historial).
    """
    maximo = (
        await session.execute(
            select(func.max(ConfigHistory.version)).where(ConfigHistory.client_id == client_id)
        )
    ).scalar_one()
    return int(maximo or 0) + 1


async def _guardar_instantanea(
    session: AsyncSession,
    client_id: UUID,
    reason: str,
    created_by: UUID | None,
    agentes: list[dict[str, Any]],
    respuestas: list[dict[str, Any]],
) -> int:
    """Guarda la configuracion actual del tenant en `config_history` y devuelve su version.

    Args:
        session: Sesion con el contexto RLS de `client_id`.
        client_id: Tenant de produccion.
        reason: `publish` o `rollback`.
        created_by: Usuario que publica o revierte.
        agentes: Filas de `agent_configs` tal como estan.
        respuestas: Filas de `quick_replies` tal como estan.

    Returns:
        El numero de version de la instantanea.
    """
    version = await _siguiente_version(session, client_id)
    session.add(
        ConfigHistory(
            client_id=client_id,
            config_type="agent_configs",
            version=version,
            reason=reason,
            config_data=_agentes_a_json(agentes),
            created_by=created_by,
        )
    )
    session.add(
        ConfigHistory(
            client_id=client_id,
            config_type="quick_replies",
            version=version,
            reason=reason,
            config_data=[{k: _a_json(v) for k, v in r.items()} for r in respuestas],
            created_by=created_by,
        )
    )
    await session.flush()
    return version


async def publicar(client_id: UUID, user_id: UUID | None = None) -> int:
    """Publica la configuracion del sandbox a produccion en una sola transaccion.

    Antes de tocar nada guarda la configuracion de produccion en `config_history`,
    para poder volver con `revertir()`. Si cualquier paso falla, no cambia nada.
    Las claves propias del tenant (`CLAVES_DEL_TENANT`) se conservan.

    Args:
        client_id: Tenant de produccion.
        user_id: Usuario que publica.

    Returns:
        El numero de version de la instantanea de produccion que se guardo.

    Raises:
        SandboxNoExisteError: Si el tenant no tiene sandbox.
        SinConfiguracionError: Si el sandbox no tiene agentes: publicarlo dejaria a
            produccion sin configuracion.
    """
    async with tenant_session(client_id, user_id) as session:
        await _bloquear(session, client_id)
        sandbox_id = await _requerir_sandbox(session, client_id)

        await _en_tenant(session, sandbox_id)
        agentes_sandbox = await _leer_agentes(session, sandbox_id)
        respuestas_sandbox = await _leer_respuestas(session, sandbox_id)
        if _agente_principal(agentes_sandbox) is None:
            raise SinConfiguracionError("El sandbox no tiene un agente activo que publicar")

        await _en_tenant(session, client_id)
        agentes_prod = await _leer_agentes(session, client_id)
        respuestas_prod = await _leer_respuestas(session, client_id)
        principal = _agente_principal(agentes_prod)

        version = await _guardar_instantanea(
            session, client_id, "publish", user_id, agentes_prod, respuestas_prod
        )
        await _reemplazar_agentes(
            session, client_id, agentes_sandbox, principal["config"] if principal else None
        )
        await _reemplazar_respuestas(session, client_id, respuestas_sandbox)
        await session.execute(
            update(TenantSandbox)
            .where(TenantSandbox.client_id == client_id)
            .values(last_published_at=func.now())
        )
    logger.info("Tenant %s publico su sandbox (instantanea v%s)", client_id, version)
    return version


async def listar_historial(client_id: UUID, limite: int = 50) -> list[dict[str, Any]]:
    """Versiones guardadas en el historial, la mas reciente primero.

    Args:
        client_id: Tenant de produccion.
        limite: Maximo de versiones a devolver.

    Returns:
        `[{version, reason, created_at, created_by}]`.
    """
    async with tenant_session(client_id) as session:
        filas = (
            await session.execute(
                select(
                    ConfigHistory.version,
                    ConfigHistory.reason,
                    ConfigHistory.created_at,
                    ConfigHistory.created_by,
                )
                .where(
                    ConfigHistory.client_id == client_id,
                    ConfigHistory.config_type == "agent_configs",
                )
                .order_by(ConfigHistory.version.desc())
                .limit(limite)
            )
        ).all()
    return [
        {
            "version": f.version,
            "reason": f.reason,
            "created_at": f.created_at,
            "created_by": f.created_by,
        }
        for f in filas
    ]


async def revertir(
    client_id: UUID, user_id: UUID | None = None, version: int | None = None
) -> dict[str, int]:
    """Devuelve la configuracion de produccion a una instantanea del historial.

    Antes de restaurar guarda la configuracion actual como una version nueva
    (`reason='rollback'`): volver atras tambien se puede deshacer. Las claves
    propias del tenant se conservan: un rollback no revierte, por ejemplo, la
    lista de profesionales clinicos que se cambio despues.

    Args:
        client_id: Tenant de produccion.
        user_id: Usuario que revierte.
        version: Version a restaurar; por defecto la ultima.

    Returns:
        `{"restored": <version restaurada>, "saved_as": <version de la copia de lo que habia>}`.

    Raises:
        VersionInexistenteError: Si no hay historial, o esa version no existe.
    """
    async with tenant_session(client_id, user_id) as session:
        await _bloquear(session, client_id)
        if version is None:
            version = (
                await session.execute(
                    select(func.max(ConfigHistory.version)).where(
                        ConfigHistory.client_id == client_id
                    )
                )
            ).scalar_one()
        filas = {}
        if version is not None:
            filas = {
                h.config_type: h.config_data
                for h in (
                    await session.execute(
                        select(ConfigHistory).where(
                            ConfigHistory.client_id == client_id,
                            ConfigHistory.version == version,
                        )
                    )
                )
                .scalars()
                .all()
            }
        if "agent_configs" not in filas:
            raise VersionInexistenteError(str(version))

        agentes_prod = await _leer_agentes(session, client_id)
        respuestas_prod = await _leer_respuestas(session, client_id)
        principal = _agente_principal(agentes_prod)
        guardada = await _guardar_instantanea(
            session, client_id, "rollback", user_id, agentes_prod, respuestas_prod
        )
        await _reemplazar_agentes(
            session,
            client_id,
            _agentes_de_json(filas["agent_configs"]),
            principal["config"] if principal else None,
        )
        await _reemplazar_respuestas(
            session, client_id, _respuestas_de_json(filas.get("quick_replies", []))
        )
    logger.info("Tenant %s revirtio a la version %s (guardo v%s)", client_id, version, guardada)
    return {"restored": int(version), "saved_as": guardada}


# ─── Mensajes de prueba ──────────────────────────────────────────────────────


async def enviar_mensaje_de_prueba(
    client_id: UUID, texto: str, user_id: UUID, nueva_conversacion: bool = False
) -> dict[str, Any]:
    """Manda un mensaje al agente del sandbox y devuelve su respuesta.

    Recorre el grafo real (con el LLM de verdad) en el tenant sandbox, por el canal
    `sandbox`, que no envia nada fuera. Una conversacion que quedo en manos de un
    humano (handoff) se cierra y se abre una nueva: en el sandbox no hay a quien
    atenderla y el bot no volveria a contestar.

    Args:
        client_id: Tenant de produccion.
        texto: Mensaje de prueba.
        user_id: Administrador que prueba (cada uno tiene su propio contacto).
        nueva_conversacion: Empezar una conversacion limpia.

    Returns:
        `{conversation_id, response, intent, requires_handoff, handoff_reason, detected_language}`.

    Raises:
        SandboxNoExisteError: Si el tenant no tiene sandbox.
    """
    # Importes locales: `webhook_processor` y `ai_processor` traen el grafo entero.
    from app.tasks.ai_processor import _invoke_graph
    from app.tasks.webhook_processor import _find_or_create_contact, _resolve_conversation

    async with tenant_session(client_id) as session:
        sandbox_id = await _requerir_sandbox(session, client_id)

    external_id = f"{CANAL_DE_PRUEBA}:{uuid4().hex}"
    async with tenant_session(sandbox_id) as session:
        await _asegurar_presupuesto(session, sandbox_id)
        contacto, _ = await _find_or_create_contact(
            session, sandbox_id, CANAL_DE_PRUEBA, f"tester-{user_id}", "Probador del sandbox"
        )
        conversacion, _ = await _resolve_conversation(
            session, sandbox_id, contacto.id, CANAL_DE_PRUEBA
        )
        if nueva_conversacion or conversacion.status in _ESTADOS_DE_UN_HUMANO:
            conversacion.status = _ESTADO_CERRADO
            await session.flush()
            conversacion, _ = await _resolve_conversation(
                session, sandbox_id, contacto.id, CANAL_DE_PRUEBA
            )
        session.add(
            Message(
                id=uuid4(),
                client_id=sandbox_id,
                conversation_id=conversacion.id,
                direction="inbound",
                message_type="text",
                content=texto,
                external_message_id=external_id,
                sender_type="contact",
                sender_id=contacto.id,
                metadata_={},
            )
        )
        conversacion.last_message_at = datetime.now(timezone.utc)
        conversation_id, contact_id = conversacion.id, contacto.id

    estado = await _invoke_graph(
        client_id=str(sandbox_id),
        conversation_id=str(conversation_id),
        contact_id=str(contact_id),
        channel=CANAL_DE_PRUEBA,
        message_data={
            "text": texto,
            "external_message_id": external_id,
            "channel": CANAL_DE_PRUEBA,
            "sender_identifier": f"tester-{user_id}",
        },
    )
    return {
        "conversation_id": conversation_id,
        "response": estado.get("response_text"),
        "intent": estado.get("intent"),
        "requires_handoff": bool(estado.get("requires_handoff")),
        "handoff_reason": estado.get("handoff_reason"),
        "detected_language": estado.get("detected_language"),
    }
