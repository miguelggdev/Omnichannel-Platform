"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { apiPost, errorMessage } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";
import type { LoginRequest, TokenResponse } from "@/types";
import { usePreferences } from "./usePreferences";

export function useAuth() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { loadFromBackend } = usePreferences();
  const { user, accessToken, setTokens, logout: clearAuth } = useAuthStore();
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function login(credenciales: LoginRequest): Promise<boolean> {
    setPending(true);
    setError(null);
    try {
      const tokens = await apiPost<TokenResponse>("/auth/login", credenciales);
      setTokens(tokens.access_token);
      await loadFromBackend();
      router.replace("/");
      router.refresh();
      return true;
    } catch (e) {
      setError(errorMessage(e));
      return false;
    } finally {
      setPending(false);
    }
  }

  async function logout(): Promise<void> {
    // El backend anula el refresh de este navegador y borra su cookie. Aunque falle (sin red),
    // la sesion local se olvida igual y la cache se vacia para no ver datos del anterior.
    await apiPost("/auth/logout", {}).catch(() => undefined);
    clearAuth();
    queryClient.clear();
    router.replace("/login");
  }

  return { user, isAuthenticated: accessToken !== null, login, logout, error, pending };
}
