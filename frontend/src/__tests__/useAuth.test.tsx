import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "@/hooks/useAuth";
import { useAuthStore } from "@/stores/authStore";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace, refresh: vi.fn() }) }));
vi.mock("@/hooks/usePreferences", () => ({ usePreferences: () => ({ loadFromBackend: vi.fn() }) }));

const apiPost = vi.fn();
vi.mock("@/lib/api", () => ({
  apiPost: (...args: unknown[]) => apiPost(...args),
  errorMessage: () => "error",
}));

function token(): string {
  const p = Buffer.from(JSON.stringify({ user_id: "u", client_id: "c", email: "a@b.c", role: "admin" }))
    .toString("base64")
    .replace(/=+$/, "");
  return `x.${p}.y`;
}

function montar() {
  const queryClient = new QueryClient();
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return renderHook(() => useAuth(), { wrapper });
}

beforeEach(() => {
  replace.mockReset();
  apiPost.mockReset();
  useAuthStore.getState().logout();
});

describe("useAuth", () => {
  it("el login guarda solo el access token: el refresh lo deja el backend en la cookie", async () => {
    const t = token();
    apiPost.mockResolvedValue({ access_token: t, token_type: "bearer" });
    const { result } = montar();

    await act(() => result.current.login({ email: "a@b.c", password: "12345678" }));

    expect(useAuthStore.getState().accessToken).toBe(t);
  });

  it("logout avisa al backend para anular el refresh y olvida la sesion", async () => {
    useAuthStore.getState().setTokens(token());
    apiPost.mockResolvedValue(undefined);
    const { result } = montar();

    await act(() => result.current.logout());

    expect(apiPost).toHaveBeenCalledWith("/auth/logout", {});
    expect(useAuthStore.getState().accessToken).toBeNull();
    expect(replace).toHaveBeenCalledWith("/login");
  });

  it("si el backend no responde, logout cierra la sesion local igual", async () => {
    useAuthStore.getState().setTokens(token());
    apiPost.mockRejectedValue(new Error("sin red"));
    const { result } = montar();

    await act(() => result.current.logout());

    expect(useAuthStore.getState().accessToken).toBeNull();
    expect(replace).toHaveBeenCalledWith("/login");
  });
});
