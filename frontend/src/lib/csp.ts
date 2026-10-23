/**
 * Content-Security-Policy con nonce por peticion.
 *
 * `script-src` solo admite scripts con el nonce de la peticion (Next lo aplica a los suyos al ver
 * la cabecera; `next-themes` lo recibe por prop) y los que ellos carguen (`strict-dynamic`). Un
 * script inyectado por XSS no lo tiene y no corre. Los estilos conservan `'unsafe-inline'`: Next,
 * Tailwind y sonner inyectan `<style>` y atributos `style`, y un estilo no ejecuta codigo.
 * El navegador solo habla con su propio origen (`connect-src 'self'`): la API pasa por el rewrite.
 */

export function crearNonce(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return btoa(String.fromCharCode(...bytes));
}

export function construirCsp(nonce: string, opciones: { desarrollo: boolean }): string {
  const { desarrollo } = opciones;
  const directivas = [
    "default-src 'self'",
    // En desarrollo React necesita eval para las trazas; en produccion no.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${desarrollo ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' blob: data: https://*.supabase.co",
    "font-src 'self'",
    `connect-src 'self'${desarrollo ? " ws:" : ""}`,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ];
  if (!desarrollo) directivas.push("upgrade-insecure-requests");
  return directivas.join("; ");
}
