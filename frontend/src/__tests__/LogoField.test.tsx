import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import es from "../../messages/es.json";
import { LogoField } from "@/components/settings/LogoField";

const subir = vi.fn();
const quitar = vi.fn();
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));

vi.mock("sonner", () => ({ toast }));
vi.mock("@/hooks/useBusinessProfile", () => ({
  useUploadLogo: () => ({ mutate: subir, isPending: false }),
  useDeleteLogo: () => ({ mutate: quitar, isPending: false }),
  useLogoBlob: (subido: boolean) => ({ data: subido ? "blob:logo-subido" : undefined }),
}));

const pintar = (props: { uploaded: boolean; logoUrl: string | null }) =>
  render(
    <NextIntlClientProvider locale="es" messages={es}>
      <LogoField {...props} />
    </NextIntlClientProvider>,
  );

const archivo = (nombre: string, tipo: string, bytes = 10) =>
  new File([new Uint8Array(bytes)], nombre, { type: tipo });

beforeEach(() => {
  subir.mockReset();
  quitar.mockReset();
  toast.success.mockReset();
  toast.error.mockReset();
});

describe("LogoField", () => {
  it("sin logo ofrece subir y no ofrece quitar", () => {
    pintar({ uploaded: false, logoUrl: null });
    expect(screen.getByRole("button", { name: "Subir logo" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Quitar" })).not.toBeInTheDocument();
  });

  it("con logo subido muestra la imagen descargada y ofrece cambiar y quitar", () => {
    pintar({ uploaded: true, logoUrl: null });
    expect(screen.getByAltText("Logotipo del negocio")).toHaveAttribute("src", "blob:logo-subido");
    expect(screen.getByRole("button", { name: "Cambiar logo" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Quitar" })).toBeInTheDocument();
  });

  it("con solo una URL externa la usa de vista previa", () => {
    pintar({ uploaded: false, logoUrl: "https://cdn.example.com/l.png" });
    expect(screen.getByAltText("Logotipo del negocio")).toHaveAttribute("src", "https://cdn.example.com/l.png");
  });

  it("envía un PNG válido", async () => {
    pintar({ uploaded: false, logoUrl: null });
    await userEvent.upload(screen.getByLabelText("Logotipo"), archivo("l.png", "image/png"));
    await waitFor(() => expect(subir).toHaveBeenCalledTimes(1));
    expect(subir.mock.calls[0]![0]).toBeInstanceOf(File);
  });

  it("no envía un SVG ni uno de más de 512 KB y avisa", async () => {
    pintar({ uploaded: false, logoUrl: null });
    const input = screen.getByLabelText("Logotipo");
    // `accept` filtra en userEvent: se desactiva para probar la comprobación propia.
    const u = userEvent.setup({ applyAccept: false });
    await u.upload(input, archivo("l.svg", "image/svg+xml"));
    expect(toast.error).toHaveBeenLastCalledWith("El logo debe ser PNG, JPEG o WebP.");
    await u.upload(input, archivo("grande.png", "image/png", 512 * 1024 + 1));
    expect(toast.error).toHaveBeenLastCalledWith("El logo no puede pesar más de 512 KB.");
    expect(subir).not.toHaveBeenCalled();
  });

  it("quita el logo subido", async () => {
    pintar({ uploaded: true, logoUrl: null });
    await userEvent.click(screen.getByRole("button", { name: "Quitar" }));
    expect(quitar).toHaveBeenCalledTimes(1);
  });
});
