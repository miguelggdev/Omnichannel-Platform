"""Endpoint WebSocket del Webchat: `wss://host/api/v1/webchat/{channel_token}`.

Ciclo de una conexion:

1. **Antes de aceptar** (se responde 403 al handshake, sin abrir el WebSocket): el
   `channel_token` de la URL, el `Origin` (un navegador siempre lo manda; solo se
   acepta el de los sitios permitidos) y que haya un tenant configurado.
2. **`hello` en los primeros segundos**, con la sesion (o `null` si es un visitante
   nuevo). La sesion va en el primer frame y no en la URL porque una URL acaba en los
   logs de acceso de cada proxy y la sesion es un secreto al portador
   (`webchat_session.py`). Un token ausente, mal firmado o caducado no es un error:
   se emite una sesion nueva.
3. **Suscripcion a Redis y luego recuperacion.** Se suscribe al canal del visitante
   *antes* de leer los mensajes perdidos en la base, para que nada publicado en
   medio se pierda; un mensaje que llegue por las dos vias lo deduplica el cliente por
   `message_id` (y el servidor no reenvia en vivo los que ya mando en la recuperacion).
4. **Bucle:** una tarea lee frames del visitante y los encola como cualquier otro
   webhook (`process_incoming_message`); otra reenvia lo que la IA o un agente
   publiquen para ese visitante.

Limites (todos en `Settings`): tamano del frame, longitud del mensaje, mensajes por
minuto y conexiones simultaneas por visitante. La ultima cuenta por proceso: con N
procesos, el tope real es N veces mayor. Cada conexion mantiene una suscripcion de
Redis propia; con decenas de miles de visitantes simultaneos habra que pasar a un
suscriptor compartido por proceso.
"""

import asyncio
import contextlib
import hmac
import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings, get_settings
from app.schemas.webchat import (
    ButtonReplyFrame,
    HelloFrame,
    InboundFrame,
    MessageFrame,
    PingFrame,
    frame_de_error,
)
from app.services.dedup import get_redis, mark_if_new, release_mark
from app.services.messaging.webchat import WebchatProvider, canal_de_salida
from app.services.webchat_history import mensajes_perdidos
from app.services.webchat_session import emitir_sesion, verificar_sesion

logger = logging.getLogger(__name__)

router = APIRouter()

#: Codigos de cierre propios (rango 4000-4999 reservado a la aplicacion).
CLOSE_TOKEN_INVALIDO = 4401
CLOSE_ORIGEN_NO_PERMITIDO = 4403
CLOSE_HELLO_INVALIDO = 4400
CLOSE_INACTIVO = 4408
CLOSE_DEMASIADAS_CONEXIONES = 4429
CLOSE_FRAME_GRANDE = 1009
CLOSE_ERROR_INTERNO = 1011

_INBOUND: TypeAdapter[Any] = TypeAdapter(InboundFrame)

#: Conexiones abiertas por visitante, en este proceso.
_conexiones: dict[str, int] = {}

#: Ruta fija (sin entrada del usuario): no hay riesgo de path traversal.
_WIDGET_JS_PATH = Path(__file__).resolve().parents[2] / "static" / "webchat-widget.js"


@router.get("/widget.js", include_in_schema=False)
async def widget_js() -> FileResponse:
    """Sirve el script embebible del widget de Webchat.

    Es el MISMO archivo para todos los tenants: el `channel_token` y el origen
    de la API los pone quien lo instala, como atributos `data-*` del propio
    `<script>` (ver la cabecera del archivo). No lleva secretos, asi que no
    hace falta autenticacion — y `<script src>` no esta sujeto a CORS, asi que
    tampoco hace falta configurar cabeceras especiales para que cargue desde
    el dominio del cliente.

    Returns:
        El archivo, con cache corto (5 min): sirve para poder corregirlo sin
        depender de que cada visitante purgue su cache del navegador.
    """
    return FileResponse(
        _WIDGET_JS_PATH,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300"},
    )


def _origen_permitido(origen: str | None, settings: Settings) -> bool:
    """Comprueba el `Origin` del handshake.

    Un navegador siempre lo envia; que falte no es un widget legitimo.

    Args:
        origen: Cabecera `Origin`.
        settings: Configuracion.

    Returns:
        True si el origen esta entre los permitidos (`WEBCHAT_ALLOWED_ORIGINS`, o
        `CORS_ORIGINS` si esa lista esta vacia).
    """
    if not origen:
        return False
    permitidos = settings.WEBCHAT_ALLOWED_ORIGINS or settings.CORS_ORIGINS
    normalizados = {o.rstrip("/").lower() for o in permitidos}
    return origen.rstrip("/").lower() in normalizados


def _token_valido(token: str, settings: Settings) -> bool:
    """Compara el token del canal en tiempo constante; vacio = canal desactivado.

    Args:
        token: Token de la URL.
        settings: Configuracion.

    Returns:
        True si coincide con `WEBCHAT_CHANNEL_TOKEN` (que debe estar configurado).
    """
    esperado = settings.WEBCHAT_CHANNEL_TOKEN
    if not esperado:
        return False
    return hmac.compare_digest(token.encode("utf-8"), esperado.encode("utf-8"))


def _tenant(settings: Settings) -> UUID | None:
    """Tenant del canal (`DEFAULT_CLIENT_ID`, ADR-030).

    Args:
        settings: Configuracion.

    Returns:
        El UUID, o `None` si no esta configurado o no es valido.
    """
    try:
        return UUID(settings.DEFAULT_CLIENT_ID)
    except (ValueError, AttributeError):
        return None


async def _dentro_del_limite(client_id: UUID, visitor_id: str, limite: int) -> bool:
    """Cuenta un mensaje en la ventana de un minuto del visitante.

    Args:
        client_id: Tenant.
        visitor_id: Visitante.
        limite: Mensajes permitidos por minuto.

    Returns:
        True si todavia esta dentro del limite.
    """
    redis = get_redis()
    clave = f"webchat:rate:{client_id}:{visitor_id}"
    # SET NX EX antes del INCR: la clave nace siempre con caducidad. Con INCR y luego
    # EXPIRE, un fallo entre los dos la dejaria sin TTL y bloquearia al visitante para
    # siempre.
    await redis.set(clave, 0, ex=60, nx=True)
    return int(await redis.incr(clave)) <= limite


async def _ingerir(
    client_id: UUID, visitor_id: str, frame: MessageFrame | ButtonReplyFrame, nombre: str | None
) -> dict[str, Any]:
    """Normaliza un frame del visitante y lo encola como cualquier otro webhook.

    Args:
        client_id: Tenant (solo para los logs).
        visitor_id: Visitante ya autenticado.
        frame: Frame validado.
        nombre: Nombre que el visitante dio en el `hello`.

    Returns:
        El frame a devolver al cliente: `ack`, o `error` si no se pudo encolar.
    """
    payload: dict[str, Any] = {
        "visitor_id": visitor_id,
        "message_id": frame.message_id,
        "name": nombre,
    }
    if isinstance(frame, ButtonReplyFrame):
        payload["button"] = {"id": frame.id, "title": frame.title}
    else:
        payload["text"] = frame.text

    normalized = await WebchatProvider().parse_webhook(payload)
    externo = normalized.external_message_id
    ack = {"type": "ack", "message_id": frame.message_id}

    if not await mark_if_new("webchat", externo):
        return ack  # reintento del cliente: ya se recibio

    try:
        from app.tasks.webhook_processor import process_incoming_message

        await run_in_threadpool(
            process_incoming_message.delay,
            provider="webchat",
            channel="webchat",
            normalized_message=normalized.model_dump(mode="json"),
        )
    except Exception:
        # Sin liberar la marca, el reintento del cliente se descartaria como duplicado.
        await release_mark("webchat", externo)
        logger.exception("Webchat: no se pudo encolar un mensaje (tenant %s)", client_id)
        return frame_de_error("queue_unavailable", "No se pudo enviar el mensaje, reintenta")
    return ack


async def _leer(
    websocket: WebSocket, client_id: UUID, visitor_id: str, nombre: str | None, settings: Settings
) -> None:
    """Lee frames del visitante hasta que se desconecte, se agote el tiempo o falle.

    Args:
        websocket: Conexion aceptada.
        client_id: Tenant.
        visitor_id: Visitante autenticado.
        nombre: Nombre dado en el `hello`.
        settings: Configuracion.
    """
    while True:
        try:
            recibido = await asyncio.wait_for(
                websocket.receive(), timeout=settings.WEBCHAT_IDLE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            await websocket.close(code=CLOSE_INACTIVO)
            return
        if recibido["type"] == "websocket.disconnect":
            return

        raw = recibido.get("text")
        if raw is None:
            await websocket.send_json(frame_de_error("invalid_frame", "Solo se admite texto"))
            continue
        if len(raw.encode("utf-8")) > settings.WEBCHAT_MAX_FRAME_BYTES:
            await websocket.send_json(frame_de_error("frame_too_large", "Frame demasiado grande"))
            await websocket.close(code=CLOSE_FRAME_GRANDE)
            return

        try:
            frame = _INBOUND.validate_json(raw)
        except ValidationError:
            await websocket.send_json(frame_de_error("invalid_frame", "Frame no valido"))
            continue

        if isinstance(frame, PingFrame):
            await websocket.send_json({"type": "pong"})
            continue
        if isinstance(frame, MessageFrame) and len(frame.text) > settings.WEBCHAT_MAX_MESSAGE_CHARS:
            await websocket.send_json(
                frame_de_error("message_too_long", "El mensaje es demasiado largo")
            )
            continue
        if not await _dentro_del_limite(
            client_id, visitor_id, settings.WEBCHAT_MAX_MESSAGES_PER_MINUTE
        ):
            await websocket.send_json(
                frame_de_error("rate_limited", "Demasiados mensajes, espera un momento")
            )
            continue

        await websocket.send_json(await _ingerir(client_id, visitor_id, frame, nombre))


async def _reenviar(websocket: WebSocket, pubsub: Any, ya_enviados: frozenset[str]) -> None:
    """Reenvia al visitante lo que se publique en su canal de Redis.

    Args:
        websocket: Conexion aceptada.
        pubsub: Suscripcion de Redis al canal del visitante.
        ya_enviados: `message_id` que ya se mandaron en la recuperacion.
    """
    async for mensaje in pubsub.listen():
        if mensaje.get("type") != "message":
            continue
        try:
            frame = json.loads(mensaje["data"])
        except (TypeError, ValueError):
            logger.warning("Webchat: frame no valido en el canal de salida")
            continue
        if frame.get("message_id") in ya_enviados:
            continue
        await websocket.send_json(frame)


async def _recibir_hello(websocket: WebSocket, settings: Settings) -> HelloFrame | None:
    """Espera y valida el primer frame.

    Args:
        websocket: Conexion aceptada.
        settings: Configuracion.

    Returns:
        El `hello`, o `None` si no llego a tiempo o no es valido.
    """
    try:
        raw = await asyncio.wait_for(
            websocket.receive_text(), timeout=settings.WEBCHAT_HELLO_TIMEOUT_SECONDS
        )
    except (TimeoutError, WebSocketDisconnect, KeyError):
        return None
    if len(raw.encode("utf-8")) > settings.WEBCHAT_MAX_FRAME_BYTES:
        return None
    try:
        return HelloFrame.model_validate_json(raw)
    except ValidationError:
        return None


@router.websocket("/{channel_token}")
async def webchat_endpoint(websocket: WebSocket, channel_token: str) -> None:
    """WebSocket bidireccional del widget de Webchat.

    Args:
        websocket: Conexion entrante.
        channel_token: Token del canal (identifica el canal, no es un secreto).
    """
    settings = get_settings()

    if not _token_valido(channel_token, settings):
        await websocket.close(code=CLOSE_TOKEN_INVALIDO)
        return
    if not _origen_permitido(websocket.headers.get("origin"), settings):
        logger.warning("Webchat: origen no permitido: %r", websocket.headers.get("origin"))
        await websocket.close(code=CLOSE_ORIGEN_NO_PERMITIDO)
        return
    client_id = _tenant(settings)
    if client_id is None:
        logger.error("Webchat: DEFAULT_CLIENT_ID no configurado")
        await websocket.close(code=CLOSE_ERROR_INTERNO)
        return

    await websocket.accept()

    hello = await _recibir_hello(websocket, settings)
    if hello is None:
        await websocket.send_json(frame_de_error("invalid_hello", "Se esperaba un hello valido"))
        await websocket.close(code=CLOSE_HELLO_INVALIDO)
        return

    visitor_id = verificar_sesion(hello.session, client_id)
    if visitor_id is not None:
        token = hello.session
    else:
        sesion = emitir_sesion(client_id)
        visitor_id, token = sesion.visitor_id, sesion.token
        # Un `last_message_id` sin sesion valida no da derecho a recuperar nada.
        hello = hello.model_copy(update={"last_message_id": None})

    if _conexiones.get(visitor_id, 0) >= settings.WEBCHAT_MAX_CONNECTIONS_PER_VISITOR:
        await websocket.send_json(
            frame_de_error("too_many_connections", "Demasiadas conexiones abiertas")
        )
        await websocket.close(code=CLOSE_DEMASIADAS_CONEXIONES)
        return

    _conexiones[visitor_id] = _conexiones.get(visitor_id, 0) + 1
    pubsub = get_redis().pubsub()
    canal = canal_de_salida(client_id, visitor_id)
    tareas: list[asyncio.Task[None]] = []
    try:
        await websocket.send_json({"type": "connected", "session": token})

        # Suscribirse ANTES de leer la base: lo publicado entre las dos cosas no se pierde.
        await pubsub.subscribe(canal)
        perdidos = await mensajes_perdidos(
            client_id, visitor_id, hello.last_message_id, settings.WEBCHAT_REPLAY_LIMIT
        )
        for frame in perdidos:
            await websocket.send_json(frame)

        ya_enviados = frozenset(str(f["message_id"]) for f in perdidos)
        tareas = [
            asyncio.create_task(_leer(websocket, client_id, visitor_id, hello.name, settings)),
            asyncio.create_task(_reenviar(websocket, pubsub, ya_enviados)),
        ]
        hechas, _ = await asyncio.wait(tareas, return_when=asyncio.FIRST_COMPLETED)
        for tarea in hechas:
            # Una desconexion del visitante no es un error; cualquier otra cosa si.
            if tarea.exception() and not isinstance(tarea.exception(), WebSocketDisconnect):
                logger.error(
                    "Webchat: la conexion termino con un error", exc_info=tarea.exception()
                )
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Webchat: error en la conexion (tenant %s)", client_id)
    finally:
        # Lo primero y sin `await`: si esta tarea se cancela durante la limpieza, el
        # cupo del visitante no puede quedar ocupado para siempre.
        restantes = _conexiones.get(visitor_id, 1) - 1
        if restantes > 0:
            _conexiones[visitor_id] = restantes
        else:
            _conexiones.pop(visitor_id, None)
        for tarea in tareas:
            tarea.cancel()
        await asyncio.gather(*tareas, return_exceptions=True)
        try:
            await pubsub.unsubscribe(canal)
            await pubsub.close()
        except Exception:
            logger.warning("Webchat: no se pudo cerrar la suscripcion de Redis", exc_info=True)
        # Puede que el visitante ya la haya cerrado.
        with contextlib.suppress(Exception):
            await websocket.close()
