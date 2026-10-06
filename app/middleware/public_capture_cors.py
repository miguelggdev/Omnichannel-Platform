"""CORS abierto solo para la captura publica de leads (Sprint 16, ADR-083).

Un formulario en la web de un cliente (`https://www.tu-cliente.com`) hace `POST` a
`/api/v1/capture/{token}` desde un origen que no esta en `CORS_ORIGINS` y que no se puede
conocer de antemano. El `CORSMiddleware` global responderia 400 al preflight de cualquier origen
no listado, asi que esta ruta tiene su propio tratamiento, **por fuera** de ese middleware.

Por que `Access-Control-Allow-Origin: *` es seguro aqui y no en el resto de la API:

- Se responde `*` y **nunca** `Access-Control-Allow-Credentials`: el navegador no envia cookies
  ni cabecera `Authorization` en una peticion con comodin, asi que no hay sesion que robar.
- El endpoint no devuelve datos, solo `202 received`, y la unica credencial es el token de la
  URL, que por diseno es visible en el HTML de la pagina del cliente.
- Lo que frena el abuso no es el origen (que un bot falsea) sino el limite por IP y por fuente,
  el honeypot y el consentimiento.

Solo se toca `/api/v1/capture/`; cualquier otra ruta pasa intacta.
"""

from starlette.datastructures import MutableHeaders
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CAPTURE_PREFIX = "/api/v1/capture/"
_PREFLIGHT_MAX_AGE = "600"


class PublicCaptureCORSMiddleware:
    """Middleware ASGI: preflight propio y `Access-Control-Allow-Origin: *` en la captura."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Atiende el preflight de la captura y marca sus respuestas; el resto pasa de largo.

        Args:
            scope: Scope ASGI de la conexion.
            receive: Canal de entrada ASGI.
            send: Canal de salida ASGI.
        """
        if scope["type"] != "http" or not scope["path"].startswith(CAPTURE_PREFIX):
            await self.app(scope, receive, send)
            return

        if scope["method"] == "OPTIONS":
            respuesta = Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type",
                    "Access-Control-Max-Age": _PREFLIGHT_MAX_AGE,
                },
            )
            await respuesta(scope, receive, send)
            return

        async def con_cors(mensaje: Message) -> None:
            if mensaje["type"] == "http.response.start":
                cabeceras = MutableHeaders(scope=mensaje)
                # El CORSMiddleware global (por dentro) puede haber puesto ya el origen
                # concreto de un cliente permitido: no se pisa.
                if "access-control-allow-origin" not in cabeceras:
                    cabeceras["Access-Control-Allow-Origin"] = "*"
                    # El CORSMiddleware global anade `Allow-Credentials: true` a toda respuesta
                    # con `Origin`; junto al comodin es una combinacion que ningun navegador
                    # acepta y que ademas insinua credenciales que esta ruta no admite.
                    if "access-control-allow-credentials" in cabeceras:
                        del cabeceras["access-control-allow-credentials"]
            await send(mensaje)

        await self.app(scope, receive, con_cors)
