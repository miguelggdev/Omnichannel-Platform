"""WebhookDispatcher — firma y envio de webhooks salientes (Sprint 11).

Firma
------
HMAC-SHA256 sobre `"{timestamp}.{body}"`, no sobre el body solo: incluir el
timestamp en lo firmado es lo que permite al receptor rechazar un replay
(descarta lo que tenga mas de unos minutos sin que nadie pueda re-firmar un
timestamp nuevo). Viaja en `X-Webhook-Signature: sha256=<hex>`.

El body que se firma es exactamente el que se manda (`content=body`, no
`json=payload`): si se serializara dos veces, cualquier diferencia de orden o
de espacios haria que la firma no verificara del otro lado.

SSRF
-----
La URL la elige el tenant, y el que hace el POST es un worker dentro de la red
privada del despliegue: sin control, un tenant puede apuntar un webhook a
`http://169.254.169.254/...` (metadata de la nube), a Redis o a la propia API
interna y usar la plataforma como proxy. Antes de cada envio se resuelve el
host y se rechaza si alguna de sus direcciones es privada, loopback,
link-local, multicast o reservada. Tambien se exige http/https y no se siguen
redirecciones (una redireccion a 127.0.0.1 esquivaria el control).

Limite conocido: entre la comprobacion y la conexion hay una ventana de DNS
rebinding. Cerrarla exige conectarse a la IP ya resuelta forzando el `Host`,
lo que rompe SNI/TLS por nombre; queda documentado en ADR-065 y fuera de este
sprint.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Cuanto del cuerpo de la respuesta se guarda en el log del envio.
RESPONSE_BODY_MAX_CHARS = 1000

# Cuanto del cuerpo se repite dentro del mensaje de error.
ERROR_BODY_MAX_CHARS = 500

USER_AGENT = "ConversationalAI-Webhook/1.0"


class WebhookTargetError(ValueError):
    """La URL destino no es un destino valido para un webhook saliente."""


class DestinoDeWebhook(Protocol):
    """Lo unico que necesita el dispatcher para firmar y enviar.

    Lo cumple tanto el modelo `TenantWebhook` como la copia suelta que arma la
    task para poder cerrar la transaccion antes del POST.
    """

    # Declarados como propiedades de solo lectura: asi lo cumple tanto el
    # modelo (atributos normales) como la copia inmutable de la task.
    @property
    def id(self) -> Any: ...

    @property
    def url(self) -> str: ...

    @property
    def secret(self) -> str: ...

    @property
    def headers(self) -> dict[str, Any]: ...


async def _direcciones_del_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resuelve un host a todas sus direcciones IP.

    Usa el `getaddrinfo` del event loop, que resuelve en su propio executor:
    el `socket.getaddrinfo` de la libreria estandar es una llamada bloqueante
    y aca estamos en contexto async (regla 4 de CLAUDE.md). Un DNS lento
    dejaria parado todo el loop del worker, no solo este envio.

    Args:
        host: Nombre o literal IP tomado de la URL.

    Returns:
        Direcciones resueltas.

    Raises:
        WebhookTargetError: Si el host no resuelve.
    """
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise WebhookTargetError(f"El host {host} no resuelve") from exc

    return [ipaddress.ip_address(info[4][0]) for info in infos]


async def validar_destino(url: str) -> None:
    """Rechaza destinos que apunten a la red interna del despliegue.

    Args:
        url: URL configurada por el tenant.

    Raises:
        WebhookTargetError: Si el esquema no es http/https, si falta el host,
            si no resuelve o si alguna de sus IPs no es publica.
    """
    partes = urlparse(url)
    if partes.scheme not in ("http", "https"):
        raise WebhookTargetError(f"Esquema no permitido: {partes.scheme or '(vacio)'}")
    if not partes.hostname:
        raise WebhookTargetError("La URL no tiene host")

    if get_settings().OUTGOING_WEBHOOK_ALLOW_PRIVATE_HOSTS:
        return

    for direccion in await _direcciones_del_host(partes.hostname):
        if not direccion.is_global or direccion.is_multicast:
            raise WebhookTargetError(
                f"El host {partes.hostname} resuelve a una direccion no publica ({direccion})"
            )


def _cabeceras_del_tenant(headers: dict[str, Any] | None) -> dict[str, str]:
    """Sanea las cabeceras extra que configuro el tenant.

    Vienen de una columna JSONB, asi que pueden traer cualquier cosa: un valor
    numerico revienta el envio con un `AttributeError` que no es un
    `httpx.HTTPError` y por lo tanto se escapaba del manejo de errores (el
    intento moria sin dejar log ni tocar los contadores), y un salto de linea
    en el valor es un intento de inyeccion de cabeceras.

    Args:
        headers: Cabeceras configuradas por el tenant, tal como estan en JSONB.

    Returns:
        Solo las utilizables, con clave y valor en texto.
    """
    limpias: dict[str, str] = {}
    for clave, valor in (headers or {}).items():
        if valor is None or isinstance(valor, (dict, list)):
            logger.warning("Cabecera %r del webhook ignorada: valor no es escalar", clave)
            continue
        clave_txt, valor_txt = str(clave), str(valor)
        if any(c in clave_txt or c in valor_txt for c in ("\r", "\n")):
            logger.warning("Cabecera %r del webhook ignorada: contiene saltos de linea", clave)
            continue
        limpias[clave_txt] = valor_txt
    return limpias


class WebhookDispatcher:
    """Firma y envia el POST a un webhook saliente."""

    def compute_signature(self, body: str, secret: str, timestamp: str) -> str:
        """Calcula la firma HMAC-SHA256 de un envio.

        Args:
            body: Cuerpo exacto que se manda.
            secret: Secreto compartido con el receptor.
            timestamp: Segundos epoch del envio, como cadena.

        Returns:
            Firma en hexadecimal.
        """
        mensaje = f"{timestamp}.{body}"
        return hmac.new(secret.encode("utf-8"), mensaje.encode("utf-8"), hashlib.sha256).hexdigest()

    def build_headers(
        self, webhook: DestinoDeWebhook, payload: dict[str, Any], body: str
    ) -> tuple[dict[str, str], str]:
        """Arma las cabeceras del envio, firma incluida.

        Las cabeceras propias se ponen despues de las del tenant: un tenant no
        puede pisar su propia firma con un `headers` mal configurado.

        Args:
            webhook: Configuracion del webhook.
            payload: Evento completo.
            body: Cuerpo serializado que se va a firmar y mandar.

        Returns:
            `(headers, timestamp)`.
        """
        timestamp = str(int(time.time()))
        firma = self.compute_signature(body, webhook.secret, timestamp)
        return {
            **_cabeceras_del_tenant(webhook.headers),
            "Content-Type": "application/json",
            "X-Webhook-Signature": f"sha256={firma}",
            "X-Webhook-Event": str(payload.get("event", "unknown")),
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-ID": str(payload.get("webhook_delivery_id", "")),
            "User-Agent": USER_AGENT,
        }, timestamp

    async def send_webhook(
        self, webhook: DestinoDeWebhook, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Envia el evento al webhook y describe como fue.

        No lanza: todo fallo (destino invalido, timeout, error de red, 5xx del
        receptor) vuelve como `success=False` con su motivo, para que quien
        llama lo registre y decida si reintenta.

        Args:
            webhook: Configuracion del webhook destino.
            payload: Evento completo (`event`, `timestamp`, `webhook_delivery_id`, `data`).

        Returns:
            `{"success": bool, "status_code": int | None, "duration_ms": int,
            "response_body": str | None, "error": str | None}`.
        """
        body = json.dumps(payload, default=str, sort_keys=True)
        inicio = time.monotonic()

        def _transcurrido() -> int:
            return int((time.monotonic() - inicio) * 1000)

        try:
            await validar_destino(webhook.url)
        except WebhookTargetError as exc:
            logger.warning("Destino rechazado para el webhook %s: %s", webhook.id, exc)
            return {"success": False, "duration_ms": _transcurrido(), "error": str(exc)}

        headers, _ = self.build_headers(webhook, payload, body)
        timeout = get_settings().OUTGOING_WEBHOOK_TIMEOUT_SECONDS

        try:
            # follow_redirects=False a proposito: una redireccion a una IP
            # interna esquivaria validar_destino().
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.post(webhook.url, content=body, headers=headers)
        except httpx.TimeoutException:
            return {
                "success": False,
                "duration_ms": _transcurrido(),
                "error": f"Timeout tras {timeout} segundos",
            }
        except httpx.HTTPError as exc:
            return {
                "success": False,
                "duration_ms": _transcurrido(),
                "error": f"Error de red: {exc}",
            }

        duration_ms = _transcurrido()
        cuerpo = response.text[:RESPONSE_BODY_MAX_CHARS]

        if 200 <= response.status_code < 300:
            return {
                "success": True,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "response_body": cuerpo,
            }

        return {
            "success": False,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
            "response_body": cuerpo,
            "error": f"HTTP {response.status_code}: {response.text[:ERROR_BODY_MAX_CHARS]}",
        }
