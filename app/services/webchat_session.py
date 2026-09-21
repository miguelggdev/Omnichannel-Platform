"""Sesion firmada del visitante de Webchat.

El visitante es anonimo: no hay login. Su identidad es un `visitor_id` aleatorio que
el servidor le asigna la primera vez y que el navegador guarda (localStorage) para
reconectar y ver su conversacion.

Dos cosas distintas a proposito:

- `visitor_id` es el **identificador de canal**: lo que se cifra en
  `contact_identifiers` y a donde se enruta cada respuesta. Lo ven los agentes en la
  bandeja; no sirve por si solo para conectarse.
- El **token de sesion** es lo que prueba ser ese visitante: `visitor_id`, una
  caducidad y una firma HMAC ligada al tenant. Sin la firma, conocer un `visitor_id`
  (o adivinarlo) no permite leer la conversacion de otro ni escribir en su nombre.

La clave de firma se deriva de `JWT_SECRET` con una etiqueta propia, de modo que un
token de Webchat no se pueda confundir con un JWT de la aplicacion ni al reves.
"""

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from uuid import UUID

from app.core.config import get_settings

_ETIQUETA = b"omnichannel:webchat-session:v1"


@dataclass(frozen=True)
class Sesion:
    """Sesion de un visitante.

    Attributes:
        visitor_id: Identificador de canal del visitante.
        token: Token que el cliente guarda y presenta al reconectar.
    """

    visitor_id: str
    token: str


def _clave() -> bytes:
    """Deriva la clave de firma de `JWT_SECRET`.

    Returns:
        32 bytes.
    """
    return hmac.new(get_settings().JWT_SECRET.encode("utf-8"), _ETIQUETA, hashlib.sha256).digest()


def _firmar(client_id: UUID, visitor_id: str, caduca: int) -> str:
    """Firma `(tenant, visitante, caducidad)`.

    Args:
        client_id: Tenant.
        visitor_id: Visitante.
        caduca: Momento de caducidad (epoch, segundos).

    Returns:
        Firma en base64 url-safe sin relleno.
    """
    mensaje = f"{client_id}:{visitor_id}:{caduca}".encode()
    firma = hmac.new(_clave(), mensaje, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(firma).rstrip(b"=").decode("ascii")


def emitir_sesion(client_id: UUID, ahora: float | None = None) -> Sesion:
    """Crea un visitante nuevo y su token.

    Args:
        client_id: Tenant del canal.
        ahora: Momento actual (epoch); solo para los tests.

    Returns:
        La sesion, con un `visitor_id` aleatorio de 128 bits.
    """
    visitor_id = secrets.token_hex(16)
    dias = get_settings().WEBCHAT_SESSION_TTL_DAYS
    caduca = int((time.time() if ahora is None else ahora) + dias * 86400)
    return Sesion(visitor_id, f"{visitor_id}.{caduca}.{_firmar(client_id, visitor_id, caduca)}")


def verificar_sesion(token: str | None, client_id: UUID, ahora: float | None = None) -> str | None:
    """Valida un token de sesion.

    Args:
        token: Token presentado por el cliente.
        client_id: Tenant del canal (la firma va ligada a el).
        ahora: Momento actual (epoch); solo para los tests.

    Returns:
        El `visitor_id` si la firma es correcta y no caduco; `None` en cualquier otro
        caso (mal formado, firma ajena, otro tenant, caducado). Nunca lanza.
    """
    if not token:
        return None
    partes = token.split(".")
    if len(partes) != 3:
        return None
    visitor_id, caduca_txt, firma = partes
    if not (len(visitor_id) == 32 and caduca_txt.isdigit()):
        return None
    caduca = int(caduca_txt)
    esperada = _firmar(client_id, visitor_id, caduca)
    if not hmac.compare_digest(firma.encode("ascii", "ignore"), esperada.encode("ascii")):
        return None
    if caduca < (time.time() if ahora is None else ahora):
        return None
    return visitor_id
