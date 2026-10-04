import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import es from "../../messages/es.json";
import { OnboardingWizard } from "@/components/auth/OnboardingWizard";
import { VerifyEmail } from "@/components/auth/VerifyEmail";
import { accountStepSchema, passwordSchema } from "@/lib/onboarding";
import type { OnboardingFailure } from "@/hooks/useOnboarding";

const registerFn = vi.fn();
let failure: OnboardingFailure | null = null;
const apiPost = vi.fn();

vi.mock("@/hooks/useOnboarding", () => ({
  useOnboarding: () => ({ register: registerFn, pending: false, failure }),
}));
vi.mock("@/lib/api", () => ({ apiPost: (...a: unknown[]) => apiPost(...a) }));
vi.mock("next/link", () => ({
  default: ({ href, children }: { href: string; children: React.ReactNode }) => (
    <a href={href}>{children}</a>
  ),
}));

function pintar(ui: React.ReactElement) {
  return render(
    <NextIntlClientProvider locale="es" messages={es}>
      {ui}
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  registerFn.mockReset();
  apiPost.mockReset();
  failure = null;
});

describe("reglas de contraseña (espejo del backend)", () => {
  it.each([
    ["corta1A", false],
    ["todominusculas1", false],
    ["TODOMAYUSCULAS1", false],
    ["SinNumeroAlguno", false],
    ["ClaveSegura1", true],
    ["A1" + "a".repeat(71), false],
  ])("%s -> %s", (clave, valida) => {
    expect(passwordSchema.safeParse(clave).success).toBe(valida);
  });

  it("exige que las dos contraseñas coincidan", () => {
    const r = accountStepSchema.safeParse({
      fullName: "Ana Gómez",
      email: "ana@example.com",
      password: "ClaveSegura1",
      confirmPassword: "ClaveSegura2",
    });
    expect(r.success).toBe(false);
  });
});

async function pasoNegocio(nombre = "Panadería La Espiga") {
  await userEvent.type(screen.getByLabelText("Nombre del negocio"), nombre);
  await userEvent.click(screen.getByRole("button", { name: "Siguiente" }));
}

async function pasoCuenta(over: Partial<Record<string, string>> = {}) {
  const v = {
    nombre: "Ana Gómez",
    email: "ana@example.com",
    pass: "ClaveSegura1",
    pass2: "ClaveSegura1",
    ...over,
  };
  await userEvent.type(screen.getByLabelText("Tu nombre completo"), v.nombre!);
  await userEvent.type(screen.getByLabelText("Correo electrónico"), v.email!);
  await userEvent.type(screen.getByLabelText("Contraseña"), v.pass!);
  await userEvent.type(screen.getByLabelText("Repite la contraseña"), v.pass2!);
  await userEvent.click(screen.getByRole("button", { name: "Siguiente" }));
}

describe("OnboardingWizard", () => {
  it("no avanza del primer paso sin nombre de negocio", async () => {
    pintar(<OnboardingWizard />);
    await userEvent.click(screen.getByRole("button", { name: "Siguiente" }));
    expect(await screen.findByText(/Escribe el nombre del negocio/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Tu nombre completo")).not.toBeInTheDocument();
  });

  it("avisa si las contraseñas no coinciden y no pasa al resumen", async () => {
    pintar(<OnboardingWizard />);
    await pasoNegocio();
    await pasoCuenta({ pass2: "ClaveSegura2" });
    expect(await screen.findByText("Las contraseñas no coinciden")).toBeInTheDocument();
    expect(screen.queryByText("Revisa tus datos")).not.toBeInTheDocument();
  });

  it("rechaza una contraseña floja", async () => {
    pintar(<OnboardingWizard />);
    await pasoNegocio();
    await pasoCuenta({ pass: "todominusculas1", pass2: "todominusculas1" });
    expect(
      await screen.findByText("Debe tener mayúscula, minúscula y número"),
    ).toBeInTheDocument();
  });

  it("exige aceptar los términos y no envía hasta entonces", async () => {
    pintar(<OnboardingWizard />);
    await pasoNegocio();
    await pasoCuenta();
    await screen.findByText("Revisa tus datos");
    await userEvent.click(screen.getByRole("button", { name: "Crear cuenta" }));
    expect(await screen.findByText("Debes aceptar los términos y condiciones")).toBeInTheDocument();
    expect(registerFn).not.toHaveBeenCalled();
  });

  it("envía el alta completa con el idioma de la interfaz", async () => {
    pintar(<OnboardingWizard />);
    await userEvent.selectOptions(screen.getByLabelText("Tipo de negocio"), "clinic");
    await pasoNegocio("  Clínica Sol ");
    await pasoCuenta();
    await screen.findByText("Revisa tus datos");
    expect(screen.getByText("ana@example.com")).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Acepto los términos y condiciones"));
    await userEvent.click(screen.getByRole("button", { name: "Crear cuenta" }));

    await waitFor(() =>
      expect(registerFn).toHaveBeenCalledWith({
        business_name: "Clínica Sol",
        business_type: "clinic",
        admin_full_name: "Ana Gómez",
        admin_email: "ana@example.com",
        admin_password: "ClaveSegura1",
        country: "CO",
        language: "es",
        terms_accepted: true,
      }),
    );
  });

  it("conserva lo escrito al volver atrás", async () => {
    pintar(<OnboardingWizard />);
    await pasoNegocio("Mi Negocio");
    await userEvent.click(screen.getByRole("button", { name: "Atrás" }));
    expect(screen.getByLabelText("Nombre del negocio")).toHaveValue("Mi Negocio");
  });

  it.each([
    [{ kind: "conflict" }, "Ya existe una cuenta con ese correo"],
    [{ kind: "rateLimited" }, "Demasiados intentos. Inténtalo de nuevo más tarde"],
    [{ kind: "other", message: "Fallo raro" }, "Fallo raro"],
  ] as [OnboardingFailure, string][])("muestra el error del servidor %o", async (f, texto) => {
    failure = f;
    pintar(<OnboardingWizard />);
    expect(screen.getByRole("alert")).toHaveTextContent(texto);
  });
});

describe("VerifyEmail", () => {
  it("sin token no llama a la API y muestra el fallo", () => {
    pintar(<VerifyEmail token={null} />);
    expect(screen.getByText("Enlace no válido")).toBeInTheDocument();
    expect(apiPost).not.toHaveBeenCalled();
  });

  it("confirma el token y lo comunica", async () => {
    apiPost.mockResolvedValue({ verified: true });
    pintar(<VerifyEmail token="abc.def.ghi" />);
    expect(await screen.findByText("Correo verificado")).toBeInTheDocument();
    expect(apiPost).toHaveBeenCalledWith("/onboarding/verify-email", { token: "abc.def.ghi" });
  });

  it("si el servidor lo rechaza muestra el enlace no válido", async () => {
    apiPost.mockRejectedValue(new Error("400"));
    pintar(<VerifyEmail token="caducado" />);
    expect(await screen.findByText("Enlace no válido")).toBeInTheDocument();
  });
});
