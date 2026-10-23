import { describe, expect, it } from "vitest";
import { construirCsp, crearNonce } from "@/lib/csp";

describe("CSP con nonce", () => {
  it("el nonce cambia en cada llamada y no esta vacio", () => {
    const a = crearNonce();
    expect(a.length).toBeGreaterThanOrEqual(22);
    expect(crearNonce()).not.toBe(a);
  });

  it("produccion: script-src con nonce, sin unsafe-inline ni unsafe-eval", () => {
    const csp = construirCsp("abc", { desarrollo: false });
    const scripts = csp.split("; ").find((d) => d.startsWith("script-src"))!;
    expect(scripts).toContain("'nonce-abc'");
    expect(scripts).not.toContain("unsafe-inline");
    expect(scripts).not.toContain("unsafe-eval");
    expect(csp).toContain("object-src 'none'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("upgrade-insecure-requests");
    expect(csp).toContain("connect-src 'self'");
  });

  it("desarrollo permite eval y websocket, y no fuerza https", () => {
    const csp = construirCsp("abc", { desarrollo: true });
    expect(csp).toContain("'unsafe-eval'");
    expect(csp).toContain("ws:");
    expect(csp).not.toContain("upgrade-insecure-requests");
  });
});
