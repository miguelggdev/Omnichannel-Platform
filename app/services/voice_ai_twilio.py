"""Voz IA para las llamadas agendadas, sobre el canal de voz propio con Twilio (Sprint 19, Dev B).

ADR-087 dejo abierta la eleccion del proveedor (Vapi o Bland, segun el spec, o el canal de voz del
Sprint 13). Se usa **el del Sprint 13**: ya habla con Twilio, ya tiene agente de voz (STT por
frases, TTS, interrupciones) y no anade un tercero que reciba el telefono del lead ni otro gasto.
Vapi y Bland siguen siendo posibles: son otra subclase de `VoiceCallProvider` registrada con su
nombre.

Limites heredados del canal de voz (ADR-030): el numero de Twilio es de **un** tenant
(`DEFAULT_CLIENT_ID`), asi que solo ese tenant puede iniciar llamadas con voz IA. Para otro tenant
`start_call` falla con `VoiceProviderNotConfiguredError` (no se llama desde el numero de otro).

El agente de la llamada es el mismo del canal de voz entrante (mismo saludo y mismo grafo); el
`context` y el `config` de la peticion todavia no personalizan el guion: quedan para cuando el canal
de voz tenga agentes por llamada.
"""

import logging
import re
from typing import Any

import httpx

from app.agents.nodes._tenant import ChannelNotConfiguredError, get_channel_config
from app.core.config import get_settings
from app.services.messaging.voice_provider import (
    TWILIO_TIMEOUT_SECONDS,
    TwilioAPIError,
    TwilioVoiceProvider,
    faltan_credenciales_salientes,
)
from app.services.voice_call_provider import (
    OutboundCallRequest,
    ProviderCallRef,
    VoiceCallProvider,
    VoiceProviderError,
    VoiceProviderNotConfiguredError,
    VoiceProviderTemporaryError,
    register_voice_provider,
)

logger = logging.getLogger(__name__)

NOMBRE = "twilio"

_ESTADO_EN_MENSAJE = re.compile(r"\((\d{3}),")


def _es_temporal(exc: TwilioAPIError) -> bool:
    """Si el fallo de Twilio es pasajero: no contesto, 429 o 5xx. Un 4xx es permanente."""
    texto = str(exc)
    encontrado = _ESTADO_EN_MENSAJE.search(texto)
    if encontrado is None:
        return "no respondio" in texto
    estado = int(encontrado.group(1))
    return estado >= 500 or estado == 429


@register_voice_provider
class TwilioAiVoiceProvider(VoiceCallProvider):
    """Llamadas salientes con el agente de voz del canal de Twilio."""

    name = NOMBRE

    @staticmethod
    def _configuracion(request_client: str) -> tuple[dict[str, Any], str]:
        """Credenciales y URL publica, o `VoiceProviderNotConfiguredError`.

        Args:
            request_client: Tenant que pide la llamada.

        Returns:
            `(channel_config, base_url)`.
        """
        ajustes = get_settings()
        if request_client != str(ajustes.DEFAULT_CLIENT_ID):
            raise VoiceProviderNotConfiguredError(
                "El canal de voz no esta habilitado para este tenant"
            )
        try:
            _, config = get_channel_config("voice")
        except ChannelNotConfiguredError as exc:
            raise VoiceProviderNotConfiguredError("Canal de voz no configurado") from exc
        base = ajustes.VOICE_PUBLIC_BASE_URL.rstrip("/")
        if faltan_credenciales_salientes(config) or not base:
            raise VoiceProviderNotConfiguredError("Canal de voz no configurado")
        return config, base

    async def start_call(self, request: OutboundCallRequest) -> ProviderCallRef:
        """Inicia la llamada con Twilio; el agente habla cuando el lead contesta.

        Args:
            request: Lo que hay que llamar (`to_phone` en E.164 con `+`).

        Returns:
            El `CallSid` como referencia.

        Raises:
            VoiceProviderNotConfiguredError: Canal sin configurar o de otro tenant.
            VoiceProviderTemporaryError: Twilio no contesto, 429 o 5xx.
            VoiceProviderError: Twilio la rechazo (numero invalido...).
        """
        config, base = self._configuracion(str(request.client_id))
        try:
            sid = await TwilioVoiceProvider().start_call(
                request.to_phone,
                config,
                answer_url=f"{base}/api/v1/voice/twilio/incoming",
                status_callback_url=f"{base}/api/v1/voice/twilio/status",
            )
        except TwilioAPIError as exc:
            # El texto trae el codigo de Twilio, no el telefono: se puede propagar.
            if _es_temporal(exc):
                raise VoiceProviderTemporaryError("Twilio no pudo iniciar la llamada") from exc
            raise VoiceProviderError("Twilio rechazo la llamada") from exc
        return ProviderCallRef(provider=NOMBRE, external_id=sid, status="queued")

    async def cancel_call(self, ref: ProviderCallRef) -> bool:
        """Cuelga la llamada en Twilio (`Status=completed`).

        Args:
            ref: Referencia de `start_call`.

        Returns:
            `True` si Twilio la colgo; `False` si ya no estaba activa.

        Raises:
            VoiceProviderNotConfiguredError: Canal sin configurar.
            VoiceProviderError: Si Twilio no contesta o rechaza la peticion.
        """
        ajustes = get_settings()
        config, _ = self._configuracion(str(ajustes.DEFAULT_CLIENT_ID))
        sid = config["account_sid"]
        url = f"{config['api_base_url'].rstrip('/')}/2010-04-01/Accounts/{sid}/Calls/{ref.external_id}.json"
        try:
            async with httpx.AsyncClient(timeout=TWILIO_TIMEOUT_SECONDS) as cliente:
                respuesta = await cliente.post(
                    url, data={"Status": "completed"}, auth=(sid, config["auth_token"])
                )
        except httpx.HTTPError as exc:
            raise VoiceProviderError(f"Twilio no respondio: {type(exc).__name__}") from exc
        if respuesta.status_code == 404 or respuesta.status_code == 400:
            return False  # ya no estaba activa
        if respuesta.status_code >= 300:
            raise VoiceProviderError(f"Twilio rechazo la peticion ({respuesta.status_code})")
        return True
