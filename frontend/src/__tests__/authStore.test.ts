import { beforeEach, describe, expect, it } from "vitest";
import { useAuthStore } from "@/stores/authStore";
import type { UserRole } from "@/types";

function tokenDe(role: UserRole): string {
  const payload = Buffer.from(JSON.stringify({ user_id: "u", client_id: "c", email: "a@b.c", role }))
    .toString("base64")
    .replace(/=+$/, "");
  return `x.${payload}.y`;
}

beforeEach(() => useAuthStore.getState().logout());

describe("authStore", () => {
  it("al guardar los tokens deduce el usuario del access token", () => {
    useAuthStore.getState().setTokens(tokenDe("supervisor"), "refresh");
    expect(useAuthStore.getState().user?.role).toBe("supervisor");
  });

  it("logout olvida tokens y usuario", () => {
    useAuthStore.getState().setTokens(tokenDe("admin"), "refresh");
    useAuthStore.getState().logout();
    const s = useAuthStore.getState();
    expect([s.accessToken, s.refreshToken, s.user]).toEqual([null, null, null]);
  });

  it("sin sesion nadie tiene ningun rol", () => {
    expect(useAuthStore.getState().hasMinRole("agent")).toBe(false);
  });

  it.each<[UserRole, UserRole, boolean]>([
    ["super_admin", "admin", true],
    ["super_admin", "super_admin", true],
    ["admin", "super_admin", false],
    ["admin", "supervisor", true],
    ["supervisor", "admin", false],
    ["agent", "supervisor", false],
    ["agent", "agent", true],
    // medical es un rol clinico: no lee conversaciones ni contactos, ni siquiera como agent.
    ["medical", "agent", false],
  ])("%s frente al minimo %s: %s", (rol, minimo, esperado) => {
    useAuthStore.getState().setTokens(tokenDe(rol), "refresh");
    expect(useAuthStore.getState().hasMinRole(minimo)).toBe(esperado);
  });
});
