"""Autenticacion de una llamada con un PIN por DTMF (Sprint 13, ADR-073).

El caller ID se puede falsificar, asi que una llamada no autoriza por si sola a
los agentes clinico y de marketing (`services/channel_identity.py`). Con esto un
profesional se autentica **durante la llamada**: teclea su PIN y `#`, y la
llamada queda autenticada como su contacto autorizado hasta que cuelgue.

Flujo
-----
1. El admin registra un PIN para un numero y un contacto autorizado
   (`PUT /api/v1/voice/pins`, `fijar_pin`).
2. En la llamada, Twilio manda cada tecla como un evento `dtmf` por el
   WebSocket; `CallSession` las junta hasta `#` y llama a `verificar_pin`.
3. Si acierta, `marcar_llamada_autenticada` deja en Redis
   `voice:auth:{client_id}:{call_sid} = contact_id`, con la duracion maxima de
   una llamada como caducidad.
4. Los nodos y tools de los agentes preguntan `profesional_de_la_llamada`:
   la identidad que cuenta en voz es esa, no el contacto que sale del caller ID.

Decisiones
----------
- **El PIN no viaja por Celery ni por Redis.** Se verifica en el proceso de la
  API, contra la base, y a Redis solo llega el id del contacto ya autenticado.
- **El PIN se mezcla con un HMAC de `ENCRYPTION_KEY` antes del bcrypt.** Un PIN
  de 6 digitos tiene 10^6 combinaciones; sin esto, quien tuviera solo la base la
  probaria offline en minutos. Con la clave del servidor de por medio, no.
- **Tres fallos por llamada y cinco seguidos por numero** (15 minutos de
  bloqueo). El limite por numero es el que frena a quien llama una y otra vez.
- **Un numero o PIN desconocido cuesta lo mismo que uno equivocado**: se hace un
  bcrypt igualmente, para que el tiempo no diga si el numero esta registrado.
- **Falla cerrado**: sin Redis, `profesional_de_la_llamada` devuelve `None` y el
  agente no atiende.
"""

import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.encryption import blind_index
from app.core.security import hash_password, verify_password
from app.models.voice_pin import VoicePin
from app.services.dedup import get_redis
from app.services.phone_unification import normalizar_telefono

logger = logging.getLogger(__name__)

PIN_MIN_DIGITOS = 6
PIN_MAX_DIGITOS = 8

#: Fallos seguidos, por numero, que bloquean los intentos.
MAX_FALLOS_SEGUIDOS = 5
BLOQUEO = timedelta(minutes=15)

#: Margen sobre la duracion maxima de una llamada para la clave de Redis.
_MARGEN_TTL_SEGUNDOS = 60

_SOLO_DIGITOS = re.compile(r"^\d+$")

_CLAVE_REDIS = "voice:auth:{client_id}:{call_sid}"

#: Hash de un PIN descartado, para igualar el tiempo cuando el numero no existe.
_HASH_SENUELO = hash_password(secrets.token_hex(8))


class PinInvalidoError(ValueError):
    """El PIN no cumple el formato o es demasiado predecible."""


def validar_formato_pin(pin: str) -> None:
    """Comprueba que el PIN sea de 6 a 8 digitos y no trivial.

    Args:
        pin: PIN en texto.

    Raises:
        PinInvalidoError: Si no son 6-8 digitos, si repite un mismo digito
            (`111111`) o si es una secuencia (`123456`, `654321`).
    """
    if not _SOLO_DIGITOS.match(pin) or not PIN_MIN_DIGITOS <= len(pin) <= PIN_MAX_DIGITOS:
        raise PinInvalidoError(
            f"El PIN debe tener entre {PIN_MIN_DIGITOS} y {PIN_MAX_DIGITOS} digitos"
        )
    if len(set(pin)) == 1:
        raise PinInvalidoError("El PIN no puede repetir un mismo digito")
    pasos = {int(b) - int(a) for a, b in pairwise(pin)}
    if pasos in ({1}, {-1}):
        raise PinInvalidoError("El PIN no puede ser una secuencia de digitos")


def preparar_pin(pin: str, client_id: UUID | str, contact_id: UUID | str) -> str:
    """Mezcla el PIN con un HMAC del servidor, antes del bcrypt.

    Args:
        pin: PIN en texto.
        client_id: Tenant.
        contact_id: Contacto al que autentica.

    Returns:
        El HMAC en hexadecimal (64 caracteres, dentro del limite de 72 bytes de bcrypt).
    """
    mensaje = f"voice-pin:{str(client_id).lower()}:{str(contact_id).lower()}:{pin}"
    return hmac.new(
        get_settings().ENCRYPTION_KEY.encode("utf-8"), mensaje.encode(), hashlib.sha256
    ).hexdigest()


def _hash_del_telefono(telefono: str, client_id: UUID | str) -> str | None:
    """Indice ciego del numero normalizado.

    Args:
        telefono: Numero tal como lo entrega Twilio o el admin.
        client_id: Tenant.

    Returns:
        El indice ciego, o `None` si no parece un telefono internacional.
    """
    normalizado = normalizar_telefono(telefono)
    return blind_index(normalizado, client_id) if normalizado else None


async def fijar_pin(
    session: AsyncSession, client_id: UUID, contact_id: UUID, telefono: str, pin: str
) -> None:
    """Registra (o reemplaza) el PIN de un numero, ligado a un contacto.

    Reemplazar reinicia el contador de fallos y el bloqueo.

    Args:
        session: Sesion con el contexto de tenant aplicado.
        client_id: Tenant.
        contact_id: Contacto autorizado al que autentica el PIN.
        telefono: Numero desde el que llamara.
        pin: PIN nuevo.

    Raises:
        PinInvalidoError: Si el PIN o el telefono no son validos.
    """
    validar_formato_pin(pin)
    huella = _hash_del_telefono(telefono, client_id)
    if huella is None:
        raise PinInvalidoError("El telefono debe estar en formato internacional (E.164)")
    pin_hash = await run_in_threadpool(hash_password, preparar_pin(pin, client_id, contact_id))
    existente = (
        await session.execute(
            select(VoicePin)
            .where(VoicePin.client_id == client_id, VoicePin.phone_hash == huella)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existente is None:
        session.add(
            VoicePin(
                client_id=client_id, contact_id=contact_id, phone_hash=huella, pin_hash=pin_hash
            )
        )
    else:
        existente.contact_id = contact_id
        existente.pin_hash = pin_hash
        existente.failed_attempts = 0
        existente.locked_until = None
    await session.flush()


async def quitar_pines(session: AsyncSession, client_id: UUID, contact_id: UUID) -> int:
    """Borra los PIN de un contacto (de todos sus numeros).

    Args:
        session: Sesion con el contexto de tenant aplicado.
        client_id: Tenant.
        contact_id: Contacto.

    Returns:
        Cuantos PIN se borraron.
    """
    filas = (
        (
            await session.execute(
                select(VoicePin).where(
                    VoicePin.client_id == client_id, VoicePin.contact_id == contact_id
                )
            )
        )
        .scalars()
        .all()
    )
    for fila in filas:
        await session.delete(fila)
    await session.flush()
    return len(filas)


async def verificar_pin(client_id: UUID, telefono: str, pin: str) -> UUID | None:
    """Comprueba el PIN tecleado desde un numero.

    Cuenta los fallos y bloquea el numero tras `MAX_FALLOS_SEGUIDOS`. Un numero
    sin PIN, uno bloqueado y un PIN equivocado devuelven lo mismo (`None`).

    Args:
        client_id: Tenant de la llamada.
        telefono: Numero de la persona que llama (el del caller ID).
        pin: PIN que tecleo.

    Returns:
        El contacto autenticado, o `None`.
    """
    huella = _hash_del_telefono(telefono, client_id)
    async with tenant_session(client_id) as session:
        fila = None
        if huella is not None:
            fila = (
                await session.execute(
                    select(VoicePin)
                    .where(VoicePin.client_id == client_id, VoicePin.phone_hash == huella)
                    .with_for_update()
                )
            ).scalar_one_or_none()

        bloqueado = (
            fila is not None
            and fila.locked_until is not None
            and fila.locked_until > datetime.now(timezone.utc)
        )
        objetivo = fila.pin_hash if fila is not None and not bloqueado else _HASH_SENUELO
        contacto = fila.contact_id if fila is not None else UUID(int=0)
        acierto = await run_in_threadpool(
            verify_password, preparar_pin(pin, client_id, contacto), objetivo
        )
        if fila is None or bloqueado:
            return None
        if acierto:
            fila.failed_attempts = 0
            fila.locked_until = None
            return fila.contact_id

        fila.failed_attempts += 1
        if fila.failed_attempts >= MAX_FALLOS_SEGUIDOS:
            fila.failed_attempts = 0
            fila.locked_until = datetime.now(timezone.utc) + BLOQUEO
            logger.warning("Voz: numero bloqueado por PIN incorrecto (tenant %s)", client_id)
        return None


def _clave_redis(client_id: UUID | str, call_sid: str) -> str:
    """Clave de Redis de una llamada autenticada.

    Args:
        client_id: Tenant.
        call_sid: `CallSid` de Twilio.

    Returns:
        La clave.
    """
    return _CLAVE_REDIS.format(client_id=str(client_id).lower(), call_sid=call_sid)


async def marcar_llamada_autenticada(
    client_id: UUID | str, call_sid: str, contact_id: UUID
) -> None:
    """Deja constancia de que la llamada esta autenticada como un contacto.

    Args:
        client_id: Tenant.
        call_sid: `CallSid` de Twilio.
        contact_id: Contacto autenticado.
    """
    ttl = get_settings().VOICE_MAX_CALL_SECONDS + _MARGEN_TTL_SEGUNDOS
    await get_redis().set(_clave_redis(client_id, call_sid), str(contact_id), ex=ttl)


async def profesional_de_la_llamada(client_id: UUID | str, call_sid: str | None) -> UUID | None:
    """Contacto con el que se autentico la llamada, si lo hizo.

    Falla cerrado: sin `call_sid`, sin clave o con Redis caido devuelve `None`.

    Args:
        client_id: Tenant.
        call_sid: `CallSid` de la llamada, si se conoce.

    Returns:
        El contacto autenticado, o `None`.
    """
    if not call_sid:
        return None
    try:
        valor = await get_redis().get(_clave_redis(client_id, call_sid))
        return UUID(valor) if valor else None
    except Exception:
        logger.warning("Voz: no se pudo consultar la autenticacion de la llamada", exc_info=True)
        return None


def call_sid_de_mensaje(external_message_id: str | None) -> str | None:
    """`CallSid` de un mensaje de voz (`CallSid:indice`).

    Args:
        external_message_id: Id externo del mensaje.

    Returns:
        El `CallSid`, o `None` si el id no tiene esa forma.
    """
    if not external_message_id or ":" not in external_message_id:
        return None
    return external_message_id.split(":", 1)[0] or None
