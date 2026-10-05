"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost } from "@/lib/api";

export interface VerificationStatus {
  email: string;
  /** `null`: cuenta que no nacio del registro publico, no hay nada que verificar. */
  verified: boolean | null;
}

export const useVerification = () =>
  useQuery({
    queryKey: ["verification"],
    queryFn: () => apiGet<VerificationStatus>("/onboarding/verification"),
    // Es informativo: si falla, el aviso simplemente no se muestra.
    retry: false,
    staleTime: 5 * 60_000,
  });

export function useResendVerification() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost<{ sent: boolean }>("/onboarding/resend-verification"),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["verification"] }),
  });
}
