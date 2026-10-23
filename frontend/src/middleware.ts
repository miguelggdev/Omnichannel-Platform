import { NextResponse, type NextRequest } from "next/server";
import { construirCsp, crearNonce } from "@/lib/csp";

/**
 * Pone una CSP con nonce nuevo en cada peticion. Next lee la cabecera de la *peticion* para
 * firmar sus scripts; el layout lee `x-nonce` para `next-themes`. La respuesta lleva la misma CSP.
 */
export function middleware(request: NextRequest) {
  const nonce = crearNonce();
  const csp = construirCsp(nonce, { desarrollo: process.env.NODE_ENV !== "production" });

  const cabeceras = new Headers(request.headers);
  cabeceras.set("x-nonce", nonce);
  cabeceras.set("Content-Security-Policy", csp);

  const respuesta = NextResponse.next({ request: { headers: cabeceras } });
  respuesta.headers.set("Content-Security-Policy", csp);
  return respuesta;
}

export const config = {
  // No hace falta en estaticos ni en el health check; los prefetch no llevan nonce.
  matcher: [
    {
      source: "/((?!api/health|_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
