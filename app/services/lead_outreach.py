"""Envio de mensajes a un lead por un canal de salida (Sprint 18, Dev B).

Lo usan la tarea de las secuencias (`lead_sequence_tasks`) y `POST /leads/{id}/message`. Resuelve
el destino del lead, envia con el `MessagingProvider` del canal y **clasifica el fallo**, porque
quien llama actua distinto segun sea:

- `EnvioPermanenteError`: reintentar da lo mismo (numero sin WhatsApp, email invalido, canal sin
  plantillas, rechazo 4xx del proveedor). Una secuencia saca al lead con `bounced`.
- `EnvioTemporalError`: caida o limite del proveedor (5xx, 429, timeout, red). Se reintenta.
- `CanalNoDisponibleError`: el lead no tiene destino en ese canal o el despliegue no tiene el
  canal configurado. No se reintenta contra el mismo canal.

Reglas de contenido:

- **Baja en cada correo y WhatsApp frio:** se anade una linea para pedir la baja ("responde BAJA").
  El clasificador (`lead_reply_classifier`) la reconoce y el lead sale de todas las secuencias.
  Ley 1581 de 2012 / RGPD / CAN-SPAM. No se anade a un mensaje escrito a mano por una persona
  (`con_baja=False`): ahi ya habla alguien con el cliente.
- **Un correo de secuencia abre un hilo** (`new_thread`): sin `Re:` ni cabecera de autorespuesta.
- **Datos personales nunca a los logs**: ni destino ni texto; solo canal, ids y codigos.
"""

import logging
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.nodes._tenant import ChannelNotConfiguredError, get_channel_config
from app.models.client import Client
from app.models.contact_identifier import ContactIdentifier
from app.models.lead import Lead, normalizar_telefono_de_lead
from app.services.messaging.base import MessageContent, TemplateMessage, TemplateNotSupportedError
from app.services.messaging.factory import get_messaging_provider

logger = logging.getLogger(__name__)

IDIOMA_POR_DEFECTO = "es"

#: Linea de baja por idioma. Termina en una palabra que `clasificar_respuesta` reconoce.
PIE_DE_BAJA: dict[str, str] = {
    "es": "Si no quieres recibir mas mensajes, responde BAJA.",
    "en": "If you do not want to receive more messages, reply STOP.",
    "pt": "Se nao quiser receber mais mensagens, responda STOP.",
}

#: Estados HTTP que no son culpa del mensaje: se reintentan.
_ESTADOS_TEMPORALES = frozenset({408, 425, 429})


class EnvioError(Exception):
    """Un envio que no se hizo.

    Attributes:
        code: Codigo estable (`invalid_destination`, `provider_rejected`, `provider_down`...),
            apto para el historial: nunca lleva el destino ni el texto.
    """

    def __init__(self, code: str) -> None:
        """Crea el error.

        Args:
            code: Codigo estable.
        """
        super().__init__(code)
        self.code = code


class EnvioPermanenteError(EnvioError):
    """Reintentar no lo arregla."""


class EnvioTemporalError(EnvioError):
    """Fallo pasajero del proveedor o de la red: se puede reintentar."""


class CanalNoDisponibleError(EnvioError):
    """El lead no tiene destino en el canal o el despliegue no lo tiene configurado."""


def idioma_del_tenant(ajustes: Any) -> str:
    """Idioma del tenant (`settings.default_language`), limitado a los que tienen pie de baja.

    Args:
        ajustes: `clients.settings`.

    Returns:
        `es`, `en` o `pt`.
    """
    bruto = ajustes.get("default_language") if isinstance(ajustes, dict) else None
    corto = str(bruto or "").lower()[:2]
    return corto if corto in PIE_DE_BAJA else IDIOMA_POR_DEFECTO


async def datos_del_tenant(session: AsyncSession, client_id: UUID) -> tuple[str | None, str]:
    """Nombre del negocio e idioma, para renderizar los mensajes.

    El nombre sale del perfil del negocio (`settings.business_profile.business_name`) y, si falta,
    de `clients.name`.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.

    Returns:
        `(nombre_del_negocio, idioma)`.
    """
    fila = (
        await session.execute(select(Client.name, Client.settings).where(Client.id == client_id))
    ).first()
    if fila is None:
        return None, IDIOMA_POR_DEFECTO
    nombre, ajustes = fila
    perfil = ajustes.get("business_profile") if isinstance(ajustes, dict) else None
    de_perfil = perfil.get("business_name") if isinstance(perfil, dict) else None
    return (str(de_perfil or nombre or "") or None), idioma_del_tenant(ajustes)


async def destino_del_lead(session: AsyncSession, lead: Lead, canal: str) -> str | None:
    """A donde escribirle al lead por un canal.

    - `email`: su email.
    - `whatsapp`: el identificador de su contacto si lo tiene; si no, su telefono (solo digitos,
      E.164 sin `+`, como pide YCloud).
    - Resto (`instagram`, `facebook`, `telegram`): el identificador de su contacto.

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead.
        canal: Canal de salida.

    Returns:
        El destino, o `None` si el lead no tiene.
    """
    if canal == "email":
        return str(lead.email) if lead.email else None
    if lead.contact_id is not None:
        valor = (
            await session.execute(
                select(ContactIdentifier.identifier_value)
                .where(
                    ContactIdentifier.client_id == lead.client_id,
                    ContactIdentifier.contact_id == lead.contact_id,
                    ContactIdentifier.channel == canal,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if valor:
            return str(valor)
    if canal == "whatsapp" and lead.phone:
        digitos = normalizar_telefono_de_lead(str(lead.phone))
        return digitos if digitos and digitos.isdigit() else None
    return None


def con_pie_de_baja(texto: str, idioma: str) -> str:
    """Anade la linea para pedir la baja, separada por una linea en blanco.

    Args:
        texto: Mensaje ya renderizado.
        idioma: `es`, `en` o `pt`.

    Returns:
        El mensaje con el pie.
    """
    return f"{texto}\n\n{PIE_DE_BAJA.get(idioma, PIE_DE_BAJA[IDIOMA_POR_DEFECTO])}"


def _clasificar(exc: Exception) -> EnvioError:
    """Traduce una excepcion del proveedor a un `EnvioError`, sin copiar su texto."""
    if isinstance(exc, httpx.HTTPStatusError):
        estado = exc.response.status_code
        if estado >= 500 or estado in _ESTADOS_TEMPORALES:
            return EnvioTemporalError(f"provider_http_{estado}")
        return EnvioPermanenteError(f"provider_rejected_{estado}")
    if isinstance(exc, httpx.TransportError | TimeoutError | ConnectionError | OSError):
        return EnvioTemporalError("provider_unreachable")
    if isinstance(exc, ValueError):
        return EnvioPermanenteError("invalid_destination")
    if isinstance(exc, TemplateNotSupportedError | NotImplementedError):
        return EnvioPermanenteError("channel_unsupported")
    return EnvioTemporalError("provider_error")


async def enviar_texto(
    *,
    canal: str,
    destino: str,
    texto: str,
    asunto: str | None = None,
    plantilla_whatsapp: str | None = None,
    idioma: str = IDIOMA_POR_DEFECTO,
) -> str | None:
    """Envia un mensaje por un canal.

    Un `plantilla_whatsapp` en el canal `whatsapp` manda la plantilla aprobada (la unica forma de
    abrir una conversacion fuera de la ventana de 24 h); sin ella, texto libre. La plantilla no
    lleva variables: Meta exige declararlas al aprobarla y cada tenant tiene las suyas.

    Args:
        canal: `whatsapp`, `email`, `instagram`, `facebook` o `telegram`.
        destino: Resultado de `destino_del_lead()`.
        texto: Cuerpo (ya con el pie de baja si toca).
        asunto: Asunto, solo en email.
        plantilla_whatsapp: Nombre de la plantilla aprobada por Meta.
        idioma: Codigo de idioma de la plantilla.

    Returns:
        El id externo del mensaje, si el proveedor dio uno.

    Raises:
        CanalNoDisponibleError: Si el canal no esta configurado en el despliegue.
        EnvioPermanenteError: Si el proveedor lo rechaza para siempre.
        EnvioTemporalError: Si fallo de forma pasajera.
    """
    try:
        nombre, config = get_channel_config(canal)
    except ChannelNotConfiguredError as exc:
        raise CanalNoDisponibleError("channel_not_configured") from exc
    try:
        proveedor = get_messaging_provider(nombre, {"channel": canal})
    except ValueError as exc:
        raise CanalNoDisponibleError("channel_not_configured") from exc
    try:
        if canal == "whatsapp" and plantilla_whatsapp:
            externo = await proveedor.send_template(
                to=destino,
                template=TemplateMessage(
                    template_name=plantilla_whatsapp, language=idioma, components=[]
                ),
                channel_config=config,
            )
        else:
            metadata = {"subject": asunto, "new_thread": True} if canal == "email" else None
            externo = await proveedor.send_message(
                to=destino,
                content=MessageContent(text=texto, metadata=metadata),
                channel_config=config,
            )
    except EnvioError:
        raise
    except Exception as exc:
        error = _clasificar(exc)
        # Sin `exc_info` del cuerpo: el texto de la excepcion del proveedor puede traer el
        # destino. El tipo basta para diagnosticar.
        logger.warning("Envio fallido por %s: %s (%s)", canal, error.code, type(exc).__name__)
        raise error from exc
    return str(externo) if externo else None
