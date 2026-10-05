import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import es from "../../messages/es.json";
import { VerifyEmailBanner } from "@/components/layout/VerifyEmailBanner";

let estado: { email: string; verified: boolean | null } | undefined;
const mutate = vi.fn();
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));

vi.mock("sonner", () => ({ toast }));
vi.mock("@/hooks/useVerification", () => ({
  useVerification: () => ({ data: estado }),
  useResendVerification: () => ({ mutate, isPending: false }),
}));

const pintar = () =>
  render(
    <NextIntlClientProvider locale="es" messages={es}>
      <VerifyEmailBanner />
    </NextIntlClientProvider>,
  );

beforeEach(() => {
  mutate.mockReset();
  toast.success.mockReset();
  toast.error.mockReset();
});

describe("VerifyEmailBanner", () => {
  it.each([[true], [null]])("no aparece si verified es %s", (verified) => {
    estado = { email: "ana@example.com", verified };
    pintar();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("no aparece mientras se carga", () => {
    estado = undefined;
    pintar();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("aparece si no está verificado, reenvía y avisa", async () => {
    estado = { email: "ana@example.com", verified: false };
    mutate.mockImplementation((_v, opts) => opts.onSuccess());
    pintar();
    expect(screen.getByRole("status")).toHaveTextContent("ana@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Reenviar enlace" }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Enlace enviado a ana@example.com"));
  });

  it("se puede cerrar", async () => {
    estado = { email: "ana@example.com", verified: false };
    pintar();
    await userEvent.click(screen.getByRole("button", { name: "Cerrar aviso" }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
