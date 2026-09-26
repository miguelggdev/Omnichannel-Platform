"""Token que autentica el stream de audio de una llamada (Sprint 13, Dev A).

El WebSocket de Media Streams lo abre Twilio, pero cualquiera puede abrir un
WebSocket a la misma URL. El spec la protege con el `call_id` en el path, que
no es secreto (Twilio lo manda en cada webhook, y acaba en logs), y toma el
tenant de un parametro sin firmar: cualquiera podia inyectar audio en nombre
de cualquier numero.

Aqui el webhook de llamada entrante (firmado por Twilio) emite un token que
viaja dentro del TwiML como `<Parameter>` del `<Stream>`. Twilio lo devuelve en
el evento `start` del WebSocket, que es donde se verifica. No va en la URL: una
URL acaba en los logs de acceso de cada proxy.

El token lleva todo lo que la conexion necesita saber de la llamada y que no
puede venir del propio WebSocket: tenant, `CallSid`, direccion y numeros.
Caduca en segundos (`VOICE_STREAM_TOKEN_TTL_SECONDS`) porque Twilio abre el
stream apenas recibe el TwiML.

La clave se deriva de `JWT_SECRET` con una etiqueta propia, como la sesion del
Webchat: un token de voz no sirve como JWT ni como sesion de Webchat, ni al
reves.
"""

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import asdict, dataclass

from app.core.config import get_settings

_ETIQUETA = b"omnichannel:voice-stream:v1"


@dataclass(frozen=True)
class StreamClaims:
    """Lo que el token afirma de la llamada.

    Attributes:
        client_id: Tenant.
        call_sid: `CallSid` de Twilio.
        direction: `inbound` u `outbound`.
        phone_from: Numero de origen.
        phone_to: Numero de destino.
        contact_phone: Numero del cliente (origen o destino segun la direccion).
        exp: Caducidad, en segundos desde epoch.
    """

    client_id: str
    call_sid: str
    direction: str
    phone_from: str
    phone_to: str
    contact_phone: str
    exp: int


def _clave() -> bytes:
    """Deriva la clave de firma de `JWT_SECRET`.

    Returns:
        32 bytes.
    """
    return hmac.new(get_settings().JWT_SECRET.encode("utf-8"), _ETIQUETA, hashlib.sha256).digest()


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode("ascii")


def _desde_b64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def emitir_token(
    *,
    client_id: str,
    call_sid: str,
    direction: str,
    phone_from: str,
    phone_to: str,
    contact_phone: str,
    ahora: float | None = None,
) -> str:
    """Emite el token del stream de una llamada.

    Args:
        client_id: Tenant.
        call_sid: `CallSid` de Twilio.
        direction: `inbound` u `outbound`.
        phone_from: Numero de origen.
        phone_to: Numero de destino.
        contact_phone: Numero del cliente.
        ahora: Instante de emision (para tests).

    Returns:
        `payload.firma`, ambos en base64url.
    """
    emitido = ahora if ahora is not None else time.time()
    caduca = int(emitido) + get_settings().VOICE_STREAM_TOKEN_TTL_SECONDS
    claims = StreamClaims(
        client_id=client_id,
        call_sid=call_sid,
        direction=direction,
        phone_from=phone_from,
        phone_to=phone_to,
        contact_phone=contact_phone,
        exp=caduca,
    )
    payload = _b64(json.dumps(asdict(claims), separators=(",", ":")).encode("utf-8"))
    firma = _b64(hmac.new(_clave(), payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{firma}"


def verificar_token(
    token: str | None, call_sid: str, ahora: float | None = None
) -> StreamClaims | None:
    """Verifica el token que Twilio devolvio en el evento `start`.

    Args:
        token: Token recibido.
        call_sid: `callSid` del evento `start`: tiene que ser la misma llamada
            para la que se emitio el token.
        ahora: Instante de verificacion (para tests).

    Returns:
        Las afirmaciones del token, o `None` si no es valido, caduco o es de
        otra llamada.
    """
    if not token or token.count(".") != 1:
        return None
    payload, firma = token.split(".")
    esperada = _b64(hmac.new(_clave(), payload.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(firma.encode("ascii", "ignore"), esperada.encode("ascii")):
        return None
    try:
        claims = StreamClaims(**json.loads(_desde_b64(payload)))
    except (binascii.Error, ValueError, TypeError):
        return None
    if claims.exp < (ahora if ahora is not None else time.time()):
        return None
    if not hmac.compare_digest(claims.call_sid.encode("utf-8"), call_sid.encode("utf-8")):
        return None
    return claims
