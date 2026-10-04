import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from "axios";
import { AxiosError } from "axios";
import axios from "axios";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, errorMessage } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";

function tokenDe(): string {
  const p = Buffer.from(JSON.stringify({ user_id: "u", client_id: "c", email: "a@b.c", role: "admin" }))
    .toString("base64")
    .replace(/=+$/, "");
  return `x.${p}.y`;
}

function respuesta(config: InternalAxiosRequestConfig, status: number, data: unknown = {}): AxiosResponse {
  return { data, status, statusText: "", headers: {}, config };
}

/** Adaptador falso: un 401 si el token no es el nuevo, un 200 si lo es. */
function backendFalso(tokenValido: string, vistas: string[]): AxiosAdapter {
  return async (config) => {
    vistas.push(String(config.headers.Authorization));
    if (config.headers.Authorization === `Bearer ${tokenValido}`) return respuesta(config, 200, { ok: true });
    throw new AxiosError("no autorizado", "ERR_BAD_REQUEST", config, null, respuesta(config, 401));
  };
}

beforeEach(() => {
  useAuthStore.getState().setTokens("viejo", "refresh-viejo");
  vi.restoreAllMocks();
});

describe("cliente de la API", () => {
  it("manda el access token en Authorization", async () => {
    const vistas: string[] = [];
    useAuthStore.getState().setTokens("viejo", "r");
    api.defaults.adapter = backendFalso("viejo", vistas);

    await api.get("/contacts");

    expect(vistas).toEqual(["Bearer viejo"]);
  });

  it("ante un 401 renueva el token y repite la peticion con el nuevo", async () => {
    const nuevo = tokenDe();
    const vistas: string[] = [];
    api.defaults.adapter = backendFalso(nuevo, vistas);
    const post = vi
      .spyOn(axios, "post")
      .mockResolvedValue({ data: { access_token: nuevo, refresh_token: "refresh-nuevo", token_type: "bearer" } });

    const r = await api.get("/contacts");

    expect(r.status).toBe(200);
    expect(post).toHaveBeenCalledWith("/api/v1/auth/refresh", { refresh_token: "refresh-viejo" });
    expect(vistas).toEqual(["Bearer viejo", `Bearer ${nuevo}`]);
    expect(useAuthStore.getState().refreshToken).toBe("refresh-nuevo");
  });

  it("varias peticiones que fallan a la vez comparten una sola renovacion", async () => {
    const nuevo = tokenDe();
    api.defaults.adapter = backendFalso(nuevo, []);
    const post = vi
      .spyOn(axios, "post")
      .mockResolvedValue({ data: { access_token: nuevo, refresh_token: "r2", token_type: "bearer" } });

    await Promise.all([api.get("/a"), api.get("/b"), api.get("/c")]);

    expect(post).toHaveBeenCalledTimes(1);
  });

  it("si la renovacion falla, cierra la sesion", async () => {
    api.defaults.adapter = backendFalso("nunca", []);
    vi.spyOn(axios, "post").mockRejectedValue(new Error("refresh caducado"));
    // jsdom no implementa la navegacion: se sustituye `location` para ver a donde redirige.
    const assign = vi.fn();
    vi.stubGlobal("location", { pathname: "/", assign });

    await expect(api.get("/contacts")).rejects.toBeDefined();

    expect(useAuthStore.getState().accessToken).toBeNull();
    expect(assign).toHaveBeenCalledWith("/login");
    vi.unstubAllGlobals();
  });

  it("un 401 del propio login no intenta renovar nada", async () => {
    api.defaults.adapter = backendFalso("nunca", []);
    const post = vi.spyOn(axios, "post");

    await expect(api.post("/auth/login", {})).rejects.toBeDefined();

    expect(post).not.toHaveBeenCalled();
  });
});

describe("errorMessage", () => {
  const conRespuesta = (data: unknown) =>
    new AxiosError("Request failed with status code 422", "ERR_BAD_REQUEST", undefined, null, {
      data,
      status: 422,
      statusText: "",
      headers: {},
      config: {} as InternalAxiosRequestConfig,
    });

  it("usa el message de AppException", () => {
    expect(errorMessage(conRespuesta({ error_code: "X", message: "Credenciales inválidas" }))).toBe(
      "Credenciales inválidas",
    );
  });

  it("resume los errores de validacion de FastAPI como campo: motivo", () => {
    const e = conRespuesta({
      detail: [
        { loc: ["body", "shortcut"], msg: "String should match pattern" },
        { loc: ["body", "title"], msg: "Field required" },
      ],
    });
    expect(errorMessage(e)).toBe("shortcut: String should match pattern; title: Field required");
  });

  it("acepta un detail de texto", () => {
    expect(errorMessage(conRespuesta({ detail: "Not found" }))).toBe("Not found");
  });

  it("cae al mensaje de axios y luego al fallback", () => {
    expect(errorMessage(conRespuesta({}))).toBe("Request failed with status code 422");
    expect(errorMessage("algo raro", "Fallo")).toBe("Fallo");
  });
});
