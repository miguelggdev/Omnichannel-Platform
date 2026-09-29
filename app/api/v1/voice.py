"""Endpoints HTTP del canal de voz (Sprint 13, Dev A).

POST /api/v1/voice/twilio/incoming   TwiML de una llamada (entrante o saliente contestada)
POST /api/v1/voice/twilio/status     Status callback de Twilio
POST /api/v1/voice/calls             Inicia una llamada saliente     (JWT: super_admin, admin)
GET  /api/v1/voice/calls             Llamadas del tenant             (JWT: super_admin, admin, agent)
GET  /api/v1/voice/calls/{id}        Una llamada con su transcripcion (JWT: idem)

Los dos `twilio/*` no llevan JWT (`TenantContextMiddleware` exime
`VOICE_WEBHOOK_PATHS_PREFIX`): se autentican con `X-Twilio-Signature`, que
cubre la URL publica completa y todos los parametros.

Lo que el spec hacia distinto (§6)
----------------------------------
- **Sin validar la firma.** El spec la implementa en el provider pero sus
  endpoints nunca la llaman: cualquiera podia "colgar" llamadas ajenas o, peor,
  pedir TwiML.
- **El `wss://` salia del `Host` del request** (`request.url.hostname`). El
  `Host` lo controla quien hace el request: con un `Host` falso, el TwiML
  mandaba a Twilio a transmitir el audio de la llamada a otro servidor. Aca la
  URL sale de `VOICE_PUBLIC_BASE_URL`, que ademas es la que firma Twilio.
- **El TwiML se armaba interpolando texto sin escapar** en XML.
- **La autenticacion del stream** es un token firmado con vencimiento
  (`stream_token.py`), no el `CallSid` en el path.

Tenant: `DEFAULT_CLIENT_ID`, como el resto de canales (ADR-030). Por eso una
llamada saliente solo la puede pedir un usuario de ese tenant: el numero de
Twilio es de el.
"""

import html
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from app.agents.nodes._tenant import ChannelNotConfiguredError, get_channel_config
from app.core.config import Settings, get_settings
from app.core.database import tenant_session
from app.core.dependencies import require_role
from app.core.encryption import mask_identifier
from app.core.exceptions import FORBIDDEN, NOT_FOUND, AppException
from app.models.call_record import CALL_FINAL_STATUSES, CallRecord
from app.schemas.voice import (
    CallRecordListResponse,
    CallRecordResponse,
    VoiceCallAccepted,
    VoiceCallCreate,
)
from app.services.messaging.voice_provider import (
    TwilioAPIError,
    TwilioVoiceProvider,
    faltan_credenciales_salientes,
    numero_del_contacto,
)
from app.services.voice.stream_token import emitir_token
from app.tasks.voice_tasks import direccion_reconocida, normalizar_direccion

logger = logging.getLogger(__name__)

router = APIRouter()

INVALID_SIGNATURE = "INVALID_WEBHOOK_SIGNATURE"
VOICE_NOT_CONFIGURED = "VOICE_NOT_CONFIGURED"
TWILIO_ERROR = "TWILIO_ERROR"

#: Tope del cuerpo de un webhook de Twilio (son formularios de pocos KB).
MAX_BODY_BYTES = 64 * 1024

_LECTURA = ("super_admin", "admin", "agent")
_LLAMAR = ("super_admin", "admin")


# ─── Utilidades ──────────────────────────────────────────────────────────────


def _tenant(settings: Settings) -> str:
    """Tenant del canal de voz.

    Args:
        settings: Configuracion.

    Returns:
        `DEFAULT_CLIENT_ID` como texto.

    Raises:
        AppException: 503 si no esta configurado.
    """
    try:
        return str(UUID(settings.DEFAULT_CLIENT_ID))
    except (ValueError, AttributeError) as exc:
        raise AppException(503, VOICE_NOT_CONFIGURED, "Canal de voz no configurado") from exc


def _url_publica(request: Request, settings: Settings) -> str:
    """URL publica exacta del request, tal como la firmo Twilio.

    Args:
        request: Request entrante.
        settings: Configuracion.

    Returns:
        `VOICE_PUBLIC_BASE_URL` + path + query.

    Raises:
        AppException: 503 si `VOICE_PUBLIC_BASE_URL` no esta configurada.
    """
    base = settings.VOICE_PUBLIC_BASE_URL.rstrip("/")
    if not base:
        logger.error("Voz: VOICE_PUBLIC_BASE_URL no configurada")
        raise AppException(503, VOICE_NOT_CONFIGURED, "Canal de voz no configurado")
    consulta = f"?{request.url.query}" if request.url.query else ""
    return f"{base}{request.url.path}{consulta}"


async def _parametros_firmados(request: Request, settings: Settings) -> dict[str, str]:
    """Lee el formulario de Twilio y valida su firma.

    Args:
        request: Request de Twilio.
        settings: Configuracion.

    Returns:
        Los parametros del formulario.

    Raises:
        AppException: 413 si el cuerpo es demasiado grande; 401 si la firma no
            es valida (o el canal no tiene `TWILIO_AUTH_TOKEN`).
    """
    declarado = request.headers.get("content-length", "")
    if declarado.isdigit() and int(declarado) > MAX_BODY_BYTES:
        raise AppException(413, "PAYLOAD_TOO_LARGE", "El cuerpo del webhook es demasiado grande")
    cuerpo = await request.body()
    if len(cuerpo) > MAX_BODY_BYTES:
        raise AppException(413, "PAYLOAD_TOO_LARGE", "El cuerpo del webhook es demasiado grande")

    provider = TwilioVoiceProvider({"url": _url_publica(request, settings)})
    firma = request.headers.get("x-twilio-signature", "")
    if not await provider.validate_signature(cuerpo, firma, settings.TWILIO_AUTH_TOKEN):
        logger.warning("Voz: firma de Twilio invalida en %s", request.url.path)
        raise AppException(401, INVALID_SIGNATURE, "Firma de webhook invalida")
    return dict(parse_qsl(cuerpo.decode("utf-8"), keep_blank_values=True))


def _wss(settings: Settings) -> str:
    """URL del WebSocket de audio, derivada de la URL publica.

    Args:
        settings: Configuracion.

    Returns:
        `wss://host/api/v1/voice/stream` (o `ws://` si la base es `http://`).
    """
    base = settings.VOICE_PUBLIC_BASE_URL.rstrip("/")
    esquema, _, resto = base.partition("://")
    return f"{'wss' if esquema == 'https' else 'ws'}://{resto}/api/v1/voice/stream"


def _texto_xml(valor: str) -> str:
    """Escapa texto para el contenido de un elemento TwiML.

    `html.escape` y no `xml.sax.saxutils`: el resultado es el mismo escapado
    valido en XML, sin importar un modulo de parseo XML (bandit B406) en un
    endpoint que solo genera XML y nunca lo lee.

    Args:
        valor: Texto a escapar.

    Returns:
        El texto con `&`, `<` y `>` escapados.
    """
    return html.escape(valor, quote=False)


def _atributo_xml(valor: str) -> str:
    """Escapa y entrecomilla un valor de atributo TwiML.

    Args:
        valor: Valor del atributo.

    Returns:
        El valor entre comillas dobles, con comillas y `&<>` escapados.
    """
    return f'"{html.escape(valor, quote=True)}"'


def twiml_de_llamada(settings: Settings, saludo: str, token: str) -> str:
    """TwiML que saluda, conecta el stream de audio y se despide si se corta.

    Si el stream lo cierra la plataforma (error, tope de llamadas o de
    duracion), Twilio sigue con el siguiente verbo: el aviso de no
    disponibilidad. Si cuelga el cliente, no hay nada mas que decir.

    Args:
        settings: Configuracion.
        saludo: Texto del saludo.
        token: Token del stream (`stream_token.emitir_token`).

    Returns:
        El documento TwiML.
    """
    voz = f"language={_atributo_xml(settings.VOICE_TWIML_LANGUAGE)} voice={_atributo_xml(settings.VOICE_TWIML_VOICE)}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Say {voz}>{_texto_xml(saludo)}</Say>"
        f"<Connect><Stream url={_atributo_xml(_wss(settings))}>"
        f'<Parameter name="token" value={_atributo_xml(token)}/>'
        "</Stream></Connect>"
        f"<Say {voz}>{_texto_xml(settings.VOICE_UNAVAILABLE_MESSAGE)}</Say>"
        "</Response>"
    )


async def _encolar_registro(client_id: str, call_sid: str, datos: dict[str, Any]) -> None:
    """Encola la persistencia de un evento de llamada sin bloquear la respuesta.

    Un fallo de la cola no tumba el webhook: el TwiML (o el 204) importa mas
    que la fila, y el status callback final vuelve a escribirla.

    Args:
        client_id: Tenant.
        call_sid: Llamada.
        datos: Columnas que aporta el evento.
    """
    from app.tasks.voice_tasks import save_call_record

    try:
        await run_in_threadpool(
            save_call_record.delay, client_id=client_id, call_sid=call_sid, datos=datos
        )
    except Exception:
        logger.exception("Voz: no se pudo encolar el registro de %s", call_sid)


def _entero(valor: str | None) -> int | None:
    """Convierte un campo numerico de Twilio.

    Args:
        valor: Texto del formulario.

    Returns:
        El entero, o `None` si falta o no es numerico.
    """
    return int(valor) if valor and valor.isdigit() else None


# ─── Webhooks de Twilio ──────────────────────────────────────────────────────


@router.post("/twilio/incoming", include_in_schema=False)
async def twilio_incoming(request: Request) -> Response:
    """Responde el TwiML de una llamada: saludo y stream de audio.

    Twilio lo pide al entrar una llamada al numero, y tambien al contestar una
    llamada saliente (`Url` de `start_call()`); `Direction` los distingue.

    Args:
        request: Webhook firmado de Twilio.

    Returns:
        TwiML (`application/xml`).
    """
    settings = get_settings()
    params = await _parametros_firmados(request, settings)
    client_id = _tenant(settings)

    call_sid = params.get("CallSid", "")
    contacto = numero_del_contacto(params)
    if not call_sid or not contacto:
        raise AppException(400, "INVALID_WEBHOOK", "Falta CallSid o el numero del cliente")

    direccion = normalizar_direccion(params.get("Direction"))
    token = emitir_token(
        client_id=client_id,
        call_sid=call_sid,
        direction=direccion,
        phone_from=params.get("From", ""),
        phone_to=params.get("To", ""),
        contact_phone=contacto,
    )
    await _encolar_registro(
        client_id,
        call_sid,
        {
            "direction": direccion,
            "status": "in-progress",
            "phone_from": params.get("From") or None,
            "phone_to": params.get("To") or None,
        },
    )
    saludo = (
        settings.VOICE_OUTBOUND_WELCOME_MESSAGE
        if direccion == "outbound"
        else settings.VOICE_WELCOME_MESSAGE
    )
    logger.info("Voz: llamada %s (%s) conectando stream", call_sid, direccion)
    return Response(content=twiml_de_llamada(settings, saludo, token), media_type="application/xml")


@router.post("/twilio/status", include_in_schema=False, status_code=204)
async def twilio_status(request: Request) -> Response:
    """Status callback: estado, duracion y grabacion de la llamada.

    Args:
        request: Webhook firmado de Twilio.

    Returns:
        204 sin cuerpo.
    """
    settings = get_settings()
    params = await _parametros_firmados(request, settings)
    client_id = _tenant(settings)

    call_sid = params.get("CallSid", "")
    estado = params.get("CallStatus", "")
    if not call_sid or not estado:
        return Response(status_code=204)

    datos: dict[str, Any] = {
        "status": estado,
        "direction": direccion_reconocida(params.get("Direction")),
        "phone_from": params.get("From") or None,
        "phone_to": params.get("To") or None,
        "duration_seconds": _entero(params.get("CallDuration")),
        "recording_url": params.get("RecordingUrl") or None,
        "recording_duration": _entero(params.get("RecordingDuration")),
    }
    if estado in CALL_FINAL_STATUSES:
        datos["ended_at"] = datetime.now(timezone.utc).isoformat()
    await _encolar_registro(client_id, call_sid, datos)
    return Response(status_code=204)


# ─── API del CRM ─────────────────────────────────────────────────────────────


@router.post("/calls", status_code=202, response_model=VoiceCallAccepted)
async def start_outbound_call(
    data: VoiceCallCreate,
    user: dict[str, Any] = Depends(require_role(*_LLAMAR)),
) -> VoiceCallAccepted:
    """Inicia una llamada saliente; el agente habla cuando el cliente contesta.

    Args:
        data: Numero a llamar.
        user: Usuario autenticado.

    Returns:
        El `CallSid` de la llamada.

    Raises:
        AppException: 403 si el usuario no es del tenant del canal de voz;
            503 si el canal no esta configurado; 502 si Twilio la rechaza.
    """
    settings = get_settings()
    client_id = _tenant(settings)
    if str(user["client_id"]) != client_id:
        raise AppException(403, FORBIDDEN, "El canal de voz no esta habilitado para este tenant")
    try:
        _, config = get_channel_config("voice")
    except ChannelNotConfiguredError as exc:
        raise AppException(503, VOICE_NOT_CONFIGURED, "Canal de voz no configurado") from exc
    # `get_channel_config("voice")` solo exige `client_id`, que es lo que hace
    # falta para responder dentro de una llamada. Llamar hacia fuera si necesita
    # las credenciales REST, y que falten es un problema de configuracion (503),
    # no un rechazo de Twilio (502).
    if faltan_credenciales_salientes(config):
        raise AppException(503, VOICE_NOT_CONFIGURED, "Canal de voz no configurado")
    base = settings.VOICE_PUBLIC_BASE_URL.rstrip("/")
    if not base:
        raise AppException(503, VOICE_NOT_CONFIGURED, "Canal de voz no configurado")

    try:
        call_sid = await TwilioVoiceProvider().start_call(
            data.to,
            config,
            answer_url=f"{base}/api/v1/voice/twilio/incoming",
            status_callback_url=f"{base}/api/v1/voice/twilio/status",
        )
    except TwilioAPIError as exc:
        logger.warning("Voz: Twilio rechazo una llamada saliente: %s", exc)
        raise AppException(502, TWILIO_ERROR, "Twilio no pudo iniciar la llamada") from exc

    logger.info("Voz: llamada saliente %s pedida por %s", call_sid, user["user_id"])
    return VoiceCallAccepted(call_sid=call_sid, status="queued")


def _a_respuesta(registro: CallRecord, *, con_transcripcion: bool) -> CallRecordResponse:
    """Convierte una fila en su representacion, con los telefonos enmascarados.

    Args:
        registro: Fila de `call_records`.
        con_transcripcion: Si se incluye la transcripcion (solo en el detalle).

    Returns:
        La llamada.
    """
    return CallRecordResponse(
        id=registro.id,
        call_sid=registro.call_sid,
        contact_id=registro.contact_id,
        conversation_id=registro.conversation_id,
        direction=registro.direction,
        status=registro.status,
        phone_from=mask_identifier(registro.phone_from) if registro.phone_from else None,
        phone_to=mask_identifier(registro.phone_to) if registro.phone_to else None,
        started_at=registro.started_at,
        ended_at=registro.ended_at,
        duration_seconds=registro.duration_seconds,
        transcript=registro.transcript if con_transcripcion else [],
        recording_url=registro.recording_url,
    )


@router.get("/calls", response_model=CallRecordListResponse)
async def list_calls(
    contact_id: UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_role(*_LECTURA)),
) -> CallRecordListResponse:
    """Lista las llamadas del tenant, de la mas reciente a la mas vieja.

    Args:
        contact_id: Filtra por contacto.
        page: Pagina, desde 1.
        page_size: Tamano de pagina.
        user: Usuario autenticado.

    Returns:
        La pagina, sin transcripciones.
    """
    client_id = UUID(str(user["client_id"]))
    filtros = [CallRecord.client_id == client_id]
    if contact_id is not None:
        filtros.append(CallRecord.contact_id == contact_id)

    async with tenant_session(client_id) as session:
        total = await session.scalar(select(func.count()).select_from(CallRecord).where(*filtros))
        registros = (
            (
                await session.execute(
                    select(CallRecord)
                    .where(*filtros)
                    .order_by(CallRecord.started_at.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        items = [_a_respuesta(r, con_transcripcion=False) for r in registros]

    return CallRecordListResponse(
        items=items, total=int(total or 0), page=page, page_size=page_size
    )


@router.get("/calls/{call_record_id}", response_model=CallRecordResponse)
async def get_call(
    call_record_id: UUID,
    user: dict[str, Any] = Depends(require_role(*_LECTURA)),
) -> CallRecordResponse:
    """Devuelve una llamada con su transcripcion.

    Args:
        call_record_id: Registro buscado.
        user: Usuario autenticado.

    Returns:
        La llamada.

    Raises:
        AppException: 404 si no existe en el tenant.
    """
    client_id = UUID(str(user["client_id"]))
    async with tenant_session(client_id) as session:
        registro = (
            await session.execute(
                select(CallRecord).where(
                    CallRecord.id == call_record_id, CallRecord.client_id == client_id
                )
            )
        ).scalar_one_or_none()
        if registro is None:
            raise AppException(404, NOT_FOUND, "Llamada no encontrada")
        return _a_respuesta(registro, con_transcripcion=True)
