"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiDelete, apiGet, apiPost, apiPut } from "@/lib/api";
import type { QuickReply, QuickReplyInput } from "@/types";

export function useQuickReplies() {
  return useQuery({
    queryKey: ["quick-replies"],
    queryFn: () => apiGet<QuickReply[]>("/quick-replies"),
  });
}

function useInvalidate() {
  const queryClient = useQueryClient();
  return () => void queryClient.invalidateQueries({ queryKey: ["quick-replies"] });
}

export function useSaveQuickReply() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ id, data }: { id?: string; data: QuickReplyInput }) =>
      id ? apiPut<QuickReply>(`/quick-replies/${id}`, data) : apiPost<QuickReply>("/quick-replies", data),
    onSuccess: invalidate,
  });
}

export function useDeleteQuickReply() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (id: string) => apiDelete(`/quick-replies/${id}`),
    onSuccess: invalidate,
  });
}
