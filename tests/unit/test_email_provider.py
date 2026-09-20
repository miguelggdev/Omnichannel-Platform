"""Tests de `EmailProvider` (Sprint 9, Dev A).

Los payloads siguen la forma del Inbound Parse de SendGrid (todas las cabeceras
en un campo `headers`) y de Mailgun (`message-headers` en JSON). El SMTP se
sustituye por un espia: no hay red.
"""

import base64
import json
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any

import pytest

from app.core.config import get_settings
from app.schemas.message import ChannelEnum
from app.services.messaging import email_provider as email_module
from app.services.messaging.base import (
    IgnoredWebhookError,
    MessageContent,
    TemplateMessage,
    TemplateNotSupportedError,
)
from app.services.messaging.email_provider import (
    MAX_BODY_CHARS,
    EmailProvider,
    recortar_cita,
)

DESDE = "soporte@empresa.com"
CONFIG = {
    "smtp_host": "smtp.empresa.com",
    "smtp_port": 587,
    "smtp_user": "apikey",
    "smtp_password": "s3cret",
    "from_email": DESDE,
    "from_name": "Soporte Empresa",
}

CABECERAS_SENDGRID = (
    "Received: from mail.example.com\n"
    "Message-ID: <abc123@mail.example.com>\n"
    "Date: Mon, 14 Nov 2023 22:13:20 +0000\n"
    "In-Reply-To: <previo@empresa.com>\n"
    "References: <raiz@empresa.com> <previo@empresa.com>\n"
)


def _sendgrid(**extra: Any) -> dict[str, Any]:
    """Arma un formulario de Inbound Parse de SendGrid.

    Args:
        **extra: Campos que reemplazan o suman a los de base.

    Returns:
        El formulario como diccionario de campos de texto.
    """
    campos: dict[str, Any] = {
        "from": "Juan Perez <Juan.Perez@Example.com>",
        "to": DESDE,
        "subject": "Consulta de precios",
        "text": "Hola, quiero saber el precio del plan Pro.",
        "headers": CABECERAS_SENDGRID,
    }
    campos.update(extra)
    return campos


@pytest.fixture
def provider() -> EmailProvider:
    """Provider sin config: el email no la necesita para construirse."""
    return EmailProvider()


@pytest.fixture(autouse=True)
def _direccion_propia(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fija `EMAIL_FROM_ADDRESS` sin tocar el `.env`."""
    monkeypatch.setattr(get_settings(), "EMAIL_FROM_ADDRESS", DESDE)


# ─── Recepcion ───────────────────────────────────────────────────────────────


class TestParseWebhook:
    """`parse_webhook` normaliza un email entrante."""

    async def test_email_de_sendgrid(self, provider: EmailProvider) -> None:
        """Remitente en minusculas, nombre, texto con asunto y hilo."""
        normalizado = await provider.parse_webhook(_sendgrid())

        assert normalizado.channel == ChannelEnum.email
        assert normalizado.sender_identifier == "juan.perez@example.com"
        assert normalizado.sender_name == "Juan Perez"
        assert normalizado.text == (
            "Asunto: Consulta de precios\n\nHola, quiero saber el precio del plan Pro."
        )
        assert normalizado.external_message_id == "<abc123@mail.example.com>"
        assert normalizado.timestamp == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)

    async def test_email_de_mailgun(self, provider: EmailProvider) -> None:
        """Mailgun: cabeceras en JSON y `stripped-text` sin el historial citado."""
        cabeceras = json.dumps(
            [
                ["Message-Id", "<mg-1@mail.example.com>"],
                ["In-Reply-To", "<previo@empresa.com>"],
                ["Date", "Mon, 14 Nov 2023 22:13:20 +0000"],
            ]
        )
        payload = {
            "sender": "ana@example.com",
            "from": "Ana <ana@example.com>",
            "recipient": DESDE,
            "subject": "Re: pedido",
            "body-plain": "Gracias\n\n> mensaje anterior",
            "stripped-text": "Gracias",
            "message-headers": cabeceras,
        }

        normalizado = await provider.parse_webhook(payload)

        assert normalizado.sender_identifier == "ana@example.com"
        assert normalizado.text == "Asunto: Re: pedido\n\nGracias"
        assert normalizado.external_message_id == "<mg-1@mail.example.com>"
        assert normalizado.raw_payload["in_reply_to"] == "<previo@empresa.com>"

    async def test_el_payload_guardado_es_compacto(self, provider: EmailProvider) -> None:
        """Va a `messages.metadata`, a la cola de Celery y a `audit_logs`.

        Solo lo necesario para responder en el hilo: ni el HTML ni el cuerpo.
        """
        normalizado = await provider.parse_webhook(
            _sendgrid(html="<p>" + "x" * 50_000 + "</p>", envelope="{}", charsets="{}")
        )

        assert set(normalizado.raw_payload) == {
            "subject",
            "message_id",
            "in_reply_to",
            "references",
            "from",
            "to",
        }
        assert len(json.dumps(normalizado.raw_payload)) < 2_000
        assert normalizado.raw_payload["references"] == "<raiz@empresa.com> <previo@empresa.com>"

    async def test_nombre_codificado_rfc2047(self, provider: EmailProvider) -> None:
        """`=?utf-8?q?Jos=C3=A9?=` se decodifica a `José`."""
        normalizado = await provider.parse_webhook(
            _sendgrid(**{"from": "=?utf-8?q?Jos=C3=A9_Mu=C3=B1oz?= <jose@example.com>"})
        )

        assert normalizado.sender_name == "José Muñoz"

    async def test_solo_asunto(self, provider: EmailProvider) -> None:
        """Un email sin cuerpo pero con asunto sigue siendo una peticion."""
        normalizado = await provider.parse_webhook(_sendgrid(text=""))

        assert normalizado.text == "Asunto: Consulta de precios"

    async def test_sin_asunto_ni_cuerpo_se_ignora(self, provider: EmailProvider) -> None:
        """No hay nada que responder."""
        with pytest.raises(IgnoredWebhookError, match="sin asunto ni cuerpo"):
            await provider.parse_webhook(_sendgrid(subject="", text=""))

    async def test_solo_html(self, provider: EmailProvider) -> None:
        """Sin parte de texto se convierte el HTML, sin etiquetas ni scripts."""
        normalizado = await provider.parse_webhook(
            _sendgrid(
                text="",
                html="<style>p{}</style><p>Hola&nbsp;equipo</p><script>alert(1)</script>"
                "<div>Necesito ayuda</div>",
            )
        )

        assert "Hola" in normalizado.text
        assert "Necesito ayuda" in normalizado.text
        assert "alert" not in normalizado.text
        assert "<" not in normalizado.text.split("\n\n", 1)[1]

    async def test_cuerpo_enorme_se_recorta(self, provider: EmailProvider) -> None:
        """Un cuerpo de cientos de KB cuesta tokens en cada turno."""
        normalizado = await provider.parse_webhook(_sendgrid(text="a" * 100_000))

        assert len(normalizado.text) < MAX_BODY_CHARS + 200
        assert normalizado.text.endswith("[...mensaje recortado]")

    async def test_sin_message_id_se_sintetiza_uno_estable(self, provider: EmailProvider) -> None:
        """Sin Message-Id no se podria deduplicar una reentrega."""
        sin_id = _sendgrid(headers="Date: Mon, 14 Nov 2023 22:13:20 +0000\n")

        uno = await provider.parse_webhook(sin_id)
        dos = await provider.parse_webhook(dict(sin_id))
        otro = await provider.parse_webhook({**sin_id, "text": "otro cuerpo"})

        assert uno.external_message_id == dos.external_message_id
        assert uno.external_message_id != otro.external_message_id
        assert uno.external_message_id.startswith("<sintetico-")

    async def test_sin_fecha_usa_el_momento_actual(self, provider: EmailProvider) -> None:
        """Un `Date` ausente o ilegible no debe tumbar el mensaje."""
        for headers in ("Message-ID: <x@y.z>\n", "Message-ID: <x@y.z>\nDate: no es una fecha\n"):
            normalizado = await provider.parse_webhook(_sendgrid(headers=headers))
            assert normalizado.timestamp.tzinfo is not None

    async def test_el_asunto_llega_en_una_sola_linea(self, provider: EmailProvider) -> None:
        """Un asunto con saltos de linea es un intento de inyeccion de cabeceras."""
        normalizado = await provider.parse_webhook(
            _sendgrid(subject="Hola\r\nBcc: victima@example.com")
        )

        assert "\n" not in normalizado.raw_payload["subject"]
        assert "\r" not in normalizado.raw_payload["subject"]

    @pytest.mark.parametrize("remitente", ["", "sin arroba", "Nombre <>"])
    async def test_remitente_invalido_es_un_error_no_un_descarte(
        self, provider: EmailProvider, remitente: str
    ) -> None:
        """Un email sin remitente utilizable es un payload roto, no un descarte legitimo."""
        with pytest.raises(ValueError, match="remitente") as error:
            await provider.parse_webhook(_sendgrid(**{"from": remitente}))

        assert not isinstance(error.value, IgnoredWebhookError)


class TestNoResponderAAutomaticos:
    """Un bot que contesta a otro bot es un bucle infinito."""

    @pytest.mark.parametrize(
        ("cabecera", "motivo"),
        [
            ("Auto-Submitted: auto-replied", "Auto-Submitted"),
            ("Auto-Submitted: auto-generated", "Auto-Submitted"),
            ("Precedence: bulk", "Precedence"),
            ("Precedence: list", "Precedence"),
            ("Precedence: junk", "Precedence"),
            ("List-Id: <novedades.example.com>", "lista de correo"),
            ("List-Unsubscribe: <mailto:baja@example.com>", "lista de correo"),
            ("X-Autoreply: yes", "x-autoreply"),
            ("X-Autorespond: yes", "x-autorespond"),
            ("Return-Path: <>", "rebote"),
        ],
    )
    async def test_cabeceras_de_mensaje_automatico(
        self, provider: EmailProvider, cabecera: str, motivo: str
    ) -> None:
        """Autorespuestas, listas de correo y rebotes se descartan sin procesar."""
        payload = _sendgrid(headers=CABECERAS_SENDGRID + cabecera + "\n")

        with pytest.raises(IgnoredWebhookError, match=motivo):
            await provider.parse_webhook(payload)

    async def test_auto_submitted_no_es_automatico(self, provider: EmailProvider) -> None:
        """`Auto-Submitted: no` es lo que mandan los clientes de correo de personas."""
        payload = _sendgrid(headers=CABECERAS_SENDGRID + "Auto-Submitted: no\n")

        assert (await provider.parse_webhook(payload)).sender_identifier

    @pytest.mark.parametrize(
        "remitente",
        [
            "mailer-daemon@example.com",
            "MAILER-DAEMON@example.com",
            "postmaster@example.com",
            "no-reply@example.com",
            "noreply@example.com",
            "donotreply@example.com",
        ],
    )
    async def test_buzones_de_sistema(self, provider: EmailProvider, remitente: str) -> None:
        """Nadie lee lo que se le conteste a un `noreply@`."""
        with pytest.raises(IgnoredWebhookError, match="sistema"):
            await provider.parse_webhook(_sendgrid(**{"from": remitente}))

    async def test_no_procesa_lo_que_sale_de_su_propia_direccion(
        self, provider: EmailProvider
    ) -> None:
        """Un reenvio o una regla de buzon que se lo mande a si mismo seria un bucle."""
        with pytest.raises(IgnoredWebhookError, match="propia direccion"):
            await provider.parse_webhook(_sendgrid(**{"from": f"Soporte <{DESDE.upper()}>"}))

    async def test_sin_direccion_propia_configurada_no_descarta_por_eso(
        self, provider: EmailProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con `EMAIL_FROM_ADDRESS` vacio no se puede comparar: no se inventa un descarte."""
        monkeypatch.setattr(get_settings(), "EMAIL_FROM_ADDRESS", "")

        assert (await provider.parse_webhook(_sendgrid())).sender_identifier


class TestRecortarCita:
    """El historial citado no se le pasa al agente."""

    @pytest.mark.parametrize(
        "texto",
        [
            "Gracias!\n\nOn Mon, Nov 13, 2023 at 10:00 AM Soporte <soporte@empresa.com> wrote:\n> hola\n> mundo",
            "Gracias!\n\nEl lun, 13 nov 2023 a las 10:00, Soporte <soporte@empresa.com> escribió:\n> hola",
            "Gracias!\n\n-----Original Message-----\nFrom: Soporte\nSent: lunes\nhola",
            "Gracias!\n\n________________________________\nFrom: Soporte <soporte@empresa.com>\nSent: lunes\nhola",
            "Gracias!\n> hola\n> mundo",
        ],
    )
    def test_quita_el_historial(self, texto: str) -> None:
        """Gmail (EN/ES), Outlook y citas con `>`."""
        assert recortar_cita(texto) == "Gracias!"

    def test_una_firma_con_raya_no_se_recorta(self) -> None:
        """Una linea de guiones bajos sin cabecera de mensaje debajo es una firma."""
        texto = "Necesito ayuda con el pedido 55.\n\n__________\nJuan Perez\nGerente"

        assert recortar_cita(texto) == texto

    def test_texto_sin_historial_queda_igual(self) -> None:
        """No toca lo que no es una cita."""
        assert recortar_cita("Hola\n\nQuiero comprar.") == "Hola\n\nQuiero comprar."

    def test_una_frase_que_menciona_wrote_no_corta(self) -> None:
        """Solo se corta en la linea de atribucion completa (`On ... wrote:`)."""
        texto = "Como dijo mi jefe, he wrote que el plan es caro.\nQuiero un descuento."

        assert recortar_cita(texto) == texto


# ─── Firma ───────────────────────────────────────────────────────────────────


def _basic(secreto: str) -> str:
    """Valor de la cabecera `Authorization` para `usuario:password`."""
    return "Basic " + base64.b64encode(secreto.encode()).decode()


class TestValidateSignature:
    """El Inbound Parse no va firmado: se autentica con Basic auth en la URL."""

    async def test_credenciales_correctas(self, provider: EmailProvider) -> None:
        """Coincide con `usuario:password` de la URL registrada."""
        assert await provider.validate_signature(
            b"", _basic("hook:una-password"), "hook:una-password"
        )

    async def test_password_incorrecto(self, provider: EmailProvider) -> None:
        """Cualquier otra cosa se rechaza."""
        assert not await provider.validate_signature(b"", _basic("hook:otra"), "hook:una-password")

    async def test_esquema_distinto_de_basic(self, provider: EmailProvider) -> None:
        """Un Bearer con el mismo texto no vale."""
        assert not await provider.validate_signature(
            b"", "Bearer hook:una-password", "hook:una-password"
        )

    async def test_sin_cabecera(self, provider: EmailProvider) -> None:
        """Sin `Authorization` no hay nada que comparar."""
        assert not await provider.validate_signature(b"", "", "hook:una-password")

    async def test_secreto_sin_configurar_nunca_valida(self, provider: EmailProvider) -> None:
        """Con `EMAIL_INBOUND_WEBHOOK_SECRET` vacio, el endpoint no queda abierto."""
        assert not await provider.validate_signature(b"", "", "")
        assert not await provider.validate_signature(b"", _basic(":"), "")


# ─── Envio ───────────────────────────────────────────────────────────────────


class _Smtp:
    """Sustituto de `aiosmtplib.send`: registra el mensaje y las opciones."""

    def __init__(self) -> None:
        self.mensaje: EmailMessage | None = None
        self.opciones: dict[str, Any] = {}

    async def __call__(self, mensaje: EmailMessage, **opciones: Any) -> None:
        """Guarda lo recibido en vez de conectar con un servidor."""
        self.mensaje = mensaje
        self.opciones = opciones


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> _Smtp:
    """SMTP falso instalado en el modulo del provider."""
    espia = _Smtp()
    monkeypatch.setattr(email_module.aiosmtplib, "send", espia)
    return espia


class TestSendMessage:
    """`send_message` responde en el hilo, en texto plano y sin abrir bucles."""

    async def test_respuesta_en_el_hilo(self, provider: EmailProvider, smtp: _Smtp) -> None:
        """Asunto con `Re:`, `In-Reply-To` y `References`: el cliente lo agrupa."""
        contenido = MessageContent(
            text="Claro, el plan Pro cuesta 20 USD.",
            metadata={
                "subject": "Consulta de precios",
                "in_reply_to": "<abc123@mail.example.com>",
                "references": "<raiz@empresa.com> <abc123@mail.example.com>",
            },
        )

        message_id = await provider.send_message("juan@example.com", contenido, CONFIG)

        m = smtp.mensaje
        assert m is not None
        assert m["Subject"] == "Re: Consulta de precios"
        assert m["To"] == "juan@example.com"
        assert m["From"] == "Soporte Empresa <soporte@empresa.com>"
        assert m["In-Reply-To"] == "<abc123@mail.example.com>"
        assert m["References"] == "<raiz@empresa.com> <abc123@mail.example.com>"
        assert m["Message-ID"] == message_id
        assert message_id.endswith("@empresa.com>")
        assert m.get_content().strip() == "Claro, el plan Pro cuesta 20 USD."

    async def test_marca_el_mensaje_como_automatico(
        self, provider: EmailProvider, smtp: _Smtp
    ) -> None:
        """RFC 3834: el autorespondedor del otro lado no nos contesta."""
        await provider.send_message("juan@example.com", MessageContent(text="Hola"), CONFIG)

        assert smtp.mensaje is not None
        assert smtp.mensaje["Auto-Submitted"] == "auto-replied"

    async def test_no_duplica_el_re(self, provider: EmailProvider, smtp: _Smtp) -> None:
        """`Re: Re: Re:` es la marca de un hilo mal gestionado."""
        await provider.send_message(
            "juan@example.com",
            MessageContent(text="Hola", metadata={"subject": "RE: pedido 55"}),
            CONFIG,
        )

        assert smtp.mensaje is not None
        assert smtp.mensaje["Subject"] == "RE: pedido 55"

    async def test_sin_hilo_no_pone_cabeceras_de_respuesta(
        self, provider: EmailProvider, smtp: _Smtp
    ) -> None:
        """Un primer mensaje saliente no responde a nada."""
        await provider.send_message("juan@example.com", MessageContent(text="Hola"), CONFIG)

        m = smtp.mensaje
        assert m is not None
        assert m["In-Reply-To"] is None
        assert m["References"] is None
        assert m["Subject"] == "Respuesta a tu consulta"

    async def test_references_por_defecto_es_el_in_reply_to(
        self, provider: EmailProvider, smtp: _Smtp
    ) -> None:
        """Si el email al que se responde no traia `References`, va solo el `In-Reply-To`."""
        await provider.send_message(
            "juan@example.com",
            MessageContent(text="Hola", metadata={"in_reply_to": "<a@b.c>"}),
            CONFIG,
        )

        assert smtp.mensaje is not None
        assert smtp.mensaje["References"] == "<a@b.c>"

    async def test_inyeccion_de_cabeceras_por_el_asunto(
        self, provider: EmailProvider, smtp: _Smtp
    ) -> None:
        """El asunto lo escribe un tercero: un salto de linea agregaria un `Bcc:`."""
        await provider.send_message(
            "juan@example.com",
            MessageContent(
                text="Hola",
                metadata={
                    "subject": "Pedido\r\nBcc: victima@example.com",
                    "in_reply_to": "<a@b.c>\r\nBcc: otra@example.com",
                },
            ),
            CONFIG,
        )

        m = smtp.mensaje
        assert m is not None
        assert m["Bcc"] is None
        assert "\n" not in m["Subject"]
        assert "\n" not in m["In-Reply-To"]

    async def test_starttls_en_el_587(self, provider: EmailProvider, smtp: _Smtp) -> None:
        """587 es STARTTLS: `use_tls=True` (TLS implicito) fallaria en el handshake."""
        await provider.send_message("juan@example.com", MessageContent(text="Hola"), CONFIG)

        assert smtp.opciones["start_tls"] is True
        assert smtp.opciones["use_tls"] is False
        assert smtp.opciones["hostname"] == "smtp.empresa.com"
        assert smtp.opciones["port"] == 587
        assert smtp.opciones["username"] == "apikey"

    async def test_tls_implicito_en_el_465(self, provider: EmailProvider, smtp: _Smtp) -> None:
        """465 abre la conexion ya cifrada."""
        await provider.send_message(
            "juan@example.com", MessageContent(text="Hola"), {**CONFIG, "smtp_port": 465}
        )

        assert smtp.opciones["use_tls"] is True
        assert smtp.opciones["start_tls"] is False

    async def test_sin_usuario_ni_password(self, provider: EmailProvider, smtp: _Smtp) -> None:
        """Un relay interno puede no pedir autenticacion: se pasa `None`, no una cadena vacia."""
        config = {**CONFIG, "smtp_user": "", "smtp_password": ""}

        await provider.send_message("juan@example.com", MessageContent(text="Hola"), config)

        assert smtp.opciones["username"] is None
        assert smtp.opciones["password"] is None

    @pytest.mark.parametrize("destino", ["", "sin-arroba", "Nombre <>"])
    async def test_destinatario_invalido(
        self, provider: EmailProvider, smtp: _Smtp, destino: str
    ) -> None:
        """No se intenta enviar a algo que no es una direccion."""
        with pytest.raises(ValueError, match="Destinatario"):
            await provider.send_message(destino, MessageContent(text="Hola"), CONFIG)

        assert smtp.mensaje is None

    async def test_extrae_la_direccion_de_un_nombre_con_direccion(
        self, provider: EmailProvider, smtp: _Smtp
    ) -> None:
        """`Juan <juan@example.com>` se envia a `juan@example.com`."""
        await provider.send_message("Juan <juan@example.com>", MessageContent(text="Hola"), CONFIG)

        assert smtp.mensaje is not None
        assert smtp.mensaje["To"] == "juan@example.com"

    async def test_el_texto_no_se_interpreta_como_html(
        self, provider: EmailProvider, smtp: _Smtp
    ) -> None:
        """Solo texto plano: un `<script>` del LLM no puede ejecutarse en ningun cliente."""
        await provider.send_message(
            "juan@example.com", MessageContent(text="<script>alert(1)</script> & <b>x</b>"), CONFIG
        )

        m = smtp.mensaje
        assert m is not None
        assert m.get_content_type() == "text/plain"
        assert not any(parte.get_content_type() == "text/html" for parte in m.walk())


# ─── Templates y restricciones ───────────────────────────────────────────────


class TestContratoDelCanal:
    """Lo que el resto de la aplicacion puede asumir del email."""

    async def test_no_soporta_templates(self, provider: EmailProvider) -> None:
        """Lanza `TemplateNotSupportedError` en vez de fingir un envio."""
        with pytest.raises(TemplateNotSupportedError):
            await provider.send_template(
                "a@b.c", TemplateMessage(template_name="x", language="es", components=[]), CONFIG
            )

    def test_restricciones(self, provider: EmailProvider) -> None:
        """Sin ventana de sesion, sin botones y, por ahora, sin adjuntos."""
        restricciones = provider.get_channel_constraints()

        assert restricciones.session_window_hours is None
        assert restricciones.requires_template_outside_window is False
        assert restricciones.max_buttons == 0
        assert restricciones.supported_media_types == []
        assert restricciones.max_text_length >= 10_000
