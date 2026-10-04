"use client";

import axios from "axios";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { apiPost, errorMessage } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";
import type { OnboardingRequest, OnboardingResponse } from "@/types";
import { usePreferences } from "./usePreferences";

export type OnboardingFailure =
  | { kind: "conflict" }
  | { kind: "rateLimited" }
  | { kind: "unavailable" }
  | { kind: "other"; message: string };

/** Alta de un negocio: crea el tenant y deja la sesion del administrador iniciada. */
export function useOnboarding() {
  const router = useRouter();
  const { loadFromBackend } = usePreferences();
  const setTokens = useAuthStore((s) => s.setTokens);
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<OnboardingFailure | null>(null);

  async function register(datos: OnboardingRequest): Promise<boolean> {
    setPending(true);
    setFailure(null);
    try {
      const res = await apiPost<OnboardingResponse>("/onboarding/register", datos);
      setTokens(res.access_token, res.refresh_token);
      await loadFromBackend();
      router.replace("/");
      router.refresh();
      return true;
    } catch (e) {
      const status = axios.isAxiosError(e) ? e.response?.status : undefined;
      if (status === 409) setFailure({ kind: "conflict" });
      else if (status === 429) setFailure({ kind: "rateLimited" });
      else if (status === 404) setFailure({ kind: "unavailable" });
      else setFailure({ kind: "other", message: errorMessage(e) });
      return false;
    } finally {
      setPending(false);
    }
  }

  return { register, pending, failure };
}
