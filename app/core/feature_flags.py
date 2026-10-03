"""Feature flags por tenant (Sprint 14, ADR-076).

Contrato: `specs/sprint-14-sandbox-i18n.md` §5-7. Desviaciones sobre el spec,
todas porque el spec describe un esquema que el proyecto no tiene:

- **Las flags viven en `agent_configs.config.feature_flags`**, no en
  `agent_configs.settings` (esa columna no existe: hay una fila por tenant con
  un JSONB `config`, ver `app/agents/nodes/_tenant.py`). No hace falta migracion.
- **Los agentes ya tenian un interruptor por tenant: `config.enabled_agents`.**
  Una flag `enable_<agente>` no lo reemplaza, lo *restringe*: el agente llega
  al router y a su nodo solo si esta en `enabled_agents` **y** su flag no lo
  apaga. Sin flag, manda `enabled_agents` (los tenants existentes no cambian).
  El filtro se aplica en `get_agent_settings()`, que ya lee ese JSONB en cada
  invocacion, asi que el router y los nueve nodos que la usan ven lo mismo y
  no se anade una ida a Redis al camino caliente del grafo.
- **Las flags de agente son booleanas.** El porcentaje por entidad
  (`is_enabled_percentage`) necesita un `entity_id` (p. ej. el contacto) que
  `get_agent_settings()` no recibe; un agente a medias entre el router y su
  nodo seria incoherente. Los porcentajes (0-100) son para el resto de flags.
- **Solo las flags de agente y `enable_sandbox` se aplican hoy** (esta ultima la
  exige `app/api/v1/sandbox.py`, Sprint 14c). `enable_voice`, `enable_sentiment`,
  `enable_csat`, `enable_outgoing_webhooks` y `enable_reranking`
  se pueden guardar y consultar, pero ninguna feature las lee todavia: cada una
  ya tiene su propio mecanismo y engancharlas es un cambio de comportamiento que
  el spec no pide. La API lo dice (`enforced`).
- `sha256` y no `md5` en el hash del rollout (bandit B324); la distribucion es la
  misma.

Cache: la API de `FeatureFlags` guarda las flags del tenant en Redis
(`ff:{client_id}`, TTL 5 minutos) y se invalida al escribir. Redis es una
optimizacion, no una dependencia: si falla, se lee de la base.
"""

import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select, text

from app.core.database import tenant_session
from app.models.agent_config import AgentConfig

logger = logging.getLogger(__name__)

#: Valor de una flag: booleano, o porcentaje de rollout de 0 a 100.
FlagValue = bool | int

#: Flags de agente: `enable_<flag>` restringe el agente `<valor>` de `enabled_agents`.
AGENT_FLAGS: dict[str, str] = {
    "enable_scheduling": "scheduling",
    "enable_financial": "financial",
    "enable_marketing": "marketing",
    "enable_clinical": "clinical",
}

#: Flags que no son de agente pero si se aplican: `enable_sandbox` la exige la API del sandbox.
GATE_FLAGS: tuple[str, ...] = ("enable_sandbox",)

#: Flags declaradas que ninguna feature lee todavia (ver el docstring del modulo).
UNENFORCED_FLAGS: tuple[str, ...] = (
    "enable_voice",
    "enable_sentiment",
    "enable_csat",
    "enable_outgoing_webhooks",
    "enable_reranking",
)

#: Todas las flags que acepta la API, en el orden en que se listan.
KNOWN_FLAGS: tuple[str, ...] = (*AGENT_FLAGS, *GATE_FLAGS, *UNENFORCED_FLAGS)

CACHE_TTL = 300
_CONFIG_KEY = "feature_flags"

#: Mezcla el parche dentro de `config.feature_flags` sin tocar el resto de `config`.
_ACTUALIZAR_FLAGS = text(
    """
    UPDATE agent_configs
    SET config = COALESCE(config, '{}'::jsonb) || jsonb_build_object(
        'feature_flags',
        COALESCE(config -> 'feature_flags', '{}'::jsonb) || CAST(:parche AS jsonb)
    )
    WHERE id = :id AND client_id = :client_id
    """
)


class SinAgenteConfigurableError(Exception):
    """El tenant no tiene una fila activa de `agent_configs` donde guardar la flag."""


def es_flag_de_agente(flag: str) -> bool:
    """Si la flag restringe un agente (y por tanto solo admite un booleano).

    Args:
        flag: Nombre de la flag.

    Returns:
        `True` para las flags de `AGENT_FLAGS`.
    """
    return flag in AGENT_FLAGS


def validar_valor(flag: str, valor: object) -> FlagValue:
    """Comprueba que `valor` sea admisible para `flag`.

    Args:
        flag: Nombre de una flag de `KNOWN_FLAGS`.
        valor: Valor propuesto.

    Returns:
        El mismo valor, ya tipado.

    Raises:
        ValueError: Si la flag es de agente y el valor no es booleano, o si es
            un porcentaje fuera de 0-100 o de otro tipo.
    """
    if isinstance(valor, bool):
        return valor
    if es_flag_de_agente(flag):
        raise ValueError(f"{flag} es una flag de agente: solo admite true o false")
    if isinstance(valor, int) and 0 <= valor <= 100:
        return valor
    raise ValueError(f"{flag} admite true, false o un porcentaje entero de 0 a 100")


def flags_del_tenant(config: Mapping[str, Any] | None) -> dict[str, FlagValue]:
    """Extrae las flags de un JSONB `agent_configs.config`, descartando basura.

    El JSONB lo puede haber escrito alguien a mano: solo se conservan las
    entradas con nombre de texto y valor booleano o entero.

    Args:
        config: El JSONB `config` del tenant, o `None`.

    Returns:
        Las flags validas; vacio si no hay ninguna.
    """
    crudo = (config or {}).get(_CONFIG_KEY)
    if not isinstance(crudo, Mapping):
        return {}
    return {
        nombre: valor
        for nombre, valor in crudo.items()
        if isinstance(nombre, str) and isinstance(valor, bool | int)
    }


def agentes_habilitados(
    enabled_agents: tuple[str, ...], flags: Mapping[str, FlagValue]
) -> tuple[str, ...]:
    """Quita de `enabled_agents` los agentes que una flag apaga.

    Sin flag, el agente sigue como lo dejo `enabled_agents`. Con flag, solo
    pasa si vale exactamente `true`: un valor que no sea booleano (algo escrito
    a mano en el JSONB) apaga el agente, que es el lado seguro. `rag` y el
    traspaso a un humano no tienen flag.

    Args:
        enabled_agents: Agentes que el tenant declaro en `config.enabled_agents`.
        flags: Flags del tenant, ver `flags_del_tenant()`.

    Returns:
        Los agentes que siguen disponibles, en el mismo orden.
    """
    apagados = {
        agente for flag, agente in AGENT_FLAGS.items() if flag in flags and flags[flag] is not True
    }
    return tuple(agente for agente in enabled_agents if agente not in apagados)


def en_rollout(client_id: UUID | str, flag: str, entity_id: str, porcentaje: int) -> bool:
    """Si una entidad cae dentro del `porcentaje` de un rollout gradual.

    El hash es determinista: la misma entidad obtiene siempre el mismo
    resultado para la misma flag y tenant, y subir el porcentaje solo suma
    entidades, nunca saca a las que ya estaban.

    Args:
        client_id: Tenant.
        flag: Nombre de la flag.
        entity_id: Entidad a evaluar (contacto, conversacion...).
        porcentaje: De 0 a 100.

    Returns:
        `True` si la entidad esta dentro del porcentaje.
    """
    digest = hashlib.sha256(f"{client_id}:{flag}:{entity_id}".encode()).hexdigest()
    return int(digest[:8], 16) % 100 < porcentaje


def _como_porcentaje(valor: FlagValue | None) -> int:
    """Traduce el valor de una flag a un porcentaje (`true`=100, `false`/ausente=0).

    Args:
        valor: El valor guardado de la flag, o `None` si no esta configurada.

    Returns:
        Un entero de 0 a 100.
    """
    if valor is None or valor is False:
        return 0
    if valor is True:
        return 100
    return valor


class FeatureFlags:
    """Lectura y escritura de las flags de un tenant, con cache en Redis.

    Attributes:
        redis: Cliente Redis. `None` usa el compartido de `app.services.dedup`
            (el que `run_isolated()` limpia entre tareas de Celery).
    """

    def __init__(self, redis: Any | None = None) -> None:
        """Prepara el servicio.

        Args:
            redis: Cliente Redis con `decode_responses=True`; `None` para el del servicio.
        """
        self._redis = redis

    def _cliente(self) -> Any:
        """Devuelve el cliente Redis, creando el compartido la primera vez."""
        if self._redis is not None:
            return self._redis
        from app.services.dedup import get_redis

        return get_redis()

    @staticmethod
    def _clave(client_id: UUID | str) -> str:
        """Clave de Redis donde se cachean las flags del tenant.

        Args:
            client_id: Tenant.

        Returns:
            `ff:{client_id}`.
        """
        return f"ff:{client_id}"

    async def _leer_de_la_base(self, client_id: UUID) -> dict[str, FlagValue]:
        """Lee las flags del tenant directamente de `agent_configs.config`.

        Args:
            client_id: Tenant.

        Returns:
            Las flags validas del agente activo; vacio si no hay agente o ninguna flag.
        """
        async with tenant_session(client_id) as session:
            config = (
                await session.execute(
                    select(AgentConfig.config)
                    .where(AgentConfig.is_active.is_(True))
                    .order_by(AgentConfig.created_at.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        return flags_del_tenant(config)

    async def get_all(self, client_id: UUID) -> dict[str, FlagValue]:
        """Devuelve las flags guardadas del tenant (solo las que tienen valor).

        Args:
            client_id: Tenant.

        Returns:
            `{flag: valor}`. Un tenant sin flags devuelve `{}`, y eso tambien se
            cachea para que las flags ausentes no vayan a la base cada vez.
        """
        clave = self._clave(client_id)
        try:
            cacheado = await self._cliente().get(clave)
            if cacheado is not None:
                return flags_del_tenant({_CONFIG_KEY: json.loads(cacheado)})
        except Exception:
            logger.warning("Redis no disponible; se lee ff:%s de la base", client_id, exc_info=True)

        flags = await self._leer_de_la_base(client_id)
        try:
            await self._cliente().setex(clave, CACHE_TTL, json.dumps(flags))
        except Exception:
            logger.warning("No se pudo cachear ff:%s", client_id, exc_info=True)
        return flags

    async def is_enabled(self, client_id: UUID, flag: str, default: bool = False) -> bool:
        """Si la flag esta activa para el tenant, sin distinguir entidades.

        Un porcentaje solo cuenta como activo al 100: sin una entidad que
        evaluar no hay forma de saber si cae dentro (usar `is_enabled_percentage`).

        Args:
            client_id: Tenant.
            flag: Nombre de la flag.
            default: Valor si el tenant no la ha configurado.

        Returns:
            El valor efectivo de la flag.
        """
        valor = (await self.get_all(client_id)).get(flag)
        if valor is None:
            return default
        return valor is True or (not isinstance(valor, bool) and valor >= 100)

    async def is_enabled_percentage(self, client_id: UUID, flag: str, entity_id: str) -> bool:
        """Rollout gradual: si `entity_id` cae dentro del porcentaje de la flag.

        Args:
            client_id: Tenant.
            flag: Nombre de la flag.
            entity_id: Entidad a evaluar.

        Returns:
            `True` si esta dentro; una flag sin configurar no activa a nadie.
        """
        valor = (await self.get_all(client_id)).get(flag)
        return en_rollout(client_id, flag, entity_id, _como_porcentaje(valor))

    async def set_flag(self, client_id: UUID, flag: str, value: FlagValue) -> None:
        """Guarda una flag del tenant e invalida su cache.

        La invalidacion va despues de que la transaccion se confirma: invalidar
        antes dejaria que un lector concurrente volviera a cachear el valor viejo.
        Si aun asi lo hiciera, el TTL lo corrige en cinco minutos.

        Args:
            client_id: Tenant.
            flag: Flag de `KNOWN_FLAGS`.
            value: Valor ya validado con `validar_valor()`.

        Raises:
            SinAgenteConfigurableError: Si el tenant no tiene un agente activo.
        """
        async with tenant_session(client_id) as session:
            config_id = (
                await session.execute(
                    select(AgentConfig.id)
                    .where(AgentConfig.client_id == client_id, AgentConfig.is_active.is_(True))
                    .order_by(AgentConfig.created_at.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if config_id is None:
                raise SinAgenteConfigurableError(str(client_id))
            await session.execute(
                _ACTUALIZAR_FLAGS,
                {
                    "parche": json.dumps({flag: value}),
                    "id": str(config_id),
                    "client_id": str(client_id),
                },
            )
        try:
            await self._cliente().delete(self._clave(client_id))
        except Exception:
            logger.warning("No se pudo invalidar ff:%s", client_id, exc_info=True)
