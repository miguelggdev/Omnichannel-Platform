"use client";

import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/lib/api";
import type { ChannelCount, DailyMessages, DashboardMetrics } from "@/types";

/** Las cifras se refrescan cada minuto; las series cambian menos, cada cinco. */
export function useAnalytics(enabled: boolean) {
  const metrics = useQuery({
    queryKey: ["analytics", "dashboard"],
    queryFn: () => apiGet<DashboardMetrics>("/analytics/dashboard"),
    refetchInterval: 60_000,
    enabled,
  });
  const byChannel = useQuery({
    queryKey: ["analytics", "by-channel"],
    queryFn: () => apiGet<ChannelCount[]>("/analytics/conversations-by-channel", { days: 30 }),
    staleTime: 5 * 60_000,
    enabled,
  });
  const overTime = useQuery({
    queryKey: ["analytics", "over-time"],
    queryFn: () => apiGet<DailyMessages[]>("/analytics/messages-over-time", { days: 7 }),
    staleTime: 5 * 60_000,
    enabled,
  });
  return { metrics, byChannel, overTime };
}
