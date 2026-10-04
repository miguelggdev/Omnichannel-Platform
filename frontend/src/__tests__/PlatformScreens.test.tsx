import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import es from "../../messages/es.json";
import { CeleryDashboard } from "@/components/platform/CeleryDashboard";
import { ClientDetail } from "@/components/platform/ClientDetail";
import { useAuthStore } from "@/stores/authStore";
import type { ClientDetail as Detalle, TaskInfo, WorkersResponse } from "@/types";

const toast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));
vi.mock("sonner", () => ({ toast }));
// Recharts no es lo que se prueba aqui: next/dynamic devuelve un componente vacio.
vi.mock("next/dynamic", () => ({ default: () => () => <div data-testid="grafica" /> }));

const estado = vi.hoisted(() => ({
  workers: { data: undefined, error: null } as { data?: unknown; error: unknown },
  tasks: { data: [] as unknown },
  cliente: { data: undefined as unknown },
}));
const revocar = vi.fn();
const cambiarEstado = vi.fn();

vi.mock("@/hooks/usePlatform", () => ({
  useCeleryWorkers: () => ({ ...estado.workers, refetch: vi.fn() }),
  useCeleryQueues: () => ({ data: [{ name: "bulk", pending: 3 }], error: null, refetch: vi.fn() }),
  useCeleryTasks: () => ({ data: estado.tasks.data, error: null, refetch: vi.fn() }),
  useRedisInfo: () => ({
    data: { version: "7.4.0", uptime_seconds: 7200, connected_clients: 5, used_memory: 1048576, max_memory: 0, total_keys: 42, hit_ratio: 0.5 },
    error: null,
    refetch: vi.fn(),
  }),
  useRevokeTask: () => ({ mutate: revocar, isPending: false }),
  useClient: () => ({ data: estado.cliente.data, isLoading: false, error: null, refetch: vi.fn() }),
  useSetClientStatus: () => ({ mutate: cambiarEstado, isPending: false }),
}));

const MI_TENANT = "11111111-1111-1111-1111-111111111111";
const OTRO = "22222222-2222-2222-2222-222222222222";

function pintar(ui: React.ReactElement) {
  const p = Buffer.from(JSON.stringify({ user_id: "u", client_id: MI_TENANT, email: "a@b.c", role: "super_admin" }))
    .toString("base64")
    .replace(/=+$/, "");
  useAuthStore.getState().setTokens(`x.${p}.y`, "r");
  return render(<NextIntlClientProvider locale="es" messages={es}>{ui}</NextIntlClientProvider>);
}

const worker = { hostname: "celery@w1", pid: 7, concurrency: 4, active_tasks: 2, reserved_tasks: 1, processed_total: 1500, queues: ["bulk", "webhooks"], uptime_seconds: 7300 };
const tarea = (id: string, state: TaskInfo["state"]): TaskInfo => ({ id, name: `app.tasks.${id}`, worker: "celery@w1", state, queue: "bulk", started_at: null });

beforeEach(() => {
  revocar.mockReset();
  cambiarEstado.mockReset();
  Object.values(toast).forEach((f) => f.mockReset());
  estado.workers = { data: undefined, error: null };
  estado.tasks = { data: [] };
});

describe("CeleryDashboard", () => {
  it("un broker caido NO se presenta como cero workers", () => {
    estado.workers = { data: { broker_ok: false, workers: [] } satisfies WorkersResponse, error: null };
    pintar(<CeleryDashboard />);

    expect(screen.getByRole("alert")).toHaveTextContent(/No se pudo conectar con el broker/);
    expect(screen.getByRole("alert")).toHaveTextContent(/No significa que no haya ninguno/);
    expect(screen.queryByText(/ningún worker está conectado/)).not.toBeInTheDocument();
  });

  it("con el broker vivo y sin workers se avisa de que las colas se acumularan", () => {
    estado.workers = { data: { broker_ok: true, workers: [] } satisfies WorkersResponse, error: null };
    pintar(<CeleryDashboard />);

    expect(screen.getByRole("status")).toHaveTextContent(/ningún worker está conectado/);
    expect(screen.queryByText(/No se pudo conectar/)).not.toBeInTheDocument();
  });

  it("lista los workers con sus colas y cifras", () => {
    estado.workers = { data: { broker_ok: true, workers: [worker] } satisfies WorkersResponse, error: null };
    pintar(<CeleryDashboard />);

    const fila = screen.getByText("celery@w1").closest("tr") as HTMLElement;
    expect(within(fila).getByText("bulk")).toBeInTheDocument();
    expect(within(fila).getByText("webhooks")).toBeInTheDocument();
    expect(within(fila).getByText("2")).toBeInTheDocument();
  });

  it("Redis sin limite de memoria no pinta una barra de porcentaje", () => {
    estado.workers = { data: { broker_ok: true, workers: [worker] }, error: null };
    pintar(<CeleryDashboard />);

    expect(screen.getByText(/sin límite/)).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("solo se pueden revocar las tareas que no han empezado, y tras confirmar", async () => {
    estado.workers = { data: { broker_ok: true, workers: [worker] }, error: null };
    estado.tasks = { data: [tarea("en-curso", "active"), tarea("esperando", "reserved")] };
    pintar(<CeleryDashboard />);

    const enCurso = screen.getByText("app.tasks.en-curso").closest("tr") as HTMLElement;
    const esperando = screen.getByText("app.tasks.esperando").closest("tr") as HTMLElement;
    expect(within(enCurso).queryByRole("button", { name: "Revocar" })).not.toBeInTheDocument();

    await userEvent.click(within(esperando).getByRole("button", { name: "Revocar" }));
    expect(revocar).not.toHaveBeenCalled();
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Revocar" }));

    expect(revocar.mock.calls[0]?.[0]).toBe("esperando");
  });
});

const detalle = (over: Partial<Detalle> = {}): Detalle => ({
  id: OTRO, name: "Clinica Sol", slug: "clinica-sol", plan: "free", is_active: true,
  created_at: "2026-01-01T00:00:00Z", suspended_at: null, alert_message: null,
  users_total: 5, users_active: 4, contacts_total: 100, conversations_open: 3, conversations_30d: 40,
  messages_30d: 900, last_message_at: null, documents_ready: 2, has_agent: true, token_used: 250, token_budget: 1000,
  ...over,
});

describe("ClientDetail", () => {
  it("el boton de suspender esta desactivado en tu propio tenant", () => {
    estado.cliente = { data: detalle({ id: MI_TENANT }) };
    pintar(<ClientDetail id={MI_TENANT} />);

    expect(screen.getByRole("button", { name: "Suspender" })).toBeDisabled();
  });

  it("suspender a otro pide confirmacion y manda el motivo", async () => {
    estado.cliente = { data: detalle() };
    pintar(<ClientDetail id={OTRO} />);

    await userEvent.click(screen.getByRole("button", { name: "Suspender" }));
    await userEvent.type(screen.getByLabelText("Motivo (opcional)"), "Pago pendiente");
    expect(cambiarEstado).not.toHaveBeenCalled();
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Suspender" }));

    await waitFor(() => expect(cambiarEstado).toHaveBeenCalledTimes(1));
    expect(cambiarEstado.mock.calls[0]?.[0]).toEqual({ is_active: false, reason: "Pago pendiente" });
  });

  it("reactivar no manda motivo", async () => {
    estado.cliente = { data: detalle({ is_active: false, suspended_at: "2026-02-01T00:00:00Z", alert_message: "Pago pendiente" }) };
    pintar(<ClientDetail id={OTRO} />);

    expect(screen.getByText("Pago pendiente")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reactivar" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Reactivar" }));

    expect(cambiarEstado.mock.calls[0]?.[0]).toEqual({ is_active: true, reason: undefined });
  });

  it("muestra el uso de tokens y solo conteos, sin contenido", () => {
    estado.cliente = { data: detalle() };
    pintar(<ClientDetail id={OTRO} />);

    expect(screen.getByText(/250 \/ 1000 \(25 %\)/)).toBeInTheDocument();
    expect(screen.getByText(/no muestra el contenido de las conversaciones/)).toBeInTheDocument();
  });
});
