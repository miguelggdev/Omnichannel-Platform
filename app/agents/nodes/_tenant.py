"""Configuracion por tenant y por canal que consumen los nodos del grafo.

Tres cosas viven aqui porque las necesitan varios nodos y ninguna es del dominio
de uno solo:

1. `get_agent_settings()` — la fila de `agent_configs` del tenant, ya traducida a
   un dataclass con defaults. El grafo se instancia una vez y se reutiliza
   (spec §12), asi que la configuracion se lee en cada invocacion, nunca al
   construir el grafo: cambiarla no exige reiniciar el worker.
2. `get_contact_identifier()` — el telefono/PSID al que responder en ese canal.
3. `get_channel_config()` — credenciales del canal para el `MessagingProvider`.

Desviaciones sobre `specs/sprint-06-langgraph.md` (el spec describe columnas que
el modelo de Sprint 1 no tiene):

- No hay `agent_configs.agent_type` ni `is_enabled` ni `settings`. Hay UNA fila
  de configuracion por tenant (`is_active`), con `model`, `temperature`,
  `system_prompt`, `training_mode`, `similarity_threshold` y un JSONB `config`.
  Los agentes habilitados salen de `config.enabled_agents` (default: `["rag"]`),
  y los parametros de retrieval de `config.rag_threshold` / `config.rag_top_k`.
- `agent_configs.model` cumple el papel de `model_name` de la spec.

`tenant_session` se importa al nivel de modulo (no dentro de las funciones) para
que los tests puedan sustituirlo con `monkeypatch.setattr`, igual que en
`app/services/rag.py`.
"""

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import tenant_session
from app.models.agent_config import AgentConfig
from app.models.contact_identifier import ContactIdentifier
from app.services.rag import DEFAULT_THRESHOLD, DEFAULT_TOP_K, FEW_SHOT_THRESHOLD

logger = logging.getLogger(__name__)

# Agentes habilitados cuando el tenant no configura `config.enabled_agents`.
# Solo RAG: `scheduling` llega en Sprint 7 y no debe enrutarse hasta entonces.
DEFAULT_ENABLED_AGENTS: tuple[str, ...] = ("rag",)

# Temperatura de generacion por defecto si el tenant no tiene fila de config.
DEFAULT_TEMPERATURE = 0.3

# Proveedor de mensajeria que atiende cada canal. Mientras no exista la tabla
# `channel_configs` (Fase 2), este mapa y el `.env` son la unica fuente: mismo
# criterio que `_resolve_client_id()` en `app/tasks/webhook_processor.py`.
CHANNEL_PROVIDERS: dict[str, str] = {
    "whatsapp": "ycloud",
    "instagram": "meta",
    "facebook": "meta",
}


class ChannelNotConfiguredError(RuntimeError):
    """El canal no tiene proveedor asociado o le faltan credenciales."""


class ContactIdentifierNotFoundError(RuntimeError):
    """El contacto no tiene identificador registrado en ese canal.

    Sin el no hay a donde responder. El mensaje entrante siempre crea el
    identifier (`webhook_processor._resolve_contact`), asi que llegar aqui
    significa datos inconsistentes, no un caso normal.
    """


@dataclass(frozen=True)
class AgentSettings:
    """Configuracion efectiva del agente de un tenant.

    Attributes:
        name: Nombre del agente configurado.
        model: Modelo de chat del tenant.
        temperature: Temperatura de generacion.
        system_prompt: Prompt de sistema del tenant, si definio uno.
        welcome_message: Mensaje de bienvenida configurado.
        handoff_message: Mensaje de transferencia configurado.
        training_mode: Si el modo entrenamiento (ADR-005) esta activo.
        few_shot_threshold: Similaridad minima para inyectar un few-shot.
        rag_threshold: Similaridad minima de los chunks de contexto.
        rag_top_k: Maximo de chunks a recuperar.
        enabled_agents: Agentes habilitados del tenant.
    """

    name: str = "asistente"
    model: str = ""
    temperature: float = DEFAULT_TEMPERATURE
    system_prompt: str | None = None
    welcome_message: str | None = None
    handoff_message: str | None = None
    training_mode: bool = False
    few_shot_threshold: float = FEW_SHOT_THRESHOLD
    rag_threshold: float = DEFAULT_THRESHOLD
    rag_top_k: int = DEFAULT_TOP_K
    enabled_agents: tuple[str, ...] = DEFAULT_ENABLED_AGENTS


def _as_float(valor: Any, default: float) -> float:
    """Convierte un valor del JSONB a float, o devuelve el default.

    Args:
        valor: Valor crudo leido del JSONB.
        default: Valor a usar si el crudo no es convertible.

    Returns:
        El valor convertido, o `default`.
    """
    try:
        return float(valor)
    except (TypeError, ValueError):
        return default


def _as_int(valor: Any, default: int) -> int:
    """Convierte un valor del JSONB a int, o devuelve el default.

    Args:
        valor: Valor crudo leido del JSONB.
        default: Valor a usar si el crudo no es convertible.

    Returns:
        El valor convertido, o `default`.
    """
    try:
        return int(valor)
    except (TypeError, ValueError):
        return default


def _as_agents(valor: Any) -> tuple[str, ...]:
    """Normaliza `config.enabled_agents` a una tupla de nombres de agente.

    Una lista vacia es una configuracion valida: un tenant puede deshabilitar
    todos los agentes automaticos a proposito, para que todo mensaje termine en
    un humano. Por eso una lista vacia se respeta tal cual, distinto de
    `enabled_agents` ausente (o de un tipo que no es lista), que si cae al
    default.

    Args:
        valor: Valor crudo de `config.enabled_agents`.

    Returns:
        Los agentes declarados (puede ser una tupla vacia), o
        `DEFAULT_ENABLED_AGENTS` si el tenant no configuro `enabled_agents` en
        absoluto.
    """
    if isinstance(valor, list | tuple):
        return tuple(str(item) for item in valor if isinstance(item, str))
    return DEFAULT_ENABLED_AGENTS


async def get_agent_settings(client_id: UUID) -> AgentSettings:
    """Lee la configuracion activa del agente del tenant.

    Args:
        client_id: Tenant del que se quiere la configuracion.

    Returns:
        Configuracion efectiva. Si el tenant no tiene fila activa en
        `agent_configs`, devuelve los defaults (modelo de `OPENAI_CHAT_MODEL`,
        solo el agente RAG habilitado, sin modo entrenamiento).
    """
    default_model = get_settings().OPENAI_CHAT_MODEL

    async with tenant_session(client_id) as session:
        stmt = (
            select(AgentConfig)
            .where(AgentConfig.is_active.is_(True))
            .order_by(AgentConfig.created_at.asc())
            .limit(1)
        )
        config = (await session.execute(stmt)).scalar_one_or_none()

        if config is None:
            logger.info("Tenant %s sin agent_config activo; se usan los defaults", client_id)
            return AgentSettings(model=default_model)

        extra: dict[str, Any] = config.config or {}
        return AgentSettings(
            name=config.name,
            model=config.model or default_model,
            temperature=float(config.temperature),
            system_prompt=config.system_prompt,
            welcome_message=config.welcome_message,
            handoff_message=config.handoff_message,
            training_mode=bool(config.training_mode),
            few_shot_threshold=float(config.similarity_threshold),
            rag_threshold=_as_float(extra.get("rag_threshold"), DEFAULT_THRESHOLD),
            rag_top_k=_as_int(extra.get("rag_top_k"), DEFAULT_TOP_K),
            enabled_agents=_as_agents(extra.get("enabled_agents")),
        )


async def get_contact_identifier(client_id: UUID, contact_id: UUID, channel: str) -> str:
    """Obtiene el identificador del contacto para un canal.

    Args:
        client_id: Tenant propietario.
        contact_id: Contacto al que se quiere responder.
        channel: Canal por el que responder.

    Returns:
        Telefono E.164 sin `+` (whatsapp) o PSID (instagram/facebook).

    Raises:
        ContactIdentifierNotFoundError: Si el contacto no tiene identificador en
            ese canal.
    """
    async with tenant_session(client_id) as session:
        stmt = select(ContactIdentifier.identifier_value).where(
            ContactIdentifier.contact_id == contact_id,
            ContactIdentifier.channel == channel,
        )
        identifier = (await session.execute(stmt)).scalar_one_or_none()

    if not identifier:
        raise ContactIdentifierNotFoundError(
            f"El contacto {contact_id} no tiene identificador en el canal {channel}"
        )
    return str(identifier)


def get_channel_config(channel: str) -> tuple[str, dict[str, Any]]:
    """Resuelve el proveedor y las credenciales de un canal.

    Args:
        channel: Canal de la conversacion (whatsapp, instagram, facebook).

    Returns:
        Tupla `(provider_name, channel_config)` lista para la factory de
        `MessagingProvider` y para `send_message()`.

    Raises:
        ChannelNotConfiguredError: Si el canal no tiene proveedor registrado o le
            faltan credenciales en el entorno.
    """
    provider_name = CHANNEL_PROVIDERS.get(channel)
    if provider_name is None:
        raise ChannelNotConfiguredError(
            f"Canal {channel!r} sin proveedor registrado. Disponibles: {sorted(CHANNEL_PROVIDERS)}"
        )

    settings = get_settings()
    if provider_name == "ycloud":
        config: dict[str, Any] = {
            "api_key": settings.YCLOUD_API_KEY,
            "phone_number_id": settings.YCLOUD_PHONE_NUMBER_ID,
        }
    else:
        config = {
            "page_access_token": settings.META_PAGE_ACCESS_TOKEN,
            "channel": channel,
        }

    faltantes = [clave for clave, valor in config.items() if not valor]
    if faltantes:
        raise ChannelNotConfiguredError(
            f"Faltan credenciales del canal {channel!r} ({provider_name}): {faltantes}"
        )

    return provider_name, config
