"""Tests de `TelegramProvider` (Sprint 9, Dev A).

La Bot API se sustituye con `httpx.MockTransport`: no hay red. Los payloads
siguen la forma real de un `Update` (https://core.telegram.org/bots/api#update).
"""

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from app.schemas.message import ChannelEnum, MessageTypeEnum
from app.services.messaging import telegram as telegram_module
from app.services.messaging.base import MessageContent, TemplateMessage, TemplateNotSupportedError
from app.services.messaging.telegram import (
    MAX_CAPTION_LENGTH,
    MAX_TEXT_LENGTH,
    TELEGRAM_FILE_PREFIX,
    TelegramAPIError,
    TelegramProvider,
    partir_texto,
)

TOKEN = "123456789:AAH-token_de_prueba"
CONFIG = {"bot_token": TOKEN}
FECHA = 1_700_000_000


def _update(**extra: Any) -> dict[str, Any]:
    """Arma un `Update` de mensaje privado con lo que pida cada test.

    Args:
        **extra: Campos que se suman al `message` (text, photo, voice, ...).

    Returns:
        El `Update` completo.
    """
    mensaje: dict[str, Any] = {
        "message_id": 456,
        "from": {"id": 789, "is_bot": False, "first_name": "Ada", "last_name": "Lovelace"},
        "chat": {"id": 789, "type": "private"},
        "date": FECHA,
    }
    mensaje.update(extra)
    return {"update_id": 1001, "message": mensaje}


@pytest.fixture
def provider() -> TelegramProvider:
    """Provider sin config: Telegram no la necesita para construirse."""
    return TelegramProvider()


# ─── Recepcion ───────────────────────────────────────────────────────────────


class TestParseWebhook:
    """`parse_webhook` convierte un Update en `NormalizedMessage`."""

    async def test_texto(self, provider: TelegramProvider) -> None:
        """Los campos basicos salen del mensaje, con el chat como remitente."""
        normalizado = await provider.parse_webhook(_update(text="Hola"))

        assert normalizado.channel == ChannelEnum.telegram
        assert normalizado.sender_identifier == "789"
        assert normalizado.sender_name == "Ada Lovelace"
        assert normalizado.text == "Hola"
        assert normalizado.media_url is None
        assert normalizado.timestamp == datetime.fromtimestamp(FECHA, tz=timezone.utc)

    async def test_el_id_externo_es_el_update_id_no_el_message_id(
        self, provider: TelegramProvider
    ) -> None:
        """`message_id` solo es unico dentro de un chat.

        La clave de deduplicacion es global por canal: dos usuarios distintos
        con el mismo `message_id` se descartarian entre si.
        """
        uno = _update(text="a")
        dos = _update(text="b")
        dos["update_id"] = 1002
        dos["message"]["from"]["id"] = 555
        dos["message"]["chat"]["id"] = 555

        n1 = await provider.parse_webhook(uno)
        n2 = await provider.parse_webhook(dos)

        assert uno["message"]["message_id"] == dos["message"]["message_id"]
        assert n1.external_message_id == "1001"
        assert n2.external_message_id == "1002"

    async def test_foto_toma_la_de_mayor_resolucion(self, provider: TelegramProvider) -> None:
        """`photo` trae la misma imagen en varios tamanos, de menor a mayor."""
        fotos = [
            {"file_id": "chica", "width": 90, "height": 90},
            {"file_id": "mediana", "width": 320, "height": 320},
            {"file_id": "grande", "width": 1280, "height": 1280},
        ]

        normalizado = await provider.parse_webhook(_update(photo=fotos, caption="mi factura"))

        assert normalizado.media_type == MessageTypeEnum.image
        assert normalizado.media_url == f"{TELEGRAM_FILE_PREFIX}grande"
        assert normalizado.text == "mi factura"

    @pytest.mark.parametrize(
        ("clave", "esperado"),
        [
            ("voice", MessageTypeEnum.audio),
            ("audio", MessageTypeEnum.audio),
            ("video", MessageTypeEnum.video),
            ("video_note", MessageTypeEnum.video),
            ("document", MessageTypeEnum.document),
        ],
    )
    async def test_media_por_tipo(
        self, provider: TelegramProvider, clave: str, esperado: MessageTypeEnum
    ) -> None:
        """Cada clave de media se normaliza a su tipo, con el file_id sin resolver."""
        normalizado = await provider.parse_webhook(_update(**{clave: {"file_id": "abc"}}))

        assert normalizado.media_type == esperado
        assert normalizado.media_url == f"{TELEGRAM_FILE_PREFIX}abc"

    async def test_no_llama_a_la_api_para_resolver_el_archivo(
        self, provider: TelegramProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El webhook responde en <100 ms: `getFile` es una llamada de red.

        Si `parse_webhook` abriera un cliente HTTP, esto revienta.
        """

        def _prohibido(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("parse_webhook no debe hacer llamadas de red")

        monkeypatch.setattr(telegram_module.httpx, "AsyncClient", _prohibido)

        await provider.parse_webhook(_update(voice={"file_id": "abc"}))

    async def test_ubicacion(self, provider: TelegramProvider) -> None:
        """Las coordenadas van a `location`, sin texto."""
        normalizado = await provider.parse_webhook(
            _update(location={"latitude": 4.6, "longitude": -74.1})
        )

        assert normalizado.location == {"latitude": 4.6, "longitude": -74.1}
        assert normalizado.text is None

    async def test_contacto_compartido(self, provider: TelegramProvider) -> None:
        """Se conserva como texto legible para el agente."""
        normalizado = await provider.parse_webhook(
            _update(contact={"phone_number": "+573001234567", "first_name": "Grace"})
        )

        assert normalizado.text == "Contacto compartido: Grace +573001234567"

    async def test_toque_de_boton(self, provider: TelegramProvider) -> None:
        """Un `callback_query` llega como texto y como `interactive_response`."""
        update = {
            "update_id": 2001,
            "callback_query": {
                "id": "cb-77",
                "from": {"id": 789, "is_bot": False, "first_name": "Ada"},
                "message": {"message_id": 10, "chat": {"id": 789, "type": "private"}},
                "data": "confirmar_cita",
            },
        }

        normalizado = await provider.parse_webhook(update)

        assert normalizado.text == "confirmar_cita"
        assert normalizado.sender_identifier == "789"
        assert normalizado.external_message_id == "2001"
        assert normalizado.interactive_response == {
            "type": "button_reply",
            "id": "confirmar_cita",
            "callback_query_id": "cb-77",
        }

    async def test_nombre_cae_al_usuario_y_luego_a_nada(self, provider: TelegramProvider) -> None:
        """Sin nombre de pila se usa `@usuario`; sin ninguno, `None`."""
        con_usuario = _update(text="x")
        con_usuario["message"]["from"] = {"id": 789, "is_bot": False, "username": "ada_l"}
        anonimo = _update(text="x")
        anonimo["message"]["from"] = {"id": 789, "is_bot": False}

        assert (await provider.parse_webhook(con_usuario)).sender_name == "@ada_l"
        assert (await provider.parse_webhook(anonimo)).sender_name is None

    async def test_el_payload_crudo_se_conserva(self, provider: TelegramProvider) -> None:
        """Va a `messages.metadata` para auditoria y depuracion."""
        update = _update(text="Hola")

        normalizado = await provider.parse_webhook(update)

        assert normalizado.raw_payload == update

    @pytest.mark.parametrize(
        "tipo",
        ["group", "supergroup", "channel"],
    )
    async def test_solo_chats_privados(self, provider: TelegramProvider, tipo: str) -> None:
        """En un grupo, responder al `from.id` escribiria un DM que Telegram rechaza."""
        update = _update(text="hola grupo")
        update["message"]["chat"] = {"id": -100123, "type": tipo}

        with pytest.raises(ValueError, match="chats privados"):
            await provider.parse_webhook(update)

    async def test_descarta_mensajes_de_otros_bots(self, provider: TelegramProvider) -> None:
        """Dos bots respondiendose entre si serian un bucle infinito."""
        update = _update(text="soy un bot")
        update["message"]["from"]["is_bot"] = True

        with pytest.raises(ValueError, match="otro bot"):
            await provider.parse_webhook(update)

    async def test_tipo_no_soportado(self, provider: TelegramProvider) -> None:
        """Un sticker no tiene texto ni media que procesar."""
        with pytest.raises(ValueError, match="no soportado"):
            await provider.parse_webhook(_update(sticker={"file_id": "s"}))

    @pytest.mark.parametrize(
        "update",
        [
            {"update_id": 1, "edited_message": {"text": "editado"}},
            {"update_id": 1, "my_chat_member": {}},
            {"message": {"text": "sin update_id"}},
            {"update_id": 1},
        ],
    )
    async def test_updates_que_no_son_mensajes(
        self, provider: TelegramProvider, update: dict[str, Any]
    ) -> None:
        """Cualquier otro tipo de update se rechaza con un error claro."""
        with pytest.raises(ValueError, match="Update de Telegram"):
            await provider.parse_webhook(update)


# ─── Firma ───────────────────────────────────────────────────────────────────


class TestValidateSignature:
    """Telegram devuelve el `secret_token` tal cual: no hay HMAC."""

    async def test_secreto_correcto(self, provider: TelegramProvider) -> None:
        """Coincide con el registrado en setWebhook."""
        assert await provider.validate_signature(b"", "mi-secreto", "mi-secreto")

    async def test_secreto_incorrecto(self, provider: TelegramProvider) -> None:
        """Cualquier otro valor se rechaza."""
        assert not await provider.validate_signature(b"", "otro", "mi-secreto")

    async def test_sin_header(self, provider: TelegramProvider) -> None:
        """Sin el header no hay nada que comparar."""
        assert not await provider.validate_signature(b"", "", "mi-secreto")

    async def test_secreto_sin_configurar_nunca_valida(self, provider: TelegramProvider) -> None:
        """Con `TELEGRAM_WEBHOOK_SECRET` vacio, un header vacio no puede colarse."""
        assert not await provider.validate_signature(b"", "", "")
        assert not await provider.validate_signature(b"", "cualquiera", "")

    async def test_caracteres_no_ascii_no_revientan(self, provider: TelegramProvider) -> None:
        """`hmac.compare_digest` con `str` no ASCII lanza TypeError; con bytes no."""
        assert not await provider.validate_signature(b"", "secretó", "secreto")


# ─── Envio ───────────────────────────────────────────────────────────────────


class _Api:
    """Bot API falsa: registra las peticiones y responde lo que se le indique."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.peticiones: list[tuple[str, dict[str, Any]]] = []
        self._handler = handler
        self._siguiente_id = 100

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Guarda la peticion y devuelve `ok: true` salvo que haya un handler propio."""
        cuerpo = json.loads(request.content) if request.content else {}
        self.peticiones.append((request.url.path, cuerpo))
        if self._handler:
            return self._handler(request)
        self._siguiente_id += 1
        return httpx.Response(200, json={"ok": True, "result": {"message_id": self._siguiente_id}})


def _instalar(monkeypatch: pytest.MonkeyPatch, api: _Api) -> _Api:
    """Hace que todo `httpx.AsyncClient` del provider hable con la API falsa."""
    real = httpx.AsyncClient

    def _fabrica(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(api)
        return real(*args, **kwargs)

    monkeypatch.setattr(telegram_module.httpx, "AsyncClient", _fabrica)
    return api


class TestSendMessage:
    """`send_message` habla con la Bot API y trocea lo que no cabe."""

    async def test_texto_simple(
        self, provider: TelegramProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST a `/bot<token>/sendMessage` y devuelve el message_id como texto."""
        api = _instalar(monkeypatch, _Api())

        resultado = await provider.send_message("789", MessageContent(text="Hola"), CONFIG)

        assert resultado == "101"
        assert api.peticiones == [(f"/bot{TOKEN}/sendMessage", {"chat_id": "789", "text": "Hola"})]

    async def test_no_manda_parse_mode(
        self, provider: TelegramProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El texto lo genera un LLM: con `parse_mode: HTML`, un `<` rompe el envio.

        Telegram responde 400 "can't parse entities" y el mensaje se pierde.
        """
        api = _instalar(monkeypatch, _Api())

        await provider.send_message("789", MessageContent(text="Si a < b & c > d"), CONFIG)

        assert "parse_mode" not in api.peticiones[0][1]
        assert api.peticiones[0][1]["text"] == "Si a < b & c > d"

    async def test_texto_largo_se_trocea_y_devuelve_el_ultimo_id(
        self, provider: TelegramProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Telegram rechaza mas de 4096 caracteres; nada aguas arriba lo controla."""
        api = _instalar(monkeypatch, _Api())
        texto = " ".join(["palabra"] * 1500)  # ~12000 caracteres

        resultado = await provider.send_message("789", MessageContent(text=texto), CONFIG)

        enviados = [c["text"] for _, c in api.peticiones]
        assert len(enviados) >= 3
        assert all(len(t) <= MAX_TEXT_LENGTH for t in enviados)
        assert " ".join(enviados).split() == texto.split(), "no debe perderse ni duplicarse nada"
        assert resultado == str(100 + len(enviados))

    async def test_botones_van_en_el_ultimo_trozo(
        self, provider: TelegramProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un teclado en el primer trozo quedaria a mitad de la respuesta."""
        api = _instalar(monkeypatch, _Api())
        botones = [{"title": "Si", "id": "confirmar"}, {"title": "No", "id": "cancelar"}]

        await provider.send_message(
            "789", MessageContent(text=" ".join(["x"] * 5000), buttons=botones), CONFIG
        )

        con_teclado = [c for _, c in api.peticiones if "reply_markup" in c]
        assert len(con_teclado) == 1
        assert con_teclado[0] is api.peticiones[-1][1]
        assert con_teclado[0]["reply_markup"]["inline_keyboard"] == [
            [{"text": "Si", "callback_data": "confirmar"}],
            [{"text": "No", "callback_data": "cancelar"}],
        ]

    async def test_imagen_con_pie(
        self, provider: TelegramProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Una imagen sale por `sendPhoto`, con el pie como `caption`."""
        api = _instalar(monkeypatch, _Api())
        contenido = MessageContent(
            media_url="https://x.test/a.png", media_type="image", caption="Tu factura"
        )

        await provider.send_message("789", contenido, CONFIG)

        assert api.peticiones == [
            (
                f"/bot{TOKEN}/sendPhoto",
                {"chat_id": "789", "photo": "https://x.test/a.png", "caption": "Tu factura"},
            )
        ]

    async def test_documento_usa_send_document(
        self, provider: TelegramProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cada tipo de media tiene su metodo y su campo."""
        api = _instalar(monkeypatch, _Api())

        await provider.send_message(
            "789", MessageContent(media_url="https://x.test/a.pdf", media_type="document"), CONFIG
        )

        ruta, cuerpo = api.peticiones[0]
        assert ruta.endswith("/sendDocument")
        assert cuerpo["document"] == "https://x.test/a.pdf"

    async def test_pie_demasiado_largo_sale_como_mensaje_aparte(
        self, provider: TelegramProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El pie de un media admite 1024; si no cabe, el media va sin pie."""
        api = _instalar(monkeypatch, _Api())
        pie = "a" * (MAX_CAPTION_LENGTH + 50)

        await provider.send_message(
            "789", MessageContent(media_url="https://x.test/a.png", caption=pie), CONFIG
        )

        assert [r.rsplit("/", 1)[1] for r, _ in api.peticiones] == ["sendPhoto", "sendMessage"]
        assert "caption" not in api.peticiones[0][1]
        assert api.peticiones[1][1]["text"] == pie


class TestErrores:
    """Los errores de la Bot API y de la red no filtran el token."""

    async def test_api_rechaza(
        self, provider: TelegramProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`ok: false` se convierte en `TelegramAPIError` con el motivo."""
        _instalar(
            monkeypatch,
            _Api(
                lambda r: httpx.Response(
                    403,
                    json={
                        "ok": False,
                        "error_code": 403,
                        "description": "Forbidden: bot was blocked by the user",
                    },
                )
            ),
        )

        with pytest.raises(TelegramAPIError, match="blocked by the user") as error:
            await provider.send_message("789", MessageContent(text="Hola"), CONFIG)

        assert error.value.error_code == 403

    async def test_rate_limit_expone_retry_after(
        self, provider: TelegramProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con 429 Telegram dice cuanto esperar; quien reintente lo necesita."""
        _instalar(
            monkeypatch,
            _Api(
                lambda r: httpx.Response(
                    429,
                    json={
                        "ok": False,
                        "error_code": 429,
                        "description": "Too Many Requests: retry after 7",
                        "parameters": {"retry_after": 7},
                    },
                )
            ),
        )

        with pytest.raises(TelegramAPIError) as error:
            await provider.send_message("789", MessageContent(text="Hola"), CONFIG)

        assert error.value.retry_after == 7

    async def test_error_de_red_no_incluye_el_token(
        self, provider: TelegramProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`str(httpx.HTTPError)` incluye la URL, y la URL lleva el token del bot.

        Ese texto acaba en el log de la tarea de Celery y en Sentry/Jaeger.
        """

        def _cae(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"no se pudo conectar a {request.url}", request=request)

        _instalar(monkeypatch, _Api(_cae))

        with pytest.raises(TelegramAPIError) as error:
            await provider.send_message("789", MessageContent(text="Hola"), CONFIG)

        assert TOKEN not in str(error.value)
        assert "ConnectError" in str(error.value)
        assert error.value.__cause__ is None, "encadenar la excepcion original filtraria la URL"

    async def test_respuesta_sin_json(
        self, provider: TelegramProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un 502 de un proxy con HTML no debe salir como un error de parseo crudo."""
        _instalar(monkeypatch, _Api(lambda r: httpx.Response(502, text="<html>Bad Gateway</html>")))

        with pytest.raises(TelegramAPIError, match="502") as error:
            await provider.send_message("789", MessageContent(text="Hola"), CONFIG)

        assert error.value.error_code == 502

    async def test_la_descripcion_no_incluye_el_token(
        self, provider: TelegramProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si Telegram repite el token en su descripcion, tambien se redacta."""
        _instalar(
            monkeypatch,
            _Api(lambda r: httpx.Response(401, json={"ok": False, "description": f"bad {TOKEN}"})),
        )

        with pytest.raises(TelegramAPIError) as error:
            await provider.send_message("789", MessageContent(text="Hola"), CONFIG)

        assert TOKEN not in str(error.value)


# ─── Templates, restricciones, utilidades ────────────────────────────────────


class TestContratoDelCanal:
    """Lo que el resto de la aplicacion puede asumir de Telegram."""

    async def test_no_soporta_templates(self, provider: TelegramProvider) -> None:
        """Lanza `TemplateNotSupportedError` en vez de fingir un envio."""
        with pytest.raises(TemplateNotSupportedError):
            await provider.send_template(
                "789", TemplateMessage(template_name="x", language="es", components=[]), CONFIG
            )

    def test_restricciones(self, provider: TelegramProvider) -> None:
        """4096 caracteres, sin ventana de sesion, sin template obligatorio."""
        restricciones = provider.get_channel_constraints()

        assert restricciones.max_text_length == 4096
        assert restricciones.session_window_hours is None
        assert restricciones.requires_template_outside_window is False
        assert "audio" in restricciones.supported_media_types

    def test_file_id_de(self) -> None:
        """Distingue un archivo de Telegram de cualquier otra URL."""
        assert TelegramProvider.file_id_de(f"{TELEGRAM_FILE_PREFIX}abc") == "abc"
        assert TelegramProvider.file_id_de("https://x.test/a.png") is None
        assert TelegramProvider.file_id_de(None) is None


class TestUtilidadesDeLaApi:
    """`register_webhook` y `get_file_url`."""

    async def test_register_webhook(
        self, provider: TelegramProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Registra el secret_token y limita los updates a los que se procesan."""
        api = _instalar(
            monkeypatch,
            _Api(
                lambda r: httpx.Response(
                    200, json={"ok": True, "result": True, "description": "Webhook was set"}
                )
            ),
        )

        respuesta = await provider.register_webhook(
            "https://api.example.com/api/v1/webhooks/telegram/telegram", "s3cret", CONFIG
        )

        ruta, cuerpo = api.peticiones[0]
        assert ruta.endswith("/setWebhook")
        assert cuerpo["secret_token"] == "s3cret"
        assert cuerpo["allowed_updates"] == ["message", "callback_query"]
        assert respuesta["description"] == "Webhook was set"

    async def test_get_file_url(
        self, provider: TelegramProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resuelve el file_id a la URL de descarga."""
        _instalar(
            monkeypatch,
            _Api(
                lambda r: httpx.Response(
                    200, json={"ok": True, "result": {"file_path": "voice/file_1.oga"}}
                )
            ),
        )

        url = await provider.get_file_url("abc", CONFIG)

        assert url == f"https://api.telegram.org/file/bot{TOKEN}/voice/file_1.oga"

    async def test_get_file_url_sin_file_path(
        self, provider: TelegramProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Telegram omite `file_path` en archivos de mas de 20 MB."""
        _instalar(monkeypatch, _Api(lambda r: httpx.Response(200, json={"ok": True, "result": {}})))

        with pytest.raises(TelegramAPIError, match="file_path"):
            await provider.get_file_url("abc", CONFIG)


class TestPartirTexto:
    """El troceo respeta el limite y corta donde menos molesta."""

    def test_texto_corto_no_se_parte(self) -> None:
        """Lo que cabe sale entero."""
        assert partir_texto("hola") == ["hola"]

    def test_vacio_no_genera_trozos(self) -> None:
        """Telegram rechaza un mensaje vacio: mejor no enviar nada."""
        assert partir_texto("") == []
        assert partir_texto("   \n  ") == []

    def test_justo_en_el_limite(self) -> None:
        """Un texto de exactamente `limite` caracteres es un solo trozo."""
        assert partir_texto("a" * 10, limite=10) == ["a" * 10]

    def test_ningun_trozo_supera_el_limite(self) -> None:
        """Con o sin espacios, con cualquier longitud."""
        for texto in ("a" * 95, "palabra " * 40, ("linea\n" * 30)):
            assert all(len(t) <= 20 for t in partir_texto(texto, limite=20))

    def test_prefiere_cortar_en_un_salto_de_linea(self) -> None:
        """Un parrafo no se parte por la mitad si hay un salto en la segunda mitad.

        Un salto demasiado al principio se ignora a proposito: cortar ahi
        dejaria un trozo diminuto y otro casi entero.
        """
        texto = "primer parrafo bastante largo aqui\nsegundo parrafo largo que no cabe"

        trozos = partir_texto(texto, limite=40)

        assert trozos[0] == "primer parrafo bastante largo aqui"

    def test_sin_espacios_corta_en_seco(self) -> None:
        """Una URL larga sin espacios no se puede cortar en otro sitio."""
        assert partir_texto("x" * 25, limite=10) == ["x" * 10, "x" * 10, "x" * 5]

    def test_no_deja_espacios_en_los_bordes(self) -> None:
        """Un trozo que empieza o termina en espacio se veria raro en el chat."""
        for trozo in partir_texto("uno dos tres cuatro cinco seis siete", limite=10):
            assert trozo == trozo.strip()
