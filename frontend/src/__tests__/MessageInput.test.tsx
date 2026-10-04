import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import es from "../../messages/es.json";
import { MessageInput } from "@/components/conversations/MessageInput";

const render_ = vi.fn();
const toast = vi.hoisted(() => ({ error: vi.fn(), warning: vi.fn(), success: vi.fn() }));
vi.mock("sonner", () => ({ toast }));
vi.mock("@/hooks/useQuickReplies", () => ({
  useQuickReplies: () => ({
    data: [
      { id: "q1", shortcut: "/saludo", title: "Saludo", content: "Hola {{contact_name}}", category: null },
      { id: "q2", shortcut: "/horario", title: "Horario", content: "Abrimos de 8 a 18", category: null },
    ],
  }),
  renderQuickReply: (...args: unknown[]) => render_(...args),
}));

function pintar(props: Partial<React.ComponentProps<typeof MessageInput>> = {}) {
  const onSend = props.onSend ?? vi.fn().mockResolvedValue(undefined);
  render(
    <NextIntlClientProvider locale="es" messages={es}>
      <MessageInput conversationId="c1" blockedReason={null} sending={false} onSend={onSend} {...props} />
    </NextIntlClientProvider>,
  );
  return { onSend: onSend as ReturnType<typeof vi.fn> };
}

const caja = () => screen.getByPlaceholderText("Escribe una respuesta…");

beforeEach(() => {
  render_.mockReset();
  Object.values(toast).forEach((f) => f.mockReset());
});

describe("MessageInput", () => {
  it("Enter envia el texto sin espacios sobrantes y vacia la caja", async () => {
    const { onSend } = pintar();

    await userEvent.type(caja(), "  Hola Ada  {Enter}");

    await waitFor(() => expect(onSend).toHaveBeenCalledWith("Hola Ada"));
    await waitFor(() => expect(caja()).toHaveValue(""));
  });

  it("Mayus+Enter hace salto de linea y no envia", async () => {
    const { onSend } = pintar();

    await userEvent.type(caja(), "linea 1{Shift>}{Enter}{/Shift}linea 2");

    expect(onSend).not.toHaveBeenCalled();
    expect(caja()).toHaveValue("linea 1\nlinea 2");
  });

  it("no envia un texto en blanco", async () => {
    const { onSend } = pintar();

    await userEvent.type(caja(), "   {Enter}");

    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Enviar mensaje" })).toBeDisabled();
  });

  it("si el envio falla conserva el texto y avisa", async () => {
    const onSend = vi.fn().mockRejectedValue(new Error("No se pudo enviar el mensaje"));
    pintar({ onSend });

    await userEvent.type(caja(), "Hola{Enter}");

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("No se pudo enviar el mensaje"));
    expect(caja()).toHaveValue("Hola");
  });

  it("escribir / ofrece los atajos que empiezan igual y Enter resuelve las variables", async () => {
    render_.mockResolvedValue({ shortcut: "/saludo", content: "Hola Ada", unresolved: [] });
    const { onSend } = pintar();

    await userEvent.type(caja(), "/sa");
    expect(screen.getByRole("option", { name: /saludo/i })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /horario/i })).not.toBeInTheDocument();
    await userEvent.keyboard("{Enter}");

    await waitFor(() => expect(caja()).toHaveValue("Hola Ada"));
    expect(render_).toHaveBeenCalledWith("q1", "c1");
    // Elegir un atajo no envia el mensaje: la persona lo revisa antes.
    expect(onSend).not.toHaveBeenCalled();
  });

  it("avisa si quedan variables sin resolver y deja el texto para corregirlo", async () => {
    render_.mockResolvedValue({
      shortcut: "/saludo",
      content: "Hola {{contact_name}}",
      unresolved: ["contact_name"],
    });
    pintar();

    await userEvent.type(caja(), "/saludo{Enter}");

    await waitFor(() => expect(caja()).toHaveValue("Hola {{contact_name}}"));
    expect(toast.warning).toHaveBeenCalledWith(expect.stringContaining("contact_name"));
  });

  it.each([
    ["closed", "Esta conversación está cerrada"],
    ["assignedToOther", "la atiende otra persona"],
  ] as const)("con la conversacion bloqueada (%s) no hay caja y se explica por que", (motivo, texto) => {
    pintar({ blockedReason: motivo });

    expect(screen.queryByPlaceholderText("Escribe una respuesta…")).not.toBeInTheDocument();
    expect(screen.getByText(new RegExp(texto))).toBeInTheDocument();
  });
});
