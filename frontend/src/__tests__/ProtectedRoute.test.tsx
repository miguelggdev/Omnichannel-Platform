import { render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import es from "../../messages/es.json";
import { ProtectedRoute } from "@/components/auth/ProtectedRoute";
import { useAuthStore } from "@/stores/authStore";
import type { UserRole } from "@/types";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));

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
  useAuthStore.getState().logout();
});

describe("ProtectedRoute", () => {
  it("sin sesion lleva a /login y no enseña el contenido", async () => {
    pintar();

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
    expect(screen.queryByText("contenido privado")).not.toBeInTheDocument();
  });

  it("con sesion y rol suficiente muestra el contenido", async () => {
    useAuthStore.getState().setTokens(tokenDe("agent"), "r");

    pintar("agent");

    expect(await screen.findByText("contenido privado")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it("con un rol insuficiente avisa en vez de mostrar el contenido", async () => {
    useAuthStore.getState().setTokens(tokenDe("agent"), "r");

    pintar("admin");

    expect(await screen.findByText("Sin acceso")).toBeInTheDocument();
    expect(screen.queryByText("contenido privado")).not.toBeInTheDocument();
  });

  it("un rol medical no entra ni en lo minimo del panel", async () => {
    useAuthStore.getState().setTokens(tokenDe("medical"), "r");

    pintar();

    expect(await screen.findByText("Sin acceso")).toBeInTheDocument();
  });
});
