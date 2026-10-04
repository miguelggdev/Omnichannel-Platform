"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiPut } from "@/lib/api";
import { CONVERSATIONS_POLL_MS, MESSAGES_POLL_MS } from "@/lib/constants";
import type {
  Conversation,
  ConversationDetail,
  ConversationStatus,
  Message,
  Paginated,
} from "@/types";

export interface ConversationFilters {
  status?: ConversationStatus;
  channel?: string;
  assigned_user_id?: string;
  page?: number;
  page_size?: number;
}

/** Mensajes que se piden de una vez; el backend admite hasta 200 por pagina. */
export const MESSAGES_PAGE_SIZE = 200;

/**
 * La lista se refresca por sondeo. El spec pedia Supabase Realtime, pero las politicas RLS de
 * `messages` leen `app.current_client_id` (una variable que fija el backend por transaccion) y
 * Realtime no la tiene, asi que una suscripcion directa no veria filas. Ver ADR-079.
 */
export function useConversations(filters: ConversationFilters = {}) {
  return useQuery({
    queryKey: ["conversations", filters],
    queryFn: () => apiGet<Paginated<Conversation>>("/conversations", { page_size: 20, ...filters }),
    placeholderData: keepPreviousData,
    refetchInterval: CONVERSATIONS_POLL_MS,
  });
}

export interface ConversationView extends ConversationDetail {
  /** `true` si hay mensajes anteriores que no se cargaron (mas de 200). */
  truncated: boolean;
}

/** El detalle con los ultimos mensajes: si hay mas de 200, pide la ultima pagina. */
export function useConversation(id: string) {
  return useQuery({
    queryKey: ["conversation", id],
    refetchInterval: MESSAGES_POLL_MS,
    queryFn: async (): Promise<ConversationView> => {
      const params = { page: 1, page_size: MESSAGES_PAGE_SIZE };
      const primera = await apiGet<ConversationDetail>(`/conversations/${id}`, params);
      if (primera.total_messages <= MESSAGES_PAGE_SIZE) return { ...primera, truncated: false };
      const ultimaPagina = Math.ceil(primera.total_messages / MESSAGES_PAGE_SIZE);
      const ultima = await apiGet<ConversationDetail>(`/conversations/${id}`, {
        ...params,
        page: ultimaPagina,
      });
      return { ...ultima, truncated: true };
    },
  });
}

export function useChangeStatus(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (status: ConversationStatus) => apiPut(`/conversations/${id}/status`, { status }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["conversation", id] });
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}

export function useAssignConversation(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (userId: string) => apiPut(`/conversations/${id}/assign`, { user_id: userId }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["conversation", id] });
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}

/** Una persona del equipo contesta; al hacerlo toma la conversacion (human_active). */
export function useSendMessage(id: string) {
  const queryClient = useQueryClient();
  const refrescar = () => {
    void queryClient.invalidateQueries({ queryKey: ["conversation", id] });
    void queryClient.invalidateQueries({ queryKey: ["conversations"] });
  };
  return useMutation({
    mutationFn: (text: string) => apiPost<Message>(`/conversations/${id}/messages`, { text }),
    onSuccess: refrescar,
    // Aunque el envio falle, el backend ya pudo dejar la conversacion en manos de la persona.
    onError: refrescar,
  });
}
