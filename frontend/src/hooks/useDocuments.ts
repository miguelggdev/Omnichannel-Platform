"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, apiDelete, apiGet, apiPost } from "@/lib/api";
import type { DocumentStatus, KbDocument, Paginated } from "@/types";

const EN_PROCESO: readonly DocumentStatus[] = ["pending", "processing"];

export function useDocuments(params: { status?: DocumentStatus; page?: number }) {
  return useQuery({
    queryKey: ["documents", params],
    queryFn: () => apiGet<Paginated<KbDocument>>("/documents", { page_size: 20, ...params }),
    placeholderData: keepPreviousData,
    // Mientras algun documento se procesa, se vuelve a preguntar cada 5 s.
    refetchInterval: (query) =>
      query.state.data?.items.some((d) => EN_PROCESO.includes(d.status)) ? 5_000 : false,
  });
}

export function useUploadDocument() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      // Sin Content-Type: el navegador pone el multipart con su boundary.
      const { data } = await api.post<KbDocument>("/documents", form, {
        headers: { "Content-Type": undefined },
      });
      return data;
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["documents"] }),
  });
}

export function useDeleteDocument() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiDelete(`/documents/${id}`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["documents"] }),
  });
}

export function useReprocessDocument() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiPost(`/documents/${id}/reprocess`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["documents"] }),
  });
}
