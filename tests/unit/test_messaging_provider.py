"""Tests del ABC MessagingProvider, la factory, NormalizedMessage y YCloudProvider.

Todo esto es entrega de Dev A (Matriz §6, Sprint 4) y a la fecha de este commit no
esta en `main`. El modulo se omite entero con `importorskip` y se activa solo en
cuanto Dev A mergee, sin editar una linea.

Lo que se verifica aqui es **la costura**: la forma exacta en que
`app/api/v1/webhooks.py` y `app/tasks/webhook_processor.py` consumen esas piezas.
Si Dev A entrega algo con otra firma o con otros nombres de campo, estos tests lo
cazan antes de que el endpoint falle en runtime.

Contrato de referencia: `specs/sprint-04-webhooks.md` §1, §2, §3 y §5.
"""

import hashlib
import hmac
import inspect
import json

import pytest

from tests.fixtures.meta_payloads import YCLOUD_IMAGE, YCLOUD_TEXT

factory = pytest.importorskip(
    "app.services.messaging.factory",
    reason="La factory de messaging la entrega Dev A en Sprint 4; aun no esta en main",
)
base = pytest.importorskip(
    "app.services.messaging.base",
    reason="El ABC MessagingProvider lo entrega Dev A en Sprint 4",
)
ycloud = pytest.importorskip(
    "app.services.messaging.ycloud",
    reason="YCloudProvider lo entrega Dev A en Sprint 4",
)

message_schemas = pytest.importorskip("app.schemas.message")
if not hasattr(message_schemas, "NormalizedMessage"):
    pytest.skip(
        "NormalizedMessage lo entrega Dev A en Sprint 4; app/schemas/message.py "
        "solo tiene los schemas CRUD de Message",
        allow_module_level=True,
    )

NormalizedMessage = message_schemas.NormalizedMessage

WEBHOOK_SECRET = "secreto-de-prueba"


# ─── Factory ─────────────────────────────────────────────────────────────────


class TestFactory:
    """`get_messaging_provider()` resuelve por el nombre que viene en la URL."""

    def test_resuelve_ycloud(self) -> None:
        """El segmento `ycloud` de la URL devuelve el provider de WhatsApp."""
        provider = factory.get_messaging_provider("ycloud")

        assert isinstance(provider, ycloud.YCloudProvider)

    def test_resuelve_meta_con_subcanal(self) -> None:
        """Meta necesita el sub-canal en la config para distinguir IG de FB."""
        meta = pytest.importorskip("app.services.messaging.meta")

        provider = factory.get_messaging_provider("meta", {"channel": "instagram"})

        assert isinstance(provider, meta.MetaProvider)

    def test_provider_desconocido_lanza_value_error(self) -> None:
        """El endpoint traduce este ValueError a un 400; si cambia el tipo, se rompe."""
        with pytest.raises(ValueError, match="telegram"):
            factory.get_messaging_provider("telegram")

    def test_acepta_la_llamada_que_hace_el_endpoint(self) -> None:
        """`_resolve_provider()` llama con (nombre, config) posicionales.

        Ver `app/api/v1/webhooks.py::_resolve_provider`. Si la firma cambia a
        keyword-only o cambia el orden, el POST revienta en runtime.
        """
        firma = inspect.signature(factory.get_messaging_provider)
        parametros = list(firma.parameters.values())

        assert len(parametros) >= 2
        assert all(
            p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            for p in parametros[:2]
        )
        assert parametros[1].default is None, "provider_config debe ser opcional"

    def test_todos_los_providers_implementan_el_abc(self) -> None:
        """Ningun provider registrado puede saltarse la interfaz."""
        for nombre, clase in factory._PROVIDERS.items():
            assert issubclass(clase, base.MessagingProvider), f"{nombre} no implementa el ABC"


# ─── ABC ─────────────────────────────────────────────────────────────────────


class TestMessagingProviderABC:
    """La interfaz que desacopla la app de cualquier proveedor concreto."""

    def test_no_se_puede_instanciar(self) -> None:
        """Es abstracta: instanciarla directamente debe fallar."""
        with pytest.raises(TypeError):
            base.MessagingProvider()

    def test_declara_los_cinco_metodos(self) -> None:
        """parse_webhook, validate_signature, send_message, send_template, constraints."""
        esperados = {
            "parse_webhook",
            "validate_signature",
            "send_message",
            "send_template",
            "get_channel_constraints",
        }

        assert esperados <= base.MessagingProvider.__abstractmethods__

    def test_implementacion_incompleta_no_se_instancia(self) -> None:
        """Olvidarse de un metodo tiene que fallar al construir, no en produccion."""

        class ProviderIncompleto(base.MessagingProvider):
            async def parse_webhook(self, raw_payload: dict) -> object:
                return object()

        with pytest.raises(TypeError):
            ProviderIncompleto()


# ─── NormalizedMessage ───────────────────────────────────────────────────────


class TestNormalizedMessage:
    """El contrato interno entre el endpoint y el worker (PAT-002)."""

    @staticmethod
    def _mensaje() -> object:
        """Construye un NormalizedMessage minimo valido."""
        return NormalizedMessage(
            channel="whatsapp",
            sender_identifier="573001112233",
            text="Hola",
            timestamp="2026-09-09T20:00:00+00:00",
            external_message_id="wamid.X",
            raw_payload={"origen": "test"},
        )

    def test_campos_minimos(self) -> None:
        """Sin media, los campos opcionales quedan a None."""
        mensaje = self._mensaje()

        assert mensaje.sender_identifier == "573001112233"
        assert mensaje.external_message_id == "wamid.X"
        assert mensaje.media_url is None
        assert mensaje.media_type is None

    def test_external_message_id_es_obligatorio(self) -> None:
        """Sin el, la deduplicacion no tiene clave y el endpoint no puede seguir."""
        with pytest.raises(Exception):  # noqa: B017,PT011 — ValidationError de Pydantic
            NormalizedMessage(
                channel="whatsapp",
                sender_identifier="573001112233",
                timestamp="2026-09-09T20:00:00+00:00",
                raw_payload={},
            )

    def test_serializacion_trae_las_claves_que_consume_el_worker(self) -> None:
        """`model_dump(mode="json")` es literalmente lo que viaja a Celery.

        El endpoint serializa asi (`webhooks.py`) y el worker lee estas claves por
        nombre (`webhook_processor.py::_process_message`). Si alguna se renombra, el
        mensaje se guarda incompleto sin que salte ningun error.
        """
        serializado = self._mensaje().model_dump(mode="json")

        requeridas = {
            "channel",
            "sender_identifier",
            "text",
            "media_url",
            "media_type",
            "timestamp",
            "external_message_id",
            "raw_payload",
        }
        assert requeridas <= set(serializado)

    def test_serializacion_es_json_puro(self) -> None:
        """Celery usa serializer json: nada de datetime ni Enum sin convertir."""
        serializado = self._mensaje().model_dump(mode="json")

        json.dumps(serializado)  # revienta si queda algun tipo no serializable

    def test_el_canal_expone_value(self) -> None:
        """El endpoint hace `getattr(normalized.channel, "value", ...)` para la key de dedup."""
        mensaje = self._mensaje()

        assert getattr(mensaje.channel, "value", mensaje.channel) == "whatsapp"


# ─── YCloudProvider ──────────────────────────────────────────────────────────


def _firma_ycloud(payload: bytes, secret: str = WEBHOOK_SECRET) -> str:
    """Firma de YCloud: hex pelado, sin el prefijo `sha256=` que usa Meta."""
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


class TestYCloudProvider:
    """WhatsApp via YCloud."""

    async def test_parse_texto(self) -> None:
        """Un mensaje de texto se normaliza al canal whatsapp."""
        normalized = await ycloud.YCloudProvider().parse_webhook(YCLOUD_TEXT)

        assert getattr(normalized.channel, "value", normalized.channel) == "whatsapp"
        assert normalized.sender_identifier == "573001112233"
        assert normalized.text == "Hola, necesito ayuda"
        assert normalized.external_message_id == "wamid.TEST0000000000000001"
        assert normalized.media_url is None

    async def test_parse_imagen(self) -> None:
        """Una imagen llena media_url y usa el caption como texto."""
        normalized = await ycloud.YCloudProvider().parse_webhook(YCLOUD_IMAGE)

        assert normalized.media_url == "https://media.ycloud.com/ejemplo.jpg"
        assert getattr(normalized.media_type, "value", normalized.media_type) == "image"
        assert normalized.text == "Esta es la factura"

    async def test_firma_valida(self) -> None:
        """Una firma bien calculada valida."""
        body = json.dumps(YCLOUD_TEXT).encode()

        assert await ycloud.YCloudProvider().validate_signature(
            body, _firma_ycloud(body), WEBHOOK_SECRET
        )

    async def test_firma_de_otro_secreto_no_valida(self) -> None:
        """Una firma calculada con otro secreto se rechaza."""
        body = json.dumps(YCLOUD_TEXT).encode()
        ajena = _firma_ycloud(body, "otro-secreto")

        assert not await ycloud.YCloudProvider().validate_signature(body, ajena, WEBHOOK_SECRET)

    async def test_cuerpo_alterado_no_valida(self) -> None:
        """Si el cuerpo cambia despues de firmar, la firma deja de valer."""
        body = json.dumps(YCLOUD_TEXT).encode()
        firma = _firma_ycloud(body)

        assert not await ycloud.YCloudProvider().validate_signature(
            b'{"otro":1}', firma, WEBHOOK_SECRET
        )

    async def test_firma_vacia_no_valida(self) -> None:
        """Sin header de firma no se procesa nada."""
        body = json.dumps(YCLOUD_TEXT).encode()

        assert not await ycloud.YCloudProvider().validate_signature(body, "", WEBHOOK_SECRET)

    def test_constraints_de_whatsapp(self) -> None:
        """WhatsApp: 4096 chars y ventana de sesion de 24h con templates fuera."""
        constraints = ycloud.YCloudProvider().get_channel_constraints()

        assert constraints.max_text_length == 4096
        assert constraints.session_window_hours == 24
        assert constraints.requires_template_outside_window is True
