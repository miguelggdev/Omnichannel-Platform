import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";
import es from "../../messages/es.json";
import { SecurityOverview } from "@/components/platform/SecurityOverview";
import type { SecurityOverview as Datos } from "@/types";

let datos: Datos | undefined;
vi.mock("@/hooks/usePlatform", () => ({
  useSecurityOverview: () => ({ data: datos, error: null, refetch: vi.fn(), isFetching: false }),
}));

const pintar = () =>
  render(
    <NextIntlClientProvider locale="es" messages={es}>
      <SecurityOverview />
    </NextIntlClientProvider>,
  );

describe("SecurityOverview", () => {
  it("avisa de las tablas sin RLS y explica el fallo del rol que la salta", () => {
    datos = {
      environment: "production",
      checks: [
        { id: "rls_tables", status: "fail", detail: "1/2" },
        { id: "db_role_rls", status: "fail", detail: "BYPASSRLS" },
        { id: "cors", status: "ok", detail: "1 origen(es)" },
      ],
      rls_tables: [
        { name: "contacts", rls_enabled: true, rls_forced: true },
        { name: "notas", rls_enabled: true, rls_forced: false },
      ],
      rls_unprotected: ["notas"],
    };
    pintar();

    expect(screen.getByRole("alert")).toHaveTextContent("Sin protección completa: notas");
    expect(screen.getByText("Sin FORCE")).toBeInTheDocument();
    expect(screen.getByText("Forzada")).toBeInTheDocument();
    expect(screen.getByText(/salta RLS/)).toBeInTheDocument();
    expect(screen.getByText("BYPASSRLS")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("2 fallo(s) y 0 aviso(s) · entorno production");
  });

  it("una comprobación correcta no muestra consejo", () => {
    datos = {
      environment: "production",
      checks: [{ id: "cors", status: "ok", detail: null }],
      rls_tables: [],
      rls_unprotected: [],
    };
    pintar();
    expect(screen.getByText("Orígenes permitidos (CORS)")).toBeInTheDocument();
    expect(screen.queryByText(/comodín/)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
