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
  Quien suplante el numero de un profesional no puede tener su acceso.
- **Email**: la cabecera `From` se falsifica sin esfuerzo.
- **Webchat**: la sesion es anonima; el identificador lo elige el navegador.

Un profesional que hoy dicta por telefono queda fuera hasta que la voz tenga
autenticacion propia (por ejemplo, un PIN por DTMF o una llamada de vuelta al
numero registrado): es una decision de producto anotada en PROGRESS.md.
"""

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
