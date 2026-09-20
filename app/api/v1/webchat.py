"""Endpoint WebSocket del widget de chat web — `GET /api/v1/webchat/{channel_token}`.

Como el router de webhooks, este endpoint es distinto al resto de la API:

- **No pasa por `TenantContextMiddleware`** ni por ningun otro middleware: los
  tres son `BaseHTTPMiddleware`, que deja pasar de largo todo lo que no sea
  `scope["type"] == "http"`. El visitante no tiene JWT ni usuario.
- **La autenticacion es en dos niveles.** El `channel_token` de la URL autentica
  la *instalacion* del widget (identifica al tenant; viaja en el HTML de la
  pagina, asi que no es un secreto fuerte). El token `session` de la query
  autentica al *visitante*: lo emite y lo firma el servidor (ADR-057), y es lo
  unico que impide reclamar la conversacion de otro.
- **La respuesta del agente no la escribe este proceso.** La genera un worker de
  Celery, que la publica en Redis; aqui se esta suscrito al canal de la sesion y
  se reenvia lo que llegue (ADR-056).

Reconexion: el widget guarda su token `session` y el `message_id` del ultimo
frame que recibio, y al reconectar los manda como query params
(`?session=...&last_message_id=...`). Lo que se envio mientras estaba
desconectado se repone desde la base de datos.

Protocolo (frames JSON, `app/schemas/webchat.py`):

    servidor -> {"type": "connected", "session": "<token firmado>", ...}
    cliente  -> {"type": "message", "text": "hola", "client_message_id": "1"}
    servidor -> {"type": "ack", "message_id": "wc-...", "client_message_id": "1"}
    servidor -> {"type": "message", "message_id": "wc-...", "text": "..."}
    cliente  -> {"type": "typing" | "read_receipt" | "ping"}   (no hacen nada)
"""

import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.encryption import blind_index
from app.models.contact_identifier import ContactIdentifier
from app.models.conversation import Conversation
from app.models.message import Message
from app.schemas.webchat import WebchatMessage
from app.services.messaging.base import IgnoredWebhookError
from app.services.messaging.webchat import (
    WebchatProvider,
    canal_de_sesion,
    nueva_sesion,
    nuevo_id_externo,
    sesion_de_token,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: Codigos de cierre del socket (rango 4000-4999, reservado a la aplicacion).
CIERRE_ERROR_INTERNO = 4000
CIERRE_TOKEN_INVALIDO = 4001
CIERRE_EXCESO_DE_MENSAJES = 4008

#: Codigos de error que viajan en un frame `error`.
FRAME_INVALIDO = "INVALID_FRAME"
FRAME_DEMASIADO_GRANDE = "FRAME_TOO_LARGE"
DEMASIADOS_MENSAJES = "RATE_LIMITED"

#: Tope de un frame entrante. El widget manda texto: 16 KB es holgado, y sin
#: tope una pestana puede mandar megabytes que el proceso de la API tiene que
#: acumular en memoria antes de mirarlos.
MAX_FRAME_CHARS = 16 * 1024

#: Ventana del limitador de mensajes por socket, en segundos.
VENTANA_LIMITE_SEGUNDOS = 60.0

#: Canal y proveedor de este endpoint, tal como los espera el worker.
CANAL = "webchat"


class _Limitador:
    """Cuenta los mensajes de un socket dentro de una ventana deslizante."""

    def __init__(self, maximo: int, ventana: float = VENTANA_LIMITE_SEGUNDOS) -> None:
        """Inicializa el limitador.

        Args:
            maximo: Mensajes permitidos por ventana. 0 o menos = sin limite.
            ventana: Duracion de la ventana en segundos.
        """
        self.maximo = maximo
        self.ventana = ventana
        self._marcas: deque[float] = deque()

    def permite(self, ahora: float) -> bool:
        """Registra un mensaje y dice si esta dentro del limite.

        Args:
            ahora: Momento actual (reloj monotono).

        Returns:
            True si el mensaje se puede procesar.
        """
        if self.maximo <= 0:
            return True
        while self._marcas and ahora - self._marcas[0] > self.ventana:
            self._marcas.popleft()
        if len(self._marcas) >= self.maximo:
            return False
        self._marcas.append(ahora)
        return True


def _ahora_iso() -> str:
    """Devuelve el momento actual en ISO-8601 con timezone.

    Returns:
        Marca de tiempo UTC.
    """
    return datetime.now(timezone.utc).isoformat()


def resolver_tenant(channel_token: str) -> UUID | None:
    """Valida el token del canal y devuelve el tenant dueno del widget.

    Mientras no exista la tabla `channel_configs` (Fase 2) hay un unico widget,
    el de `DEFAULT_CLIENT_ID`, y su token es `WEBCHAT_CHANNEL_TOKEN` — mismo
    criterio que `_resolve_client_id()` en `app/tasks/webhook_processor.py`.

    Args:
        channel_token: Token tomado de la URL del socket.

    Returns:
        El tenant, o `None` si el token no es valido o el canal no esta
        configurado.
    """
    import hmac

    settings = get_settings()
    esperado = settings.WEBCHAT_CHANNEL_TOKEN
    # Un token vacio en el entorno significa "canal deshabilitado": nunca debe
    # validar por accidente contra una URL que tampoco traiga token.
    if not esperado or not channel_token:
        return None
    if not hmac.compare_digest(channel_token, esperado):
        return None
    if not settings.DEFAULT_CLIENT_ID:
        logger.error("Webchat sin DEFAULT_CLIENT_ID: imposible resolver el tenant")
        return None
    try:
        return UUID(settings.DEFAULT_CLIENT_ID)
    except ValueError:
        logger.error("DEFAULT_CLIENT_ID no es un UUID valido")
        return None


async def mensajes_perdidos(
    client_id: UUID, session_id: str, last_message_id: str, limite: int
) -> list[dict[str, Any]]:
    """Recupera lo que el agente respondio mientras el visitante no estaba.

    Args:
        client_id: Tenant dueno de la conversacion.
        session_id: Sesion del visitante (su identificador de contacto).
        last_message_id: `external_message_id` del ultimo frame que recibio.
        limite: Maximo de mensajes a reponer.

    Returns:
        Frames `message` en orden cronologico. Vacio si no hay sesion previa o
        si el mensaje de referencia no existe (un id viejo o de otra sesion: se
        prefiere no reponer nada antes que volcarle un historial entero).
    """
    async with tenant_session(client_id) as session:
        identificador = (
            await session.execute(
                select(ContactIdentifier).where(
                    ContactIdentifier.client_id == client_id,
                    ContactIdentifier.channel == CANAL,
                    ContactIdentifier.identifier_hash == blind_index(session_id, client_id),
                )
            )
        ).scalar_one_or_none()
        if identificador is None:
            return []

        # El ancla se busca dentro de las conversaciones de *este* contacto: un
        # id externo de otro visitante no puede servir de punto de partida.
        conversaciones = select(Conversation.id).where(
            Conversation.client_id == client_id,
            Conversation.contact_id == identificador.contact_id,
            Conversation.channel == CANAL,
        )
        ancla = (
            await session.execute(
                select(Message.created_at).where(
                    Message.client_id == client_id,
                    Message.conversation_id.in_(conversaciones),
                    Message.external_message_id == last_message_id,
                )
            )
        ).scalar_one_or_none()
        if ancla is None:
            logger.info("Webchat: el last_message_id no pertenece a esta sesion; no se repone")
            return []

        pendientes = (
            (
                await session.execute(
                    select(Message)
                    .where(
                        Message.client_id == client_id,
                        Message.conversation_id.in_(conversaciones),
                        Message.direction == "outbound",
                        Message.created_at > ancla,
                    )
                    .order_by(Message.created_at.asc())
                    .limit(limite)
                )
            )
            .scalars()
            .all()
        )

        return [
            {
                "type": "message",
                "message_id": mensaje.external_message_id,
                "text": mensaje.content or "",
                "timestamp": mensaje.created_at.isoformat(),
                "replayed": True,
            }
            for mensaje in pendientes
        ]


async def _encolar_mensaje(client_id: UUID, session_id: str, frame: WebchatMessage) -> str | None:
    """Normaliza el frame y lo encola en el worker de webhooks.

    Args:
        client_id: Tenant dueno de la conversacion.
        session_id: Sesion del visitante.
        frame: Frame ya validado.

    Returns:
        El `external_message_id` encolado, o `None` si el frame no era un
        mensaje procesable.

    Raises:
        Exception: Si no se pudo encolar en Celery (el llamador avisa al widget).
    """
    from app.tasks.webhook_processor import process_incoming_message

    payload: dict[str, Any] = {
        "type": frame.type,
        "session_id": session_id,
        "text": frame.text,
        "external_message_id": nuevo_id_externo(),
        "client_message_id": frame.client_message_id,
    }
    try:
        normalizado = await WebchatProvider().parse_webhook(payload)
    except IgnoredWebhookError as exc:
        logger.info("Frame de webchat ignorado: %s", exc)
        return None

    await run_in_threadpool(
        process_incoming_message.delay,
        provider=CANAL,
        channel=CANAL,
        normalized_message=normalizado.model_dump(mode="json"),
    )
    return normalizado.external_message_id


async def _recibir(websocket: WebSocket, client_id: UUID, session_id: str) -> None:
    """Bucle de entrada: lee frames del widget y los encola.

    Args:
        websocket: Socket ya aceptado.
        client_id: Tenant dueno de la conversacion.
        session_id: Sesion del visitante.

    Raises:
        WebSocketDisconnect: Cuando el visitante cierra la pestana.
    """
    limitador = _Limitador(get_settings().WEBCHAT_RATE_LIMIT_PER_MINUTE)

    while True:
        crudo = await websocket.receive_text()

        if len(crudo) > MAX_FRAME_CHARS:
            await _enviar_error(websocket, FRAME_DEMASIADO_GRANDE, "El mensaje es demasiado largo")
            continue

        if not limitador.permite(anyio.current_time()):
            await _enviar_error(websocket, DEMASIADOS_MENSAJES, "Demasiados mensajes seguidos")
            await websocket.close(code=CIERRE_EXCESO_DE_MENSAJES, reason="Rate limited")
            return

        try:
            frame = WebchatMessage(**json.loads(crudo))
        except Exception:
            await _enviar_error(websocket, FRAME_INVALIDO, "Frame invalido")
            continue

        if frame.type != "message":
            continue

        try:
            external_id = await _encolar_mensaje(client_id, session_id, frame)
        except Exception:
            # No se propaga el motivo: un traceback nunca sale al cliente.
            logger.exception("No se pudo encolar un mensaje de webchat")
            await _enviar_error(websocket, "QUEUE_UNAVAILABLE", "No se pudo recibir el mensaje")
            continue

        if external_id is not None:
            await websocket.send_json(
                {
                    "type": "ack",
                    "message_id": external_id,
                    "client_message_id": frame.client_message_id,
                    "timestamp": _ahora_iso(),
                }
            )


async def _emitir(websocket: WebSocket, pubsub: Any) -> None:
    """Bucle de salida: reenvia al socket lo que se publique en Redis.

    Args:
        websocket: Socket ya aceptado.
        pubsub: Suscripcion al canal de la sesion.
    """
    async for publicado in pubsub.listen():
        if publicado.get("type") != "message":
            continue
        datos = publicado.get("data")
        try:
            frame = json.loads(datos)
        except (TypeError, ValueError):
            logger.warning("Frame de webchat no deserializable en el canal de la sesion")
            continue
        try:
            await websocket.send_json(frame)
        except (WebSocketDisconnect, RuntimeError):
            # El visitante cerro mientras se le escribia: lo detecta el bucle de
            # entrada, que es quien termina la conexion.
            logger.info("Webchat: el socket se cerro mientras se enviaba un frame")
            return


async def _enviar_error(websocket: WebSocket, codigo: str, mensaje: str) -> None:
    """Manda un frame de error al widget.

    Args:
        websocket: Socket ya aceptado.
        codigo: Codigo de error estable.
        mensaje: Texto para el usuario. Nunca un traceback.
    """
    await websocket.send_json(
        {"type": "error", "error_code": codigo, "message": mensaje, "timestamp": _ahora_iso()}
    )


@router.websocket("/{channel_token}")
async def webchat_endpoint(websocket: WebSocket, channel_token: str) -> None:
    """Atiende la conexion de un visitante del widget.

    Args:
        websocket: Socket entrante, todavia sin aceptar.
        channel_token: Token de la instalacion del widget, tomado de la URL.
    """
    from app.services.dedup import get_redis

    client_id = resolver_tenant(channel_token)
    if client_id is None:
        logger.warning("Webchat: token de canal invalido o canal sin configurar")
        await websocket.close(code=CIERRE_TOKEN_INVALIDO, reason="Invalid channel token")
        return

    token_recibido = websocket.query_params.get("session")
    session_id = sesion_de_token(token_recibido)
    reanudada = session_id is not None
    if session_id is None:
        session_id, token_sesion = nueva_sesion()
    else:
        token_sesion = str(token_recibido)

    await websocket.accept()
    await websocket.send_json(
        {
            "type": "connected",
            "session": token_sesion,
            "timestamp": _ahora_iso(),
        }
    )

    ultimo = websocket.query_params.get("last_message_id")
    if reanudada and ultimo:
        try:
            for frame in await mensajes_perdidos(
                client_id, session_id, ultimo, get_settings().WEBCHAT_REPLAY_LIMIT
            ):
                await websocket.send_json(frame)
        except Exception:
            # Que falle la reposicion no debe impedir seguir conversando.
            logger.exception("No se pudieron reponer los mensajes perdidos de webchat")

    pubsub = get_redis().pubsub()
    await pubsub.subscribe(canal_de_sesion(client_id, session_id))

    # Task group de anyio y no `asyncio.wait` + `gather(return_exceptions=True)`:
    # ese gather se traga tambien la cancelacion que viene de fuera (el servidor
    # apagandose, o el cancel scope del cliente de tests), y una cancelacion que
    # nadie honra deja la conexion colgada. anyio es la capa que ya usa Starlette.
    try:
        async with anyio.create_task_group() as grupo:
            grupo.start_soon(_emitir, websocket, pubsub)
            try:
                await _recibir(websocket, client_id, session_id)
            except WebSocketDisconnect:
                logger.info("Webchat: el visitante cerro la conexion")
            finally:
                # Termina tambien el bucle de salida: sin esto el task group
                # espera para siempre a una suscripcion que nunca acaba.
                grupo.cancel_scope.cancel()
    except Exception:
        logger.exception("Error en la conexion de webchat")
        try:
            await websocket.close(code=CIERRE_ERROR_INTERNO, reason="Internal error")
        except Exception:
            logger.debug("El socket de webchat ya estaba cerrado")
    finally:
        try:
            await pubsub.unsubscribe()
            await pubsub.close()
        except Exception:
            logger.debug("No se pudo cerrar la suscripcion de webchat")
