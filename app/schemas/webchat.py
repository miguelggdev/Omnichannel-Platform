"""Frames del WebSocket de Webchat (Sprint 9).

Protocolo, en JSON de texto (un objeto por frame):

Cliente -> servidor
    `hello`        primer frame obligatorio: `{"type":"hello","session":<token|null>,
                   "last_message_id":<id|null>,"name":<texto|null>}`. La sesion viaja
                   aqui y no en la URL: una URL queda en los logs de acceso de cada
                   proxy, y la sesion es un secreto al portador.
    `message`      `{"type":"message","message_id":<id del cliente>,"text":...}`
    `button_reply` respuesta a un boton de un mensaje del bot.
    `ping`         keepalive.

Servidor -> cliente
    `connected`    `{"type":"connected","session":<token>}`
    `message`      mensaje del bot o de un agente humano.
    `ack`          el mensaje del cliente se acepto (o ya se habia recibido).
    `error`        `{"type":"error","code":...,"message":...}`
    `pong`

Los limites de tamano no se declaran aqui: los aplica el endpoint contra
`Settings` para que se puedan cambiar sin tocar el esquema.
"""

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Identificador de mensaje que elige el cliente (un UUID, normalmente).
_ID_CLIENTE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

#: Caracteres de control salvo salto de linea y tabulador.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def limpiar_texto(valor: str) -> str:
    """Quita los caracteres de control de un texto escrito por un visitante.

    Args:
        valor: Texto crudo.

    Returns:
        El texto sin caracteres de control (conserva saltos de linea y tabulaciones).
    """
    return _CONTROL.sub("", valor)


class _Frame(BaseModel):
    """Base: rechaza campos desconocidos (un frame mal formado es un error)."""

    model_config = ConfigDict(extra="forbid")


class HelloFrame(_Frame):
    """Primer frame del cliente.

    Attributes:
        type: Siempre `hello`.
        session: Token de sesion de una conexion anterior, o `None` para una nueva.
        last_message_id: Ultimo `message_id` que el cliente recibio, para recuperar
            lo que se perdio mientras estuvo desconectado.
        name: Nombre que el visitante da de si mismo (opcional).
    """

    type: Literal["hello"]
    session: str | None = Field(default=None, max_length=512)
    last_message_id: str | None = Field(default=None, max_length=256)
    name: str | None = Field(default=None, max_length=100)

    @field_validator("name")
    @classmethod
    def _nombre_limpio(cls, v: str | None) -> str | None:
        """Quita controles y espacios; un nombre vacio pasa a `None`."""
        if v is None:
            return None
        limpio = limpiar_texto(v).strip()
        return limpio or None


class MessageFrame(_Frame):
    """Mensaje de texto del visitante.

    Attributes:
        type: Siempre `message`.
        message_id: Identificador que elige el cliente; el servidor lo antepone al
            del visitante para que dos visitantes no puedan chocar en la dedup.
        text: Texto del mensaje (el tope real lo aplica el endpoint).
    """

    type: Literal["message"]
    message_id: str
    text: str

    @field_validator("message_id")
    @classmethod
    def _id_valido(cls, v: str) -> str:
        """Solo caracteres seguros para una clave de Redis."""
        if not _ID_CLIENTE.fullmatch(v):
            raise ValueError("message_id invalido")
        return v

    @field_validator("text")
    @classmethod
    def _texto_limpio(cls, v: str) -> str:
        """Quita controles; un texto vacio o solo espacios se rechaza."""
        limpio = limpiar_texto(v).strip()
        if not limpio:
            raise ValueError("el mensaje no puede estar vacio")
        return limpio


class ButtonReplyFrame(_Frame):
    """Pulsacion de un boton que el bot ofrecio.

    Attributes:
        type: Siempre `button_reply`.
        message_id: Identificador que elige el cliente.
        id: Identificador del boton.
        title: Texto del boton.
    """

    type: Literal["button_reply"]
    message_id: str
    id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=256)

    @field_validator("message_id")
    @classmethod
    def _id_valido(cls, v: str) -> str:
        """Solo caracteres seguros para una clave de Redis."""
        if not _ID_CLIENTE.fullmatch(v):
            raise ValueError("message_id invalido")
        return v

    @field_validator("title")
    @classmethod
    def _titulo_limpio(cls, v: str) -> str:
        """Quita controles."""
        return limpiar_texto(v).strip() or "-"


class PingFrame(_Frame):
    """Keepalive del cliente."""

    type: Literal["ping"]


#: Frames que el cliente puede mandar despues del `hello`.
InboundFrame = Annotated[MessageFrame | ButtonReplyFrame | PingFrame, Field(discriminator="type")]


def frame_de_error(code: str, message: str) -> dict[str, Any]:
    """Arma un frame `error` para el cliente.

    Args:
        code: Codigo estable, legible por maquina.
        message: Explicacion breve (nunca un traceback ni un detalle interno).

    Returns:
        El frame listo para `send_json`.
    """
    return {"type": "error", "code": code, "message": message}
