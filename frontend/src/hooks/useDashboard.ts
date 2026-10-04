"use client";

import { useQueries } from "@tanstack/react-query";
import { apiGet } from "@/lib/api";
import type { ConversationStatus, Paginated } from "@/types";

/** Estados que significan "alguien tiene que atender esto". */
const ABIERTAS: ConversationStatus[] = ["new", "bot_active", "human_active", "waiting_human", "waiting_client"];

async function total(path: string, params: Record<string, unknown> = {}): Promise<number> {
  // page_size=1: solo interesa `total`, no las filas.
  return (await apiGet<Paginated<unknown>>(path, { ...params, page_size: 1 })).total;
}

/**
 * Cifras del inicio, calculadas con los `total` de los listados que ya existen. No hay un
 * endpoint de analitica en el backend (llega con el PR 2 del Sprint 15), asi que por ahora
 * son recuentos, sin series temporales.
 */
export function useDashboardStats() {
  const [esperando, abiertas, contactos, documentos] = useQueries({
    queries: [
      { queryKey: ["stat", "waiting_human"], queryFn: () => total("/conversations", { status: "waiting_human" }), refetchInterval: 30_000 },
      {
        queryKey: ["stat", "open"],
        queryFn: async () => (await Promise.all(ABIERTAS.map((status) => total("/conversations", { status })))).reduce((a, b) => a + b, 0),
        refetchInterval: 30_000,
      },
      { queryKey: ["stat", "contacts"], queryFn: () => total("/contacts"), refetchInterval: 60_000 },
      { queryKey: ["stat", "documents"], queryFn: () => total("/documents", { status: "completed" }), refetchInterval: 60_000 },
    ],
  });
  return { esperando, abiertas, contactos, documentos };
}
