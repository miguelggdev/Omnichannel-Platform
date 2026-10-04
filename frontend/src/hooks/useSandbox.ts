"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiPut } from "@/lib/api";
import type { FeatureFlagItem, SandboxStatus } from "@/types";

/** Flags del tenant; un super_admin puede pedir las de otro con `clientId`. */
export function useFeatureFlags(clientId?: string) {
  return useQuery({
    queryKey: ["feature-flags", clientId ?? "own"],
    queryFn: () =>
      apiGet<{ flags: FeatureFlagItem[] }>("/admin/feature-flags", clientId ? { client_id: clientId } : undefined),
  });
}

export function useSetFlag(clientId?: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ flag, value }: { flag: string; value: boolean | number }) =>
      apiPut<{ flags: FeatureFlagItem[] }>(
        `/admin/feature-flags/${flag}${clientId ? `?client_id=${clientId}` : ""}`,
        { value },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["feature-flags"] });
      void queryClient.invalidateQueries({ queryKey: ["sandbox"] });
    },
  });
}

export function useSandboxStatus() {
  return useQuery({
    queryKey: ["sandbox"],
    queryFn: () => apiGet<SandboxStatus>("/sandbox"),
    retry: false,
  });
}

export function useSandboxActions() {
  const queryClient = useQueryClient();
  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["sandbox"] });
  return {
    create: useMutation({ mutationFn: () => apiPost<SandboxStatus>("/sandbox"), onSuccess: invalidate }),
    reset: useMutation({ mutationFn: () => apiPost<SandboxStatus>("/sandbox/reset"), onSuccess: invalidate }),
    publish: useMutation({ mutationFn: () => apiPost<{ version: number }>("/sandbox/publish"), onSuccess: invalidate }),
    rollback: useMutation({ mutationFn: () => apiPost("/sandbox/rollback", {}), onSuccess: invalidate }),
  };
}
