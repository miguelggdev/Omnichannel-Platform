"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiPut } from "@/lib/api";
import type {
  ClientDetail,
  ClientSummary,
  Paginated,
  QueueDepth,
  RedisInfo,
  SecurityOverview,
  SystemStatus,
  TaskInfo,
  WorkersResponse,
} from "@/types";

const CADA_30S = 30_000;

export function useClients(params: { search?: string; status?: "all" | "active" | "inactive"; page?: number }) {
  return useQuery({
    queryKey: ["platform", "clients", params],
    queryFn: () => apiGet<Paginated<ClientSummary>>("/platform/clients", { page_size: 20, ...params }),
    placeholderData: keepPreviousData,
  });
}

export function useClient(id: string) {
  return useQuery({
    queryKey: ["platform", "client", id],
    queryFn: () => apiGet<ClientDetail>(`/platform/clients/${id}`),
  });
}

export function useSetClientStatus(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { is_active: boolean; reason?: string }) =>
      apiPut<ClientDetail>(`/platform/clients/${id}/status`, data),
    onSuccess: (detalle) => {
      queryClient.setQueryData(["platform", "client", id], detalle);
      void queryClient.invalidateQueries({ queryKey: ["platform", "clients"] });
    },
  });
}

export const useCeleryWorkers = () =>
  useQuery({
    queryKey: ["platform", "celery", "workers"],
    queryFn: () => apiGet<WorkersResponse>("/platform/celery/workers"),
    refetchInterval: CADA_30S,
  });

export const useCeleryQueues = () =>
  useQuery({
    queryKey: ["platform", "celery", "queues"],
    queryFn: () => apiGet<QueueDepth[]>("/platform/celery/queues"),
    refetchInterval: CADA_30S,
  });

export const useCeleryTasks = () =>
  useQuery({
    queryKey: ["platform", "celery", "tasks"],
    queryFn: () => apiGet<TaskInfo[]>("/platform/celery/tasks"),
    refetchInterval: CADA_30S,
  });

export const useRedisInfo = () =>
  useQuery({
    queryKey: ["platform", "redis"],
    queryFn: () => apiGet<RedisInfo>("/platform/redis"),
    refetchInterval: CADA_30S,
  });

export const useSecurityOverview = () =>
  useQuery({
    queryKey: ["platform", "security"],
    queryFn: () => apiGet<SecurityOverview>("/platform/security"),
  });

export const useSystemStatus = () =>
  useQuery({
    queryKey: ["platform", "system"],
    queryFn: () => apiGet<SystemStatus>("/platform/system"),
    refetchInterval: CADA_30S,
  });

export function useRevokeTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (taskId: string) => apiPost(`/platform/celery/tasks/${taskId}/revoke`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["platform", "celery"] }),
  });
}
