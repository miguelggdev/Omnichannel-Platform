"""WebSocket de audio de las llamadas: `wss://host/api/v1/voice/stream` (Sprint 13).

Lo abre Twilio (Media Streams) cuando ejecuta el `<Connect><Stream>` del TwiML
que devolvio `/api/v1/voice/twilio/incoming`. Protocolo de Twilio, todo en
frames de texto JSON:

- `connected`: primer evento, sin datos utiles.
- `start`: `streamSid`, `callSid` y los `customParameters` del TwiML; aca llega
  el token que autentica el stream.
- `media`: 20 ms de audio mu-law 8 kHz del cliente, en base64.
- `mark`: Twilio termino de reproducir el audio que mandamos hasta esa marca.
- `stop`: la llamada termino.

Ciclo de una conexion
---------------------
1. Se acepta y se espera `start` unos segundos. Sin un token valido para ese
   `callSid` se cierra: cualquiera puede abrir un WebSocket a esta URL.
2. Un stream por llamada: el token no se puede reutilizar para abrir otro.
3. Tope de llamadas simultaneas por proceso y de duracion por llamada. Al
   cerrar de nuestro lado, Twilio sigue con el siguiente verbo del TwiML (el
   aviso de no disponibilidad).
4. `CallSession` (`app/services/voice/call_manager.py`) hace el resto.
"""

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.config import get_settings
from app.services.dedup import mark_if_new
from app.services.voice.call_manager import CallSession
from app.services.voice.stream_token import verificar_token

logger = logging.getLogger(__name__)

router = APIRouter()

CLOSE_TOKEN_INVALIDO = 4401
CLOSE_STREAM_DUPLICADO = 4409
CLOSE_DEMASIADAS_LLAMADAS = 4429
CLOSE_TIEMPO_AGOTADO = 4408

#: Espera maxima del evento `start` tras aceptar la conexion.
START_TIMEOUT_SECONDS = 10.0

#: Tope de un frame de Twilio (uno de `media` son ~300 bytes).
MAX_FRAME_BYTES = 64 * 1024

#: Llamadas con stream abierto en este proceso.
_llamadas_activas = 0


async def _esperar_start(websocket: WebSocket) -> dict[str, Any] | None:
    """Lee frames hasta el `start`, ignorando el `connected` previo.

    Args:
        websocket: Conexion aceptada.

    Returns:
        El evento `start`, o `None` si no llego a tiempo o no es valido.
    """
    limite = time.monotonic() + START_TIMEOUT_SECONDS
    while True:
        restante = limite - time.monotonic()
        if restante <= 0:
            return None
        try:
            crudo = await asyncio.wait_for(websocket.receive_text(), timeout=restante)
        except (TimeoutError, WebSocketDisconnect, KeyError):
            return None
        if len(crudo) > MAX_FRAME_BYTES:
            return None
        try:
            evento = json.loads(crudo)
        except ValueError:
            return None
        if not isinstance(evento, dict):
            return None
        if evento.get("event") == "start":
            return evento
        if evento.get("event") != "connected":
            return None


async def _atender(websocket: WebSocket, sesion: CallSession, duracion_maxima: float) -> str:
    """Reparte los eventos de Twilio a la sesion hasta que la llamada termine.

    Args:
        websocket: Conexion aceptada.
        sesion: Sesion de la llamada.
        duracion_maxima: Segundos que puede durar la llamada.

    Returns:
        El motivo del fin: `stop`, `disconnected` o `timeout`.
    """
    limite = time.monotonic() + duracion_maxima
    while True:
        restante = limite - time.monotonic()
        if restante <= 0:
            return "timeout"
        try:
            crudo = await asyncio.wait_for(websocket.receive_text(), timeout=restante)
        except TimeoutError:
            return "timeout"
        except (WebSocketDisconnect, KeyError):
            return "disconnected"
        if len(crudo) > MAX_FRAME_BYTES:
            continue
        try:
            evento = json.loads(crudo)
        except ValueError:
            continue
        if not isinstance(evento, dict):
            continue

        tipo = evento.get("event")
        if tipo == "media":
            media = evento.get("media") or {}
            # Solo el audio del cliente; `outbound` seria el nuestro de vuelta.
            if media.get("track", "inbound") == "inbound" and media.get("payload"):
                await sesion.on_media(str(media["payload"]))
        elif tipo == "mark":
            sesion.on_mark(str((evento.get("mark") or {}).get("name", "")))
        elif tipo == "stop":
            return "stop"


def _texto(valor: Any) -> str:
    """Devuelve el valor solo si de verdad venia como texto.

    Args:
        valor: Campo crudo del evento `start`.

    Returns:
        El texto, o `""` si no era una cadena.
    """
    return valor if isinstance(valor, str) else ""


@router.websocket("/stream")
async def media_stream(websocket: WebSocket) -> None:
    """WebSocket bidireccional de audio de una llamada de Twilio.

    Args:
        websocket: Conexion entrante (de Twilio).
    """
    global _llamadas_activas
    settings = get_settings()

    await websocket.accept()
    start = await _esperar_start(websocket)
    # Todo esto corre sin autenticar y fuera del `try` de mas abajo, asi que se
    # comprueban los tipos y no solo la presencia: un `start` con
    # `{"customParameters": {"token": 5}}` o `{"start": "abc"}` reventaba con un
    # `AttributeError` sin capturar, y el cierre 4401 nunca llegaba a correr.
    marco = start if isinstance(start, dict) else {}
    datos_start = marco.get("start")
    if not isinstance(datos_start, dict):
        datos_start = {}
    parametros = datos_start.get("customParameters")
    if not isinstance(parametros, dict):
        parametros = {}
    call_sid = _texto(datos_start.get("callSid"))
    stream_sid = _texto(datos_start.get("streamSid")) or _texto(marco.get("streamSid"))
    token = parametros.get("token")

    claims = verificar_token(token, call_sid) if call_sid and stream_sid else None
    if claims is None:
        logger.warning("Voz: stream rechazado, token ausente o invalido")
        await websocket.close(code=CLOSE_TOKEN_INVALIDO)
        return
    if not await mark_if_new("voice_stream", call_sid):
        logger.warning("Voz: segundo stream para la llamada %s", call_sid)
        await websocket.close(code=CLOSE_STREAM_DUPLICADO)
        return
    if _llamadas_activas >= settings.VOICE_MAX_CONCURRENT_CALLS:
        logger.warning("Voz: tope de llamadas simultaneas alcanzado; se rechaza %s", call_sid)
        await websocket.close(code=CLOSE_DEMASIADAS_LLAMADAS)
        return

    _llamadas_activas += 1
    sesion = CallSession(claims, stream_sid, websocket.send_json)
    motivo = "error"
    try:
        await sesion.start()
        motivo = await _atender(websocket, sesion, settings.VOICE_MAX_CALL_SECONDS)
    except Exception:
        logger.exception("Voz: error en el stream de la llamada %s", call_sid)
    finally:
        # Primero y sin `await`: si esta tarea se cancela al limpiar, el cupo no
        # puede quedar ocupado para siempre.
        _llamadas_activas -= 1
        try:
            await sesion.close(motivo)
        except Exception:
            logger.exception("Voz: error al cerrar la llamada %s", call_sid)
        if motivo != "disconnected":
            with contextlib.suppress(Exception):
                await websocket.close(code=CLOSE_TIEMPO_AGOTADO if motivo == "timeout" else 1000)
