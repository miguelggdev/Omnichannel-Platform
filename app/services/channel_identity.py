"""Que canales identifican de verdad a quien escribe (ADR-072).

Los agentes que operan sobre datos de otras personas —el clinico y el de
marketing— autorizan por `contact_id`: solo atienden a los contactos que el
tenant declaro como profesionales u operadores. Eso solo vale si el canal
demuestra quien es el contacto:

- **WhatsApp, Instagram, Facebook y Telegram**: la plataforma autentica la
  cuenta (numero verificado por Meta, PSID, id de usuario de Telegram); el
  identificador no lo puede fijar el remitente.
- **Voz**: el contacto sale del `From` de Twilio (caller ID), que se puede
  falsificar; `voice_provider.py` ya evita usarlo como telefono verificado.
  Por si solo no autoriza. Lo que si vale es el **PIN por DTMF** que teclea el
  profesional durante la llamada (`services/voice/pin_auth.py`, ADR-073): deja
  la llamada autenticada como un contacto y esa es la identidad que cuenta.
- **Email**: la cabecera `From` se falsifica sin esfuerzo.
- **Webchat**: la sesion es anonima; el identificador lo elige el navegador.

Un profesional que dicta por telefono solo entra si tecleo su PIN en esa llamada.
"""

from typing import Any
from uuid import UUID

from app.services.voice.pin_auth import call_sid_de_mensaje, profesional_de_la_llamada

#: Canales cuya identidad de contacto no puede fijar el remitente.
CANALES_CON_IDENTIDAD_VERIFICADA: frozenset[str] = frozenset(
    {"whatsapp", "instagram", "facebook", "telegram"}
)


def identidad_verificada(channel: str | None) -> bool:
    """Si el canal autentica al contacto, y por tanto sirve para autorizar.

    Args:
        channel: Canal de la conversacion (`whatsapp`, `voice`, ...), si se conoce.

    Returns:
        `True` solo para los canales de `CANALES_CON_IDENTIDAD_VERIFICADA`. Un
        canal desconocido o ausente no autoriza: es el lado seguro.
    """
    return channel in CANALES_CON_IDENTIDAD_VERIFICADA


async def contacto_autenticado(
    *,
    channel: str | None,
    client_id: UUID | str,
    contact_id: Any,
    external_message_id: str | None = None,
    call_sid: str | None = None,
) -> str | None:
    """Contacto con el que se autentico quien escribe o llama, si lo hizo.

    - Canales con identidad verificada: el contacto de la conversacion.
    - Voz: el contacto con el que la llamada se autentico por PIN (puede ser
      otro que el del caller ID: el contacto de voz no se une con el de WhatsApp).
    - Cualquier otro canal, o voz sin PIN: `None`.

    Args:
        channel: Canal de la conversacion.
        client_id: Tenant.
        contact_id: Contacto de la conversacion (UUID o texto), si lo hay.
        external_message_id: Id externo del mensaje (`CallSid:indice` en voz).
        call_sid: `CallSid`, si ya se conoce; si no, sale del id del mensaje.

    Returns:
        El id del contacto autenticado, en texto, o `None` si no hay identidad
        que valga.
    """
    if identidad_verificada(channel):
        return str(contact_id) if contact_id else None
    if channel == "voice":
        contacto = await profesional_de_la_llamada(
            client_id, call_sid or call_sid_de_mensaje(external_message_id)
        )
        return str(contacto) if contacto else None
    return None
