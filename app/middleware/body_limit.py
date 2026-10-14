"""Tope del cuerpo de las peticiones, contado sobre lo que llega de verdad (ADR-088).

Los endpoints comprobaban `Content-Length`, pero esa cabecera la escribe el cliente: con
`Transfer-Encoding: chunked` no la hay, y `await request.body()` (o el parseo de un modelo
Pydantic, que FastAPI hace antes del handler) cargaba en memoria lo que mandaran. Varios de
esos endpoints son publicos y leen el cuerpo **antes** de autenticar (el HMAC lo necesita).

Este middleware ASGI va por fuera de todo:

- Con `Content-Length` por encima del tope responde 413 sin llamar a la app.
- Sin ella (o mintiendo), cuenta los bytes de cada `http.request`; al pasarse corta la lectura
  y, si la app aun no respondio, responde 413 en su lugar.

El tope depende de la ruta (el prefijo mas largo que coincida); los endpoints conservan sus
propios topes, mas finos.
"""

import json
from typing import Any

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
MB = 1024 * 1024

#: Tope por defecto: ningun JSON de la API se acerca a 1 MB.
TOPE_POR_DEFECTO = 1 * MB

#: Topes por prefijo de ruta. Gana el prefijo mas largo.
TOPES_POR_RUTA: dict[str, int] = {
    # Inbound Parse de email: puede traer adjuntos (SendGrid admite 30 MB).
    "/api/v1/webhooks/email/": 32 * MB,
    # YCloud, Meta y Telegram mandan pocos KB; Meta agrupa eventos, de ahi el margen.
    "/api/v1/webhooks/": 2 * MB,
    # Documentos del RAG: 50 MB por archivo (`documents.MAX_FILE_SIZE`) mas el multipart.
    "/api/v1/documents": 51 * MB,
    # Importacion CSV de leads: 2 MB (`lead_import.MAX_BYTES`) mas el multipart.
    "/api/v1/leads": 3 * MB,
}


def tope_para(ruta: str) -> int:
    """Tope de bytes del cuerpo para una ruta.

    Args:
        ruta: Path de la peticion.

    Returns:
        El tope del prefijo mas largo que coincida, o `TOPE_POR_DEFECTO`.
    """
    mejor = ""
    for prefijo in TOPES_POR_RUTA:
        if ruta.startswith(prefijo) and len(prefijo) > len(mejor):
            mejor = prefijo
    return TOPES_POR_RUTA[mejor] if mejor else TOPE_POR_DEFECTO


class CuerpoDemasiadoGrandeError(HTTPException):
    """Se lanza dentro de `receive` al pasarse del tope, para cortar la lectura."""

    def __init__(self) -> None:
        """413 con el mismo mensaje que la respuesta del middleware."""
        super().__init__(status_code=413, detail="Cuerpo demasiado grande")


async def _responder_413(send: Send) -> None:
    """Envia el 413 con el formato de `AppException`.

    Args:
        send: Canal ASGI de salida.
    """
    cuerpo = json.dumps(
        {"error_code": PAYLOAD_TOO_LARGE, "message": "El cuerpo de la peticion es demasiado grande"}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(cuerpo)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": cuerpo})


class BodySizeLimitMiddleware:
    """Corta las peticiones cuyo cuerpo supere el tope de su ruta."""

    def __init__(self, app: ASGIApp) -> None:
        """Envuelve la app.

        Args:
            app: Aplicacion ASGI interna.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Aplica el tope en peticiones HTTP; el resto pasa sin tocar.

        Args:
            scope: Scope ASGI.
            receive: Canal de entrada.
            send: Canal de salida.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        tope = tope_para(scope.get("path", ""))
        for nombre, valor in scope.get("headers", []):
            if nombre == b"content-length":
                declarado = valor.decode("latin-1").strip()
                if declarado.isdigit() and int(declarado) > tope:
                    await _responder_413(send)
                    return

        estado: dict[str, Any] = {
            "leidos": 0,
            "excedido": False,
            "iniciado": False,
            "respondido": False,
        }

        async def receive_acotado() -> Message:
            mensaje = await receive()
            if mensaje["type"] == "http.request":
                estado["leidos"] += len(mensaje.get("body", b""))
                if estado["leidos"] > tope:
                    # Si la app ya empezo a responder no se puede cambiar el estado HTTP.
                    estado["excedido"] = not estado["iniciado"]
                    raise CuerpoDemasiadoGrandeError()
            return mensaje

        async def send_controlado(mensaje: Message) -> None:
            if estado["excedido"]:
                # La app respondio a la excepcion a su manera: se sustituye por el 413 propio.
                if not estado["respondido"]:
                    estado["respondido"] = True
                    await _responder_413(send)
                return
            if mensaje["type"] == "http.response.start":
                estado["iniciado"] = True
            await send(mensaje)

        await self.app(scope, receive_acotado, send_controlado)
