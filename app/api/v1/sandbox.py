"""Sandbox por tenant: probar configuracion sin tocar produccion (Sprint 14c, ADR-078).

    POST /api/v1/sandbox              crea el sandbox (copia configuracion y KB)
    GET  /api/v1/sandbox              estado
    POST /api/v1/sandbox/reset        lo rehace desde produccion
    GET  /api/v1/sandbox/agent-config   el agente del sandbox
    PUT  /api/v1/sandbox/agent-config   lo edita
    POST /api/v1/sandbox/messages       manda un mensaje de prueba
    POST /api/v1/sandbox/publish        publica a produccion (atomico)
    GET  /api/v1/sandbox/history        versiones guardadas
    POST /api/v1/sandbox/rollback       vuelve a una version

Solo `admin` y `super_admin`, y solo si el tenant tiene la flag `enable_sandbox`
(por defecto apagada). **La flag no es un control del operador:** la escribe el `admin`
del propio tenant (`/api/v1/admin/feature-flags`, como pide el spec), asi que un admin
puede encenderla. Lo que acota el gasto de verdad es el presupuesto de tokens del
sandbox (`SANDBOX_TOKEN_BUDGET`), que ni siquiera un reset repone. El tenant sale del
token; el sandbox se resuelve desde `tenant_sandboxes`.
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends

from app.core.dependencies import require_role
from app.core.exceptions import (
    DUPLICATE,
    FORBIDDEN,
    INTERNAL_ERROR,
    NOT_FOUND,
    VALIDATION_ERROR,
    AppException,
)
from app.core.feature_flags import FeatureFlags
from app.schemas.agent_config import AgentConfigUpdate
from app.schemas.sandbox import (
    HistoryItem,
    PublishResponse,
    RollbackRequest,
    RollbackResponse,
    SandboxAgentConfig,
    SandboxMessageRequest,
    SandboxMessageResponse,
    SandboxStatus,
)
from app.services import sandbox as servicio

logger = logging.getLogger(__name__)

router = APIRouter()

_ROLES = ("super_admin", "admin")
FLAG = "enable_sandbox"


async def sandbox_habilitado(
    user: dict[str, Any] = Depends(require_role(*_ROLES)),
) -> dict[str, Any]:
    """Deja pasar solo a un admin de un tenant con `enable_sandbox` activa.

    Args:
        user: Usuario autenticado, ya con el rol comprobado.

    Returns:
        El mismo usuario.

    Raises:
        AppException: 403 si el tenant no tiene la flag.
    """
    if not await FeatureFlags().is_enabled(user["client_id"], FLAG, default=False):
        raise AppException(
            status_code=403,
            error_code=FORBIDDEN,
            message=f"El modo sandbox no esta habilitado para este tenant (flag {FLAG})",
        )
    return user


def _sin_sandbox() -> AppException:
    """El error 404 de un tenant que todavia no creo su sandbox.

    Returns:
        La excepcion lista para lanzar.
    """
    return AppException(
        status_code=404,
        error_code=NOT_FOUND,
        message="El tenant no tiene sandbox: crealo primero con POST /api/v1/sandbox",
    )


def _estado(datos: dict[str, Any]) -> SandboxStatus:
    """Convierte el estado del servicio en la respuesta de la API.

    Args:
        datos: Lo que devuelve `servicio.obtener_estado()`.

    Returns:
        El estado del sandbox.
    """
    return SandboxStatus(**datos)


@router.post("", response_model=SandboxStatus, status_code=201)
async def create_sandbox(user: dict[str, Any] = Depends(sandbox_habilitado)) -> SandboxStatus:
    """Crea el sandbox del tenant, con una copia de su configuracion y su base de conocimiento.

    Args:
        user: Usuario autenticado.

    Returns:
        El estado del sandbox recien creado.

    Raises:
        AppException: 409 si ya tiene uno; 400 si el propio tenant es un sandbox.
    """
    client_id: UUID = user["client_id"]
    try:
        await servicio.crear_sandbox(client_id, _usuario(user))
    except servicio.SandboxYaExisteError as exc:
        raise AppException(
            status_code=409, error_code=DUPLICATE, message="El tenant ya tiene un sandbox"
        ) from exc
    except servicio.SandboxDeSandboxError as exc:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message="Un sandbox no puede tener su propio sandbox",
        ) from exc
    return _estado(await servicio.obtener_estado(client_id))


@router.get("", response_model=SandboxStatus)
async def get_sandbox(user: dict[str, Any] = Depends(sandbox_habilitado)) -> SandboxStatus:
    """Estado del sandbox del tenant (`exists: false` si todavia no lo tiene).

    Args:
        user: Usuario autenticado.

    Returns:
        El estado.
    """
    return _estado(await servicio.obtener_estado(user["client_id"]))


@router.post("/reset", response_model=SandboxStatus)
async def reset_sandbox(user: dict[str, Any] = Depends(sandbox_habilitado)) -> SandboxStatus:
    """Rehace el sandbox desde produccion: descarta sus cambios y sus conversaciones de prueba.

    Args:
        user: Usuario autenticado.

    Returns:
        El estado, con `reset_at` actualizado.

    Raises:
        AppException: 404 si el tenant no tiene sandbox.
    """
    client_id: UUID = user["client_id"]
    try:
        await servicio.reiniciar_sandbox(client_id)
    except servicio.SandboxNoExisteError as exc:
        raise _sin_sandbox() from exc
    return _estado(await servicio.obtener_estado(client_id))


@router.get("/agent-config", response_model=SandboxAgentConfig)
async def get_sandbox_agent_config(
    user: dict[str, Any] = Depends(sandbox_habilitado),
) -> SandboxAgentConfig:
    """El agente del sandbox, tal como lo leeria el grafo.

    Args:
        user: Usuario autenticado.

    Returns:
        Los campos editables del agente.

    Raises:
        AppException: 404 si no hay sandbox; 400 si no tiene un agente activo.
    """
    try:
        return SandboxAgentConfig(**await servicio.leer_config_sandbox(user["client_id"]))
    except servicio.SandboxNoExisteError as exc:
        raise _sin_sandbox() from exc
    except servicio.SinConfiguracionError as exc:
        raise _sin_agente(exc) from exc


@router.put("/agent-config", response_model=SandboxAgentConfig)
async def update_sandbox_agent_config(
    data: AgentConfigUpdate, user: dict[str, Any] = Depends(sandbox_habilitado)
) -> SandboxAgentConfig:
    """Edita el agente del sandbox; `config` se mezcla con el actual, no lo reemplaza.

    Args:
        data: Campos a cambiar; los que no se envian no se tocan.
        user: Usuario autenticado.

    Returns:
        El agente ya actualizado.

    Raises:
        AppException: 404 si no hay sandbox; 400 si `config` trae claves propias del
            tenant (`clinical`, `marketing`, `feature_flags`) o no hay un agente activo.
    """
    cambios = data.model_dump(exclude_unset=True)
    try:
        return SandboxAgentConfig(
            **await servicio.actualizar_config_sandbox(user["client_id"], cambios)
        )
    except servicio.SandboxNoExisteError as exc:
        raise _sin_sandbox() from exc
    except servicio.ClaveDelTenantError as exc:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message=(
                f"Estas claves del config son del tenant y no se editan en el sandbox: "
                f"{', '.join(exc.claves)}"
            ),
        ) from exc
    except servicio.CampoNoAnulableError as exc:
        raise AppException(
            status_code=400,
            error_code=VALIDATION_ERROR,
            message=f"Estos campos no admiten null: {', '.join(exc.campos)}",
        ) from exc
    except servicio.SinConfiguracionError as exc:
        raise _sin_agente(exc) from exc


@router.post("/messages", response_model=SandboxMessageResponse)
async def send_sandbox_message(
    data: SandboxMessageRequest, user: dict[str, Any] = Depends(sandbox_habilitado)
) -> SandboxMessageResponse:
    """Manda un mensaje de prueba al agente del sandbox y devuelve su respuesta.

    Recorre el grafo real con el LLM de verdad, dentro del presupuesto de tokens del
    sandbox (`SANDBOX_TOKEN_BUDGET`); no envia nada por ningun canal real.

    Args:
        data: Mensaje de prueba.
        user: Usuario autenticado.

    Returns:
        La respuesta del agente y lo que decidio (intent, handoff, idioma).

    Raises:
        AppException: 404 si no hay sandbox; 502 si el agente no pudo responder.
    """
    try:
        resultado = await servicio.enviar_mensaje_de_prueba(
            user["client_id"], data.text, _usuario(user), data.new_conversation
        )
    except servicio.SandboxNoExisteError as exc:
        raise _sin_sandbox() from exc
    except Exception as exc:
        logger.exception("El agente del sandbox del tenant %s no pudo responder", user["client_id"])
        raise AppException(
            status_code=502,
            error_code=INTERNAL_ERROR,
            message="El agente del sandbox no pudo responder; revisa su configuracion",
        ) from exc
    return SandboxMessageResponse(**resultado)


@router.post("/publish", response_model=PublishResponse)
async def publish_sandbox(user: dict[str, Any] = Depends(sandbox_habilitado)) -> PublishResponse:
    """Publica la configuracion del sandbox a produccion, de forma atomica.

    Guarda antes lo que habia en produccion, para poder volver con `/rollback`.
    No toca las claves propias del tenant (`clinical`, `marketing`, `feature_flags`).

    Args:
        user: Usuario autenticado.

    Returns:
        La version del historial donde quedo lo que habia antes.

    Raises:
        AppException: 404 si no hay sandbox; 400 si el sandbox no tiene un agente activo.
    """
    try:
        version = await servicio.publicar(user["client_id"], _usuario(user))
    except servicio.SandboxNoExisteError as exc:
        raise _sin_sandbox() from exc
    except servicio.SinConfiguracionError as exc:
        raise _sin_agente(exc) from exc
    return PublishResponse(version=version)


@router.get("/history", response_model=list[HistoryItem])
async def get_sandbox_history(
    user: dict[str, Any] = Depends(sandbox_habilitado),
) -> list[HistoryItem]:
    """Versiones de la configuracion de produccion guardadas, la mas reciente primero.

    Args:
        user: Usuario autenticado.

    Returns:
        Las versiones del historial.
    """
    return [HistoryItem(**v) for v in await servicio.listar_historial(user["client_id"])]


@router.post("/rollback", response_model=RollbackResponse)
async def rollback_sandbox(
    data: RollbackRequest, user: dict[str, Any] = Depends(sandbox_habilitado)
) -> RollbackResponse:
    """Devuelve la configuracion de produccion a una version del historial.

    Lo que habia antes de revertir se guarda como una version nueva: volver atras
    tambien se puede deshacer.

    Args:
        data: Version a restaurar; sin ella, la ultima.
        user: Usuario autenticado.

    Returns:
        La version restaurada y la version donde quedo lo que habia.

    Raises:
        AppException: 404 si esa version no existe (o no hay historial).
    """
    try:
        resultado = await servicio.revertir(user["client_id"], _usuario(user), data.version)
    except servicio.VersionInexistenteError as exc:
        raise AppException(
            status_code=404,
            error_code=NOT_FOUND,
            message="No hay una version del historial a la que volver",
        ) from exc
    return RollbackResponse(**resultado)


def _usuario(user: dict[str, Any]) -> UUID:
    """El id del usuario del token (siempre viene: lo exige el middleware de tenant).

    Args:
        user: Usuario autenticado, tal como lo entrega `require_role()`.

    Returns:
        El `user_id` como UUID.
    """
    return UUID(str(user["user_id"]))


def _sin_agente(exc: Exception) -> AppException:
    """El error 400 de un sandbox sin agente activo.

    Args:
        exc: La `SinConfiguracionError` que lo origino; su mensaje llega al cliente.

    Returns:
        La excepcion lista para lanzar.
    """
    return AppException(status_code=400, error_code=VALIDATION_ERROR, message=str(exc))
