import { describe, expect, it } from "vitest";
import { userFromToken } from "@/lib/jwt";

function token(payload: Record<string, unknown>): string {
  const b64 = (o: unknown) =>
    Buffer.from(JSON.stringify(o)).toString("base64").replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
  return `${b64({ alg: "HS256", typ: "JWT" })}.${b64(payload)}.firma`;
}

describe("userFromToken", () => {
  it("lee los claims del backend", () => {
    const user = userFromToken(
      token({ user_id: "u1", client_id: "c1", email: "ana@example.com", role: "admin", exp: 1, type: "access" }),
    );
    expect(user).toEqual({ id: "u1", client_id: "c1", email: "ana@example.com", role: "admin" });
  });

  it("decodifica bien un email con caracteres no ASCII", () => {
    const user = userFromToken(token({ user_id: "u", client_id: "c", email: "ñandú@example.com", role: "agent" }));
    expect(user?.email).toBe("ñandú@example.com");
  });

  it.each([
    ["sin payload", "abc"],
    ["no es base64 ni JSON", "a.%%%.c"],
    ["falta un claim", token({ user_id: "u", client_id: "c", role: "admin" })],
    ["rol desconocido", token({ user_id: "u", client_id: "c", email: "a@b.c", role: "root" })],
  ])("devuelve null si %s", (_caso, t) => {
    expect(userFromToken(t)).toBeNull();
  });
});
