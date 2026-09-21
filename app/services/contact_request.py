"""Flujo determinista para pedir y agradecer el telefono en Telegram.

Telegram no expone el telefono del usuario en sus mensajes; solo lo entrega si el
propio usuario lo comparte con un boton `request_contact`. Ese numero es lo que
permite unificar al mismo contacto entre canales (`phone_unification.py`).

Dos momentos, ninguno pasa por la IA (un LLM no debe decidir cuando pedir un dato
personal ni redactar el agradecimiento):

1. El usuario escribe `/vincular` (o `/link`): el bot le ofrece el boton. Es a
   peticion suya; el bot **no** lo pide por iniciativa propia en el primer
   mensaje, porque pedir un dato personal sin que nadie lo haya pedido es
   intrusivo y no tiene base de consentimiento.
2. El usuario comparte su numero: el bot agradece y retira el teclado. La
   respuesta es la misma haya o no unificacion: decir "te reconoci en WhatsApp"
   revelaria que existe otro contacto asociado a ese numero.

Los textos estan en espanol, igual que el resto de mensajes fijos del sistema
(`HANDOFF_MESSAGES`); la traduccion por tenant llega con el i18n del backend.
"""

from dataclasses import dataclass
from typing import Any

from app.schemas.message import NormalizedMessage
from app.services.phone_unification import telefono_verificado

#: Comandos con los que el usuario pide vincular su numero.
COMANDOS_VINCULAR = frozenset({"/vincular", "/link"})

TEXTO_SOLICITUD = (
    "Si quieres que te reconozcamos cuando nos escribas desde otro canal (por "
    "ejemplo WhatsApp), puedes compartir tu numero de telefono con el boton de "
    "abajo. Es opcional y solo se usa para eso."
)
ETIQUETA_BOTON = "📱 Compartir mi numero"
TEXTO_AGRADECIMIENTO = "Gracias, recibi tu numero. ¿En que te puedo ayudar?"


@dataclass(frozen=True)
class RespuestaFija:
    """Respuesta del sistema que no pasa por la IA.

    Attributes:
        text: Texto a enviar.
        metadata: Datos de canal para el envio (teclado de Telegram).
    """

    text: str
    metadata: dict[str, Any]


def _comando(texto: str | None) -> str | None:
    """Extrae el comando de un mensaje de Telegram.

    Args:
        texto: Texto del mensaje.

    Returns:
        El comando en minusculas y sin `@NombreDelBot`, o `None` si el mensaje no
        empieza por `/`.
    """
    if not texto:
        return None
    primero = texto.strip().split(maxsplit=1)[0] if texto.strip() else ""
    if not primero.startswith("/"):
        return None
    return primero.split("@", 1)[0].lower()


def respuesta_del_flujo_de_telefono(mensaje: NormalizedMessage) -> RespuestaFija | None:
    """Decide si el mensaje es parte del flujo de vinculo de telefono.

    Args:
        mensaje: Mensaje entrante normalizado.

    Returns:
        La respuesta fija que corresponde, o `None` si el mensaje sigue su camino
        normal hacia la IA. Solo aplica a Telegram.
    """
    if mensaje.channel.value != "telegram":
        return None
    # El agradecimiento exige un telefono que Telegram haya verificado; una
    # tarjeta ajena o reenviada llega sin `verified_phone` y sigue a la IA.
    if telefono_verificado(mensaje) is not None:
        return RespuestaFija(TEXTO_AGRADECIMIENTO, {"remove_keyboard": True})
    if _comando(mensaje.text) in COMANDOS_VINCULAR:
        return RespuestaFija(TEXTO_SOLICITUD, {"request_contact": ETIQUETA_BOTON})
    return None
