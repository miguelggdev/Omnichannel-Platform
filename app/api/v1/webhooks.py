"""Endpoint publico de webhooks entrantes — YCloud (WhatsApp) y Meta (IG/FB).

Este router es distinto al resto de la API:

- **No pasa por `TenantContextMiddleware`**: los webhooks no llevan JWT. El
  middleware exime el prefijo `/api/v1/webhooks/` (ver `WEBHOOK_PATHS_PREFIX`).
- **La autenticacion es por firma HMAC** del proveedor, no por token.
- **Debe responder 200 en <100ms**: aqui solo se valida, deduplica y encola.
  Todo el trabajo con base de datos ocurre en el worker de Celery.

Resolucion del provider
-----------------------
`app/services/messaging/factory.py` es archivo de Dev A (matriz Sprint 4) y a la
fecha de este commit todavia no esta entregado. Para no acoplar el arranque de la
app a esa entrega, la factory se importa de forma perezosa dentro de
`_resolve_provider()`: el modulo importa bien, la app levanta, el GET de
verificacion de Meta funciona, y solo el POST depende de la entrega de Dev A.
Los tests sustituyen `_resolve_provider` para aislarse del proveedor real.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.services.dedup import mark_if_new, release_mark

logger = logging.getLogger(__name__)

router = APIRouter()

# Codigos de error propios del receptor de webhooks.
INVALID_SIGNATURE = "INVALID_WEBHOOK_SIGNATURE"
UNSUPPORTED_PROVIDER = "UNSUPPORTED_PROVIDER"
VERIFICATION_FAILED = "WEBHOOK_VERIFICATION_FAILED"
QUEUE_UNAVAILABLE = "QUEUE_UNAVAILABLE"

# Header que transporta la firma HMAC-SHA256 de cada proveedor.
SIGNATURE_HEADERS: dict[str, str] = {
    "meta": "x-hub-signature-256",
    "ycloud": "X-Ycloud-Signature",
}


def _resolve_provider(provider: str, provider_config: dict[str, Any] | None = None) -> Any:
    """Obtiene el MessagingProvider correspondiente.

    Import perezoso a proposito: `app.services.messaging.factory` lo entrega Dev A
    y no debe bloquear el arranque de la app ni la coleccion de tests.

    Args:
        provider: Nombre del proveedor tomado de la URL (ycloud, meta).
        provider_config: Config extra del proveedor (para Meta, el sub-canal).

    Returns:
        Instancia del provider.

    Raises:
        AppException: 400 si el provider no esta registrado o aun no existe.
    """
    try:
        from app.services.messaging.factory import get_messaging_provider
    except ImportError as exc:  # pragma: no cover — desaparece cuando Dev A entregue
        logger.error("Factory de messaging no disponible: %s", exc)
        raise AppException(
            status_code=400,
            error_code=UNSUPPORTED_PROVIDER,
            message=f"Provider {provider} no soportado",
        ) from exc

    try:
        return get_messaging_provider(provider, provider_config)
    except ValueError as exc:
        raise AppException(
            status_code=400,
            error_code=UNSUPPORTED_PROVIDER,
            message=f"Provider {provider} no soportado",
        ) from exc


def _webhook_secret(provider: str) -> str:
    """Devuelve el secreto con el que se valida la firma del proveedor.

    Args:
        provider: Nombre del proveedor tomado de la URL.

    Returns:
        Secreto configurado en `.env`. Nunca se hardcodea.
    """
    settings = get_settings()
    if provider == "meta":
        return settings.META_APP_SECRET
    return settings.YCLOUD_WEBHOOK_SECRET


@router.post("/{provider}/{channel}")
async def receive_webhook(provider: str, channel: str, request: Request) -> JSONResponse:
    """Recibe un webhook entrante, lo deduplica y lo encola.

    Flujo: firma -> parseo -> dedup en Redis -> encolar en Celery -> 200.
    Ningun paso toca la base de datos, para sostener el presupuesto de <100ms.

    Args:
        provider: Proveedor de la URL (ycloud, meta).
        channel: Canal de la URL (whatsapp, instagram, facebook).
        request: Request crudo — se necesita el body sin parsear para el HMAC.

    Returns:
        200 con `queued`, `duplicate` o `parse_error`.

    Raises:
        AppException: 400 provider desconocido, 401 firma invalida,
            503 si no se pudo encolar (para que el proveedor reintente).
    """
    raw_body = await request.body()

    messaging_provider = _resolve_provider(provider, {"channel": channel})

    # ── 1. Firma HMAC ────────────────────────────────────────────────────────
    signature = request.headers.get(SIGNATURE_HEADERS.get(provider, ""), "")
    if not await messaging_provider.validate_signature(
        raw_body, signature, _webhook_secret(provider)
    ):
        logger.warning("Firma invalida en webhook provider=%s channel=%s", provider, channel)
        raise AppException(
            status_code=401,
            error_code=INVALID_SIGNATURE,
            message="Firma de webhook invalida",
        )

    # ── 2. Parseo a NormalizedMessage ────────────────────────────────────────
    # Un payload que no se puede parsear devuelve 200: si respondieramos 4xx/5xx el
    # proveedor reintentaria indefinidamente un mensaje que nunca va a poder procesarse.
    try:
        payload = json.loads(raw_body)
        normalized = await messaging_provider.parse_webhook(payload)
    except Exception:
        logger.exception("Error parseando webhook provider=%s channel=%s", provider, channel)
        return JSONResponse(status_code=200, content={"status": "parse_error"})

    message_channel = str(getattr(normalized.channel, "value", normalized.channel))
    external_id = normalized.external_message_id

    # ── 3. Deduplicacion (nivel 1: Redis) ────────────────────────────────────
    if not await mark_if_new(message_channel, external_id):
        logger.info("Webhook duplicado ignorado: %s", external_id)
        return JSONResponse(status_code=200, content={"status": "duplicate"})

    # ── 4. Encolar en Celery ─────────────────────────────────────────────────
    # `.delay()` es I/O sincrono contra Redis: va a threadpool para no bloquear el loop.
    try:
        from app.tasks.webhook_processor import process_incoming_message

        payload_for_task = normalized.model_dump(mode="json")
        await run_in_threadpool(
            process_incoming_message.delay,
            provider=provider,
            channel=channel,
            normalized_message=payload_for_task,
        )
    except Exception as exc:
        # La marca de dedup ya esta puesta; si no se libera, el reintento del
        # proveedor se descartaria como duplicado y el mensaje se perderia 24h.
        await release_mark(message_channel, external_id)
        logger.exception("No se pudo encolar el webhook %s", external_id)
        raise AppException(
            status_code=503,
            error_code=QUEUE_UNAVAILABLE,
            message="No se pudo encolar el mensaje, reintentar",
        ) from exc

    return JSONResponse(status_code=200, content={"status": "queued"})


@router.get("/{provider}/{channel}")
async def verify_webhook(provider: str, channel: str, request: Request) -> Response:
    """Verificacion de la URL del webhook (handshake del proveedor).

    Meta envia `hub.mode=subscribe`, `hub.verify_token` y `hub.challenge`, y espera
    el challenge devuelto como texto plano. YCloud usa un `challenge` simple.

    Args:
        provider: Proveedor de la URL (ycloud, meta).
        channel: Canal de la URL — no se usa, Meta verifica por app, no por canal.
        request: Request con los query params del handshake.

    Returns:
        El challenge en `text/plain`, o `{"status": "ok"}` si no hay challenge.

    Raises:
        AppException: 403 si el verify_token de Meta no coincide.
    """
    params = request.query_params

    if provider == "meta":
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge", "")
        expected = get_settings().META_WEBHOOK_VERIFY_TOKEN

        # `expected` vacio significa sin configurar: nunca debe validar por accidente.
        if mode == "subscribe" and expected and token == expected:
            return Response(content=challenge, media_type="text/plain")

        logger.warning("Verificacion de webhook Meta fallida (channel=%s)", channel)
        raise AppException(
            status_code=403,
            error_code=VERIFICATION_FAILED,
            message="Verificacion de webhook fallida",
        )

    simple_challenge = params.get("challenge")
    if simple_challenge:
        return Response(content=simple_challenge, media_type="text/plain")
    return JSONResponse(status_code=200, content={"status": "ok"})
