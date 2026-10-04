import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import es from "../../messages/es.json";
import { LoginForm } from "@/components/auth/LoginForm";

const login = vi.fn();

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ login, pending: false, error: null }),
}));

function pintar() {
  return render(
    <NextIntlClientProvider locale="es" messages={es}>
      <LoginForm />
    </NextIntlClientProvider>,
  );
}

beforeEach(() => login.mockReset());

describe("LoginForm", () => {
  it("no envia nada y avisa si el correo o la contrasena no son validos", async () => {
    pintar();

    await userEvent.type(screen.getByLabelText("Correo electrónico"), "no-es-un-correo");
    await userEvent.type(screen.getByLabelText("Contraseña"), "corta");
    await userEvent.click(screen.getByRole("button", { name: "Iniciar sesión" }));

    expect(await screen.findByText("Introduce un correo válido")).toBeInTheDocument();
    expect(screen.getByText("Mínimo 8 caracteres")).toBeInTheDocument();
    expect(login).not.toHaveBeenCalled();
  });

  it("envia las credenciales cuando son validas", async () => {
    pintar();

    await userEvent.type(screen.getByLabelText("Correo electrónico"), "ana@example.com");
    await userEvent.type(screen.getByLabelText("Contraseña"), "una-clave-larga");
    await userEvent.click(screen.getByRole("button", { name: "Iniciar sesión" }));

    await waitFor(() =>
      expect(login).toHaveBeenCalledWith({ email: "ana@example.com", password: "una-clave-larga" }),
    );
  });

  it("marca el campo con error como invalido para los lectores de pantalla", async () => {
    pintar();

    await userEvent.click(screen.getByRole("button", { name: "Iniciar sesión" }));

    await waitFor(() => expect(screen.getByLabelText("Correo electrónico")).toHaveAttribute("aria-invalid", "true"));
  });
});
