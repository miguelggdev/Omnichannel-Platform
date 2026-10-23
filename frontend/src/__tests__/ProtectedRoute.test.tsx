import { render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import es from "../../messages/es.json";
import { ProtectedRoute } from "@/components/auth/ProtectedRoute";
import { useAuthStore } from "@/stores/authStore";
import type { UserRole } from "@/types";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));

const refreshAccessToken = vi.fn();
vi.mock("@/lib/api", () => ({ refreshAccessToken: () => refreshAccessToken() }));

function tokenDe(role: UserRole): string {
  const p = Buffer.from(JSON.stringify({ user_id: "u", client_id: "c", email: "a@b.c", role }))
    .toString("base64")
    .replace(/=+$/, "");
  return `x.${p}.y`;
}

function pintar(minRole?: UserRole) {
  return render(
    <NextIntlClientProvider locale="es" messages={es}>
      <ProtectedRoute minRole={minRole}>
        <p>contenido privado</p>
      </ProtectedRoute>
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  replace.mockReset();
  refreshAccessToken.mockReset();
  refreshAccessToken.mockRejectedValue(new Error("sin cookie"));
  useAuthStore.getState().logout();
});

describe("ProtectedRoute", () => {
  it("sin sesion lleva a /login y no enseña el contenido", async () => {
    pintar();

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
    expect(screen.queryByText("contenido privado")).not.toBeInTheDocument();
    // Intenta recuperar la sesion con la cookie una sola vez, sin bucle.
    expect(refreshAccessToken).toHaveBeenCalledTimes(1);
  });

  it("tras recargar recupera la sesion con la cookie y muestra el contenido", async () => {
    refreshAccessToken.mockImplementation(async () => {
      const token = tokenDe("agent");
      useAuthStore.getState().setTokens(token);
      return token;
    });

    pintar("agent");

    expect(await screen.findByText("contenido privado")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it("con sesion y rol suficiente muestra el contenido", async () => {
    useAuthStore.getState().setTokens(tokenDe("agent"));

    pintar("agent");

    expect(await screen.findByText("contenido privado")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it("con un rol insuficiente avisa en vez de mostrar el contenido", async () => {
    useAuthStore.getState().setTokens(tokenDe("agent"));

    pintar("admin");

    expect(await screen.findByText("Sin acceso")).toBeInTheDocument();
    expect(screen.queryByText("contenido privado")).not.toBeInTheDocument();
  });

  it("un rol medical no entra ni en lo minimo del panel", async () => {
    useAuthStore.getState().setTokens(tokenDe("medical"));

    pintar();

    expect(await screen.findByText("Sin acceso")).toBeInTheDocument();
  });
});
