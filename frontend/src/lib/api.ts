import axios, { AxiosError, type InternalAxiosRequestConfig } from "axios";
import { useAuthStore } from "@/stores/authStore";
import type { ApiError, TokenResponse } from "@/types";

/**
 * El navegador llama a `/api/v1/...` en su propio origen y Next lo reenvia al backend
 * (ver `rewrites` en next.config.mjs), asi que no hay CORS ni URL del backend en el cliente.
 */
export const api = axios.create({
  baseURL: "/api/v1",
  headers: { "Content-Type": "application/json" },
});

api.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = useAuthStore.getState().accessToken;
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

let refreshing: Promise<string> | null = null;

/** Renueva el access token. Las peticiones que fallan a la vez comparten una sola renovacion. */
function refreshAccessToken(): Promise<string> {
  if (!refreshing) {
    const refreshToken = useAuthStore.getState().refreshToken;
    if (!refreshToken) return Promise.reject(new Error("sin refresh token"));
    refreshing = axios
      .post<TokenResponse>("/api/v1/auth/refresh", { refresh_token: refreshToken })
      .then(({ data }) => {
        useAuthStore.getState().setTokens(data.access_token, data.refresh_token);
        return data.access_token;
      })
      .finally(() => {
        refreshing = null;
      });
  }
  return refreshing;
}

function expulsar(): void {
  useAuthStore.getState().logout();
  if (typeof window !== "undefined" && window.location.pathname !== "/login") {
    window.location.assign("/login");
  }
}

api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const original = error.config as (InternalAxiosRequestConfig & { _retry?: boolean }) | undefined;
    const esLogin = original?.url?.includes("/auth/login") ?? false;

    if (error.response?.status === 401 && original && !original._retry && !esLogin) {
      original._retry = true;
      try {
        const token = await refreshAccessToken();
        original.headers.Authorization = `Bearer ${token}`;
        return api(original);
      } catch {
        expulsar();
      }
    }
    return Promise.reject(error);
  },
);

/** Mensaje legible de un error de la API (o del propio axios). */
export function errorMessage(error: unknown, fallback = "Error inesperado"): string {
  if (axios.isAxiosError<ApiError>(error)) {
    return error.response?.data?.message ?? error.message ?? fallback;
  }
  return error instanceof Error ? error.message : fallback;
}

export async function apiGet<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  return (await api.get<T>(url, { params })).data;
}
export async function apiPost<T>(url: string, data?: unknown): Promise<T> {
  return (await api.post<T>(url, data)).data;
}
export async function apiPut<T>(url: string, data?: unknown): Promise<T> {
  return (await api.put<T>(url, data)).data;
}
export async function apiDelete<T>(url: string): Promise<T> {
  return (await api.delete<T>(url)).data;
}
