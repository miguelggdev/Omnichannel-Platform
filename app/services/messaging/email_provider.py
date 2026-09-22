"""EmailProvider — email como canal de clientes (SMTP de salida, Inbound Parse de entrada).

Desviaciones deliberadas sobre `specs/sprint-09-channels.md` §3, por lo que el
codigo real ya hace o por lo que romperia en produccion:

- **Firma de la ABC real** (`parse_webhook(raw_payload)`, `send_message(to,
  content, channel_config) -> str`), y credenciales globales por entorno: el spec
  da por hecha una tabla `channel_configs` que no existe.
- **TLS segun el puerto.** El spec llama `aiosmtplib.send(use_tls=True)` con el
  puerto 587 por defecto; `use_tls` es TLS *implicito* (puerto 465) y el 587 es
  STARTTLS, asi que el envio fallaria en el handshake. Aqui 465 usa TLS implicito
  y cualquier otro puerto, STARTTLS.
- **Solo texto plano, sin plantilla HTML.** El spec llama a un
  `_render_html_template` que no define. Un cuerpo generado por un LLM insertado
  en HTML sin escapar es un vector de inyeccion; el texto plano no lo es.
- **La entrada no va firmada.** Ni SendGrid ni Mailgun firman el Inbound Parse.
  Sin autenticacion, cualquiera podria inyectar "emails de clientes" al agente.
  Los dos permiten poner credenciales en la URL (`https://user:pass@host/...`)
  y envian la cabecera `Authorization: Basic`; `validate_signature()` la compara
  con `EMAIL_INBOUND_WEBHOOK_SECRET`.
- **No se responde a mensajes automaticos.** Un bot que contesta a otro
  autorespondedor, a una lista de correo o a un rebote es un bucle infinito que
  llena buzones y quema tokens. `parse_webhook` descarta `Auto-Submitted`,
  `Precedence: bulk/list/junk`, `List-Id`, rebotes y los mensajes de la propia
  direccion; y los envios propios llevan `Auto-Submitted: auto-replied`
  (RFC 3834) para que el otro lado tampoco conteste.
- **El hilo lo lleva el remitente, no el asunto.** `In-Reply-To` y `References`
  se calculan a partir del ultimo mensaje entrante (ver
  `app/agents/nodes/_delivery.py`); el spec propone buscar por asunto, que une
  conversaciones ajenas con el mismo "Re: consulta".
- **Adjuntos: solo metadata, nunca el contenido.** El endpoint extrae
  `filename`/`content_type`/`size` (`app/api/v1/webhooks.py::_leer_adjuntos`);
  aqui se valida de nuevo (defensa en profundidad) y se anota en `raw_payload` y
  como una linea al final del texto, para que el agente sepa que hubo un
  adjunto en vez de silencio total. Guardar el archivo y poder recuperarlo es
  trabajo aparte — Storage, tipos permitidos, cuotas — (ADR-062).
- **`raw_payload` compacto.** Se guarda en `messages.metadata`, en la cola de
  Celery y en `audit_logs`: solo lo necesario para el hilo, no el HTML entero.
"""

import base64
import hashlib
import hmac
import html
import json
import re
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import HeaderParser
from email.utils import make_msgid, parseaddr, parsedate_to_datetime
from typing import Any

import aiosmtplib

from app.core.config import get_settings
from app.schemas.message import ChannelEnum, NormalizedMessage
from app.services.messaging.base import (
    ChannelConstraints,
    IgnoredWebhookError,
    MessageContent,
    MessagingProvider,
    TemplateMessage,
    TemplateNotSupportedError,
)

#: Puerto SMTP con TLS implicito; cualquier otro usa STARTTLS.
SMTP_TLS_IMPLICITO = 465

#: Tope del texto que se le pasa al agente: un cuerpo enorme cuesta tokens.
MAX_BODY_CHARS = 10_000
MAX_SUBJECT_CHARS = 200
_TIMEOUT_SECONDS = 30.0

#: Nombre de archivo cuando el que llego esta vacio o no es texto.
_ADJUNTO_SIN_NOMBRE = "archivo"

# Remitentes que son buzones de sistema.
_REMITENTES_DE_SISTEMA = ("mailer-daemon", "postmaster", "no-reply", "noreply", "donotreply")
_PRECEDENCIA_AUTOMATICA = frozenset({"bulk", "list", "junk"})

_LINEA_CITA = re.compile(
    r"^\s*(?:On|El)\s.{5,300}?\s(?:wrote|escribi[oó]|ha escrito):\s*$", re.IGNORECASE
)
_LINEA_ORIGINAL = re.compile(
    r"^\s*-{2,}\s*(?:Original Message|Mensaje original|Forwarded message)\s*-{2,}\s*$",
    re.IGNORECASE,
)
_DIVISOR_OUTLOOK = re.compile(r"^\s*_{10,}\s*$")
_CABECERA_CITADA = re.compile(r"^\s*(?:From|De|Sent|Enviado|Date|Fecha)\s*:", re.IGNORECASE)
_ETIQUETAS = re.compile(r"<[^>]+>")


def recortar_cita(texto: str) -> str:
    """Quita del cuerpo de un email el historial citado.

    Un cliente que responde a un email trae debajo toda la conversacion anterior
    (`> ...`, "On lunes ... escribio:", "-----Original Message-----"). Pasarle
    eso al agente en cada turno multiplica los tokens y lo confunde con texto
    que ya vio.

    Args:
        texto: Cuerpo en texto plano tal como llego.

    Returns:
        Solo lo que el cliente escribio de nuevo, sin espacios sobrantes.
    """
    lineas = texto.splitlines()
    resultado: list[str] = []
    for i, linea in enumerate(lineas):
        if _LINEA_CITA.match(linea) or _LINEA_ORIGINAL.match(linea):
            break
        if _DIVISOR_OUTLOOK.match(linea):
            # Solo es una cita de Outlook si lo siguiente es la cabecera de un
            # mensaje: una firma con una raya de guiones bajos no se recorta.
            siguiente = next((x for x in lineas[i + 1 :] if x.strip()), "")
            if _CABECERA_CITADA.match(siguiente):
                break
        if linea.lstrip().startswith(">"):
            continue
        resultado.append(linea)
    return "\n".join(resultado).strip()


def _html_a_texto(contenido: str) -> str:
    """Convierte HTML a texto plano de forma burda, para emails sin parte `text`.

    Args:
        contenido: Cuerpo HTML.

    Returns:
        Texto sin etiquetas y con las entidades resueltas.
    """
    sin_bloques = re.sub(r"(?is)<(script|style).*?</\1>", "", contenido)
    con_saltos = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", sin_bloques)
    return html.unescape(_ETIQUETAS.sub("", con_saltos)).strip()


def _decodificar(valor: str) -> str:
    """Decodifica una cabecera codificada (RFC 2047: `=?utf-8?q?...?=`).

    Args:
        valor: Valor de la cabecera.

    Returns:
        El texto legible; el valor original si no se puede decodificar.
    """
    try:
        return str(make_header(decode_header(valor)))
    except Exception:
        return valor


def _limpiar_cabecera(valor: str) -> str:
    """Quita saltos de linea de un valor que va a una cabecera.

    Args:
        valor: Texto que puede venir de un tercero (el asunto de un email).

    Returns:
        El texto en una sola linea. Un salto de linea en una cabecera es
        inyeccion de cabeceras (`Bcc:` adicional, por ejemplo).
    """
    return " ".join(valor.split())


def _cabeceras(payload: dict[str, Any]) -> dict[str, str]:
    """Reune las cabeceras del email, vengan de SendGrid o de Mailgun.

    SendGrid manda todas en un unico campo `headers` (texto RFC 822); Mailgun,
    en `message-headers` (JSON: lista de pares) y algunas tambien sueltas.

    Args:
        payload: Campos del formulario de Inbound Parse.

    Returns:
        Diccionario nombre en minusculas -> primer valor.
    """
    resultado: dict[str, str] = {}
    crudo = payload.get("headers") or payload.get("message-headers")
    if isinstance(crudo, str) and crudo.strip():
        if crudo.lstrip().startswith("["):
            try:
                for nombre, valor in json.loads(crudo):
                    resultado.setdefault(str(nombre).lower(), str(valor))
            except (ValueError, TypeError):
                pass
        else:
            for nombre, valor in HeaderParser().parsestr(crudo).items():
                resultado.setdefault(nombre.lower(), str(valor))
    for nombre in ("Message-Id", "In-Reply-To", "References", "Date"):
        for clave in (nombre, nombre.lower()):
            if isinstance(payload.get(clave), str):
                resultado.setdefault(nombre.lower(), payload[clave])
    return resultado


def _es_automatico(cabeceras: dict[str, str], direccion: str, propia: str) -> str | None:
    """Indica por que un email no debe recibir respuesta automatica, si asi es.

    Args:
        cabeceras: Cabeceras del email (en minusculas).
        direccion: Direccion del remitente, en minusculas.
        propia: Direccion desde la que responde el bot (puede ser vacia).

    Returns:
        El motivo, o `None` si es un email de una persona.
    """
    auto = cabeceras.get("auto-submitted", "no").strip().lower()
    if auto and auto != "no":
        return f"Auto-Submitted: {auto}"
    if cabeceras.get("precedence", "").strip().lower() in _PRECEDENCIA_AUTOMATICA:
        return f"Precedence: {cabeceras['precedence'].strip().lower()}"
    if "list-id" in cabeceras or "list-unsubscribe" in cabeceras:
        return "lista de correo"
    for cabecera in ("x-autoreply", "x-autorespond"):
        if cabecera in cabeceras:
            return cabecera
    if cabeceras.get("return-path", "").strip() == "<>":
        return "rebote (Return-Path vacio)"
    local = direccion.split("@", 1)[0]
    if local in _REMITENTES_DE_SISTEMA:
        return f"buzon de sistema ({local})"
    if propia and direccion == propia.lower():
        return "mensaje de la propia direccion del bot"
    return None


def _adjuntos(crudos: Any) -> list[dict[str, Any]]:
    """Valida y acota la metadata de adjuntos que paso el endpoint.

    Defensa en profundidad: el endpoint (`app/api/v1/webhooks.py::_leer_adjuntos`)
    ya acota cantidad y tamano, pero este metodo no confia ciegamente en
    `raw_payload["_attachments"]` — cualquiera que construya el payload a mano
    (los tests, un llamador futuro) podria mandar algo distinto.

    Args:
        crudos: Lo que traiga `raw_payload.get("_attachments")`.

    Returns:
        Hasta `EMAIL_MAX_ATTACHMENTS` entradas `{filename, content_type, size}`
        saneadas; lista vacia si `crudos` no tiene la forma esperada.
    """
    if not isinstance(crudos, list):
        return []
    resultado: list[dict[str, Any]] = []
    for item in crudos[: get_settings().EMAIL_MAX_ATTACHMENTS]:
        if not isinstance(item, dict):
            continue
        nombre = str(item.get("filename") or _ADJUNTO_SIN_NOMBRE).strip() or _ADJUNTO_SIN_NOMBRE
        tipo = str(item.get("content_type") or "application/octet-stream")[:100]
        try:
            tamano = max(0, int(item.get("size") or 0))
        except (TypeError, ValueError):
            tamano = 0
        resultado.append({"filename": nombre[:200], "content_type": tipo, "size": tamano})
    return resultado


def _tamano_legible(cantidad_bytes: int) -> str:
    """Formatea un tamano en bytes de forma legible para un humano.

    Args:
        cantidad_bytes: Tamano en bytes.

    Returns:
        P. ej. `"842 B"`, `"240 KB"`, `"1.2 MB"`.
    """
    if cantidad_bytes < 1024:
        return f"{cantidad_bytes} B"
    if cantidad_bytes < 1024 * 1024:
        return f"{cantidad_bytes / 1024:.0f} KB"
    return f"{cantidad_bytes / (1024 * 1024):.1f} MB"


def _resumen_de_adjuntos(adjuntos: list[dict[str, Any]]) -> str:
    """Linea legible para agregar al texto del mensaje.

    Args:
        adjuntos: Salida de `_adjuntos()`.

    Returns:
        `"[Adjunto(s): factura.pdf (240 KB), foto.jpg (1.2 MB)]"`.
    """
    partes = [f"{a['filename']} ({_tamano_legible(a['size'])})" for a in adjuntos]
    return f"[Adjunto(s): {', '.join(partes)}]"


class EmailProvider(MessagingProvider):
    """Implementacion de `MessagingProvider` para email.

    Sin config en el constructor, como YCloud y Telegram: la direccion propia y
    el secreto llegan de los ajustes y las credenciales SMTP, por llamada, en
    `channel_config`.
    """

    # ─── Recepcion ──────────────────────────────────────────────────────────

    async def parse_webhook(self, raw_payload: dict[str, Any]) -> NormalizedMessage:
        """Normaliza un email del Inbound Parse de SendGrid o Mailgun.

        Args:
            raw_payload: Campos del formulario, mas `_attachments` (metadata de
                los adjuntos, ver `app/api/v1/webhooks.py::_leer_adjuntos`) si
                el email traia alguno.

        Returns:
            Mensaje normalizado con `channel=email`. `raw_payload` queda reducido
            a lo necesario para responder dentro del hilo, mas `attachments`.

        Raises:
            ValueError: Si no hay remitente valido o si el email es automatico
                (autorespuesta, lista, rebote, la propia direccion del bot). El
                endpoint lo trata como `parse_error` (200): el proveedor no
                reintenta, y el bot no le contesta a otro bot.
        """
        remitente = raw_payload.get("from") or raw_payload.get("sender") or ""
        nombre, direccion = parseaddr(str(remitente))
        direccion = direccion.strip().lower()
        if "@" not in direccion:
            raise ValueError(f"Email sin remitente valido: {str(remitente)[:80]!r}")

        cabeceras = _cabeceras(raw_payload)
        motivo = _es_automatico(cabeceras, direccion, get_settings().EMAIL_FROM_ADDRESS)
        if motivo:
            raise IgnoredWebhookError(f"Email automatico, no se responde: {motivo}")

        asunto = _limpiar_cabecera(_decodificar(str(raw_payload.get("subject") or "")))[
            :MAX_SUBJECT_CHARS
        ]
        cuerpo = self._cuerpo(raw_payload)
        adjuntos = _adjuntos(raw_payload.get("_attachments"))
        if not cuerpo and not asunto and not adjuntos:
            raise IgnoredWebhookError("Email sin asunto, cuerpo ni adjuntos")

        # El asunto suele llevar la peticion en un hilo nuevo ("Consulta de precios").
        texto = f"Asunto: {asunto}\n\n{cuerpo}".strip() if asunto else cuerpo
        if adjuntos:
            # Sin esto el agente no se entera de que hubo un adjunto: el contenido
            # no se procesa (ADR-062), pero al menos no queda en silencio total.
            resumen = _resumen_de_adjuntos(adjuntos)
            texto = f"{texto}\n\n{resumen}".strip() if texto else resumen

        message_id = (cabeceras.get("message-id") or "").strip()
        if not message_id:
            # Sin Message-Id no hay como deduplicar una reentrega: se sintetiza uno
            # estable a partir del contenido (adjuntos incluidos: dos emails sin
            # asunto ni cuerpo pero con adjuntos distintos no deben colisionar).
            huella = hashlib.sha256(
                f"{direccion}|{asunto}|{cuerpo}|{adjuntos}".encode()
            ).hexdigest()[:32]
            message_id = f"<sintetico-{huella}@inbound.local>"

        return NormalizedMessage(
            channel=ChannelEnum.email,
            sender_identifier=direccion,
            sender_name=_decodificar(nombre).strip() or None,
            text=texto,
            timestamp=self._fecha(cabeceras.get("date")),
            external_message_id=message_id,
            raw_payload={
                "subject": asunto,
                "message_id": message_id,
                "in_reply_to": (cabeceras.get("in-reply-to") or "").strip()[:500],
                "references": (cabeceras.get("references") or "").strip()[:2000],
                "attachments": adjuntos,
                "from": direccion,
                "to": str(raw_payload.get("to") or raw_payload.get("recipient") or "")[:500],
            },
        )

    @staticmethod
    def _cuerpo(payload: dict[str, Any]) -> str:
        """Elige el cuerpo y le quita el historial citado.

        Mailgun ya trae `stripped-text` sin citas; SendGrid, no.

        Args:
            payload: Campos del formulario.

        Returns:
            El texto que escribio el cliente, acotado a `MAX_BODY_CHARS`.
        """
        texto = payload.get("stripped-text")
        if isinstance(texto, str) and texto.strip():
            cuerpo = texto.strip()
        else:
            plano = payload.get("text") or payload.get("body-plain") or ""
            if not str(plano).strip():
                plano = _html_a_texto(str(payload.get("html") or payload.get("body-html") or ""))
            cuerpo = recortar_cita(str(plano))
        if len(cuerpo) > MAX_BODY_CHARS:
            cuerpo = cuerpo[:MAX_BODY_CHARS].rstrip() + "\n[...mensaje recortado]"
        return cuerpo

    @staticmethod
    def _fecha(valor: str | None) -> datetime:
        """Interpreta la cabecera `Date`, o devuelve el momento actual.

        Args:
            valor: Valor de la cabecera `Date`, si vino.

        Returns:
            Fecha con zona horaria (UTC si el email no la traia).
        """
        if valor:
            try:
                fecha = parsedate_to_datetime(valor)
                return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
        return datetime.now(timezone.utc)

    async def validate_signature(self, payload: bytes, signature: str, secret: str) -> bool:
        """Compara la cabecera `Authorization: Basic` con el secreto configurado.

        El Inbound Parse no va firmado (ver el docstring del modulo): SendGrid y
        Mailgun envian las credenciales que se pusieron en la URL del webhook.

        Args:
            payload: Cuerpo crudo (no participa: no hay firma sobre el cuerpo).
            signature: Valor de la cabecera `Authorization`.
            secret: `EMAIL_INBOUND_WEBHOOK_SECRET`, con la forma `usuario:password`.

        Returns:
            True si coincide. Un secreto vacio (sin configurar) nunca valida.
        """
        if not secret or not signature:
            return False
        esperado = "Basic " + base64.b64encode(secret.encode("utf-8")).decode("ascii")
        return hmac.compare_digest(signature.encode("utf-8"), esperado.encode("utf-8"))

    # ─── Envio ──────────────────────────────────────────────────────────────

    async def send_message(
        self, to: str, content: MessageContent, channel_config: dict[str, Any]
    ) -> str:
        """Envia un email de texto plano, dentro del hilo si `content.metadata` lo indica.

        Args:
            to: Direccion del destinatario.
            content: Contenido. `metadata` puede traer `subject`, `in_reply_to` y
                `references` para que el cliente de correo lo agrupe en el hilo.
            channel_config: Debe traer `smtp_host` y `from_email`; opcionales
                `smtp_port`, `smtp_user`, `smtp_password`, `from_name`.

        Returns:
            El `Message-ID` del email enviado (sirve de `In-Reply-To` cuando el
            cliente responda).

        Raises:
            ValueError: Si `to` no es una direccion valida.
            aiosmtplib.SMTPException: Si el servidor SMTP rechaza el envio.
        """
        _, destino = parseaddr(to)
        if "@" not in destino:
            raise ValueError(f"Destinatario de email invalido: {to[:80]!r}")

        meta = content.metadata or {}
        from_email = channel_config["from_email"]
        dominio = from_email.split("@", 1)[1] if "@" in from_email else None
        message_id = make_msgid(domain=dominio)

        mensaje = EmailMessage()
        mensaje["From"] = f"{channel_config.get('from_name') or 'Soporte'} <{from_email}>"
        mensaje["To"] = destino
        mensaje["Subject"] = self._asunto(meta.get("subject"))
        mensaje["Message-ID"] = message_id
        # RFC 3834: evita que un autorespondedor del otro lado nos conteste.
        mensaje["Auto-Submitted"] = "auto-replied"

        en_respuesta_a = _limpiar_cabecera(str(meta.get("in_reply_to") or ""))
        if en_respuesta_a:
            referencias = _limpiar_cabecera(str(meta.get("references") or ""))
            mensaje["In-Reply-To"] = en_respuesta_a
            mensaje["References"] = referencias or en_respuesta_a

        mensaje.set_content(content.text or "")

        puerto = int(channel_config.get("smtp_port") or 587)
        await aiosmtplib.send(
            mensaje,
            hostname=channel_config["smtp_host"],
            port=puerto,
            username=channel_config.get("smtp_user") or None,
            password=channel_config.get("smtp_password") or None,
            use_tls=puerto == SMTP_TLS_IMPLICITO,
            start_tls=puerto != SMTP_TLS_IMPLICITO,
            timeout=_TIMEOUT_SECONDS,
        )
        return message_id

    @staticmethod
    def _asunto(asunto: str | None) -> str:
        """Prepara el asunto de la respuesta.

        Args:
            asunto: Asunto del ultimo email del cliente, si se conoce.

        Returns:
            `Re: <asunto>` sin duplicar el `Re:`, en una sola linea.
        """
        limpio = _limpiar_cabecera(asunto or "")[:MAX_SUBJECT_CHARS]
        if not limpio:
            return "Respuesta a tu consulta"
        return limpio if re.match(r"(?i)^re:", limpio) else f"Re: {limpio}"

    async def send_template(
        self, to: str, template: TemplateMessage, channel_config: dict[str, Any]
    ) -> str:
        """El email no tiene templates preaprobados.

        Args:
            to: Sin uso.
            template: Sin uso.
            channel_config: Sin uso.

        Raises:
            TemplateNotSupportedError: Siempre.
        """
        raise TemplateNotSupportedError("El canal email no soporta templates preaprobados")

    def get_channel_constraints(self) -> ChannelConstraints:
        """Restricciones del email.

        Returns:
            Sin ventana de sesion, sin botones ni listas y, por ahora, sin adjuntos.
        """
        return ChannelConstraints(
            max_text_length=50_000,
            supported_media_types=[],
            session_window_hours=None,
            requires_template_outside_window=False,
            max_buttons=0,
            max_list_items=0,
        )
