import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import es from "../../messages/es.json";
import { TeamManager } from "@/components/settings/TeamManager";
import { useAuthStore } from "@/stores/authStore";
import type { TeamUser, UserRole } from "@/types";

const mutateUpdate = vi.fn();
const mutateCreate = vi.fn();
const toast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));
vi.mock("sonner", () => ({ toast }));

const YO = "11111111-1111-1111-1111-111111111111";
const usuario = (id: string, over: Partial<TeamUser> = {}): TeamUser => ({
  id,
  client_id: "c",
  email: `${id.slice(0, 4)}@example.com`,
  first_name: "Nombre" + id.slice(0, 2),
  last_name: "Apellido",
  role: "agent",
  is_active: true,
  last_login_at: null,
  ...over,
});
const FILAS: TeamUser[] = [
  usuario(YO, { first_name: "Ana", role: "admin" }),
  usuario("22222222-2222-2222-2222-222222222222", { first_name: "Aldo", role: "agent" }),
  usuario("33333333-3333-3333-3333-333333333333", { first_name: "Sergio", role: "super_admin" }),
  usuario("44444444-4444-4444-4444-444444444444", { first_name: "Inés", is_active: false }),
];

vi.mock("@/hooks/useUsers", () => ({
  useUsers: () => ({ data: { items: FILAS, total: FILAS.length, page: 1, page_size: 20 }, isLoading: false, error: null }),
  useUpdateUser: () => ({ mutate: mutateUpdate, isPending: false }),
  useCreateUser: () => ({ mutate: mutateCreate, isPending: false }),
}));

function tokenDe(role: UserRole): string {
  const p = Buffer.from(JSON.stringify({ user_id: YO, client_id: "c", email: "a@b.c", role }))
    .toString("base64")
    .replace(/=+$/, "");
  return `x.${p}.y`;
}

function pintar(rol: UserRole = "admin") {
  useAuthStore.getState().setTokens(tokenDe(rol), "r");
  render(
    <NextIntlClientProvider locale="es" messages={es}>
      <TeamManager />
    </NextIntlClientProvider>,
  );
}

const fila = (nombre: string) => screen.getByText(new RegExp(`^${nombre}`)).closest("tr") as HTMLElement;

beforeEach(() => {
  mutateUpdate.mockReset();
  mutateCreate.mockReset();
  Object.values(toast).forEach((f) => f.mockReset());
  useAuthStore.getState().logout();
});

describe("TeamManager: lo que se puede tocar", () => {
  it("uno mismo se puede editar pero no desactivar", () => {
    pintar();

    const mia = within(fila("Ana"));
    expect(mia.getByRole("button", { name: /Editar a Ana/ })).toBeInTheDocument();
    expect(mia.queryByRole("button", { name: /Desactivar a Ana/ })).not.toBeInTheDocument();
  });

  it("un admin no ve acciones sobre un super_admin", () => {
    pintar("admin");

    expect(within(fila("Sergio")).queryAllByRole("button")).toHaveLength(0);
  });

  it("un super_admin si puede actuar sobre otro super_admin", () => {
    pintar("super_admin");

    expect(within(fila("Sergio")).getByRole("button", { name: /Editar a Sergio/ })).toBeInTheDocument();
  });

  it("a un desactivado se le ofrece reactivar, no desactivar", () => {
    pintar();

    const ines = within(fila("Inés"));
    expect(ines.getByRole("button", { name: /Reactivar a Inés/ })).toBeInTheDocument();
    expect(ines.queryByRole("button", { name: /Desactivar a Inés/ })).not.toBeInTheDocument();
  });
});

describe("TeamManager: acciones", () => {
  it("desactivar pide confirmacion y manda solo is_active", async () => {
    pintar();

    await userEvent.click(within(fila("Aldo")).getByRole("button", { name: /Desactivar a Aldo/ }));
    expect(mutateUpdate).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Desactivar" }));

    expect(mutateUpdate).toHaveBeenCalledTimes(1);
    expect(mutateUpdate.mock.calls[0]?.[0]).toEqual({
      id: "22222222-2222-2222-2222-222222222222",
      data: { is_active: false },
    });
  });

  it("editar solo envia los campos que cambiaron", async () => {
    pintar();

    await userEvent.click(within(fila("Aldo")).getByRole("button", { name: /Editar a Aldo/ }));
    const apellido = screen.getByLabelText("Apellido");
    await userEvent.clear(apellido);
    await userEvent.type(apellido, "Nuevo");
    await userEvent.click(screen.getByRole("button", { name: "Guardar" }));

    await waitFor(() => expect(mutateUpdate).toHaveBeenCalledTimes(1));
    expect(mutateUpdate.mock.calls[0]?.[0]).toEqual({
      id: "22222222-2222-2222-2222-222222222222",
      data: { last_name: "Nuevo" },
    });
  });

  it("editarse a uno mismo deja el rol bloqueado", async () => {
    pintar();

    await userEvent.click(within(fila("Ana")).getByRole("button", { name: /Editar a Ana/ }));

    expect(screen.getByRole("combobox", { name: "Rol" })).toBeDisabled();
    expect(screen.getByText(/No puedes cambiar tu propio rol/)).toBeInTheDocument();
  });

  it("guardar sin cambios no llama al backend", async () => {
    pintar();

    await userEvent.click(within(fila("Aldo")).getByRole("button", { name: /Editar a Aldo/ }));
    await userEvent.click(screen.getByRole("button", { name: "Guardar" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mutateUpdate).not.toHaveBeenCalled();
  });

  it("el alta valida correo y contrasena antes de enviar", async () => {
    pintar();

    await userEvent.click(screen.getByRole("button", { name: "Nuevo usuario" }));
    await userEvent.type(screen.getByLabelText("Correo electrónico"), "mal");
    await userEvent.type(screen.getByLabelText("Contraseña inicial"), "corta");
    await userEvent.click(screen.getByRole("button", { name: "Guardar" }));

    expect(await screen.findByText("Introduce un correo válido")).toBeInTheDocument();
    expect(screen.getByText("Mínimo 8 caracteres")).toBeInTheDocument();
    expect(mutateCreate).not.toHaveBeenCalled();
  });

  it("el alta valida envia los datos con el rol elegido (agent por defecto)", async () => {
    pintar();

    await userEvent.click(screen.getByRole("button", { name: "Nuevo usuario" }));
    await userEvent.type(screen.getByLabelText("Nombre"), "Luis");
    await userEvent.type(screen.getByLabelText("Apellido"), "Prieto");
    await userEvent.type(screen.getByLabelText("Correo electrónico"), "luis@example.com");
    await userEvent.type(screen.getByLabelText("Contraseña inicial"), "una-clave-larga");
    await userEvent.click(screen.getByRole("button", { name: "Guardar" }));

    await waitFor(() => expect(mutateCreate).toHaveBeenCalledTimes(1));
    expect(mutateCreate.mock.calls[0]?.[0]).toEqual({
      first_name: "Luis",
      last_name: "Prieto",
      email: "luis@example.com",
      password: "una-clave-larga",
      role: "agent",
    });
  });
});
