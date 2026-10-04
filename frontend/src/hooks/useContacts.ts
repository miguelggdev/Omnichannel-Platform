"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiPut } from "@/lib/api";
import type { Contact, ContactDetail, Conversation, Paginated } from "@/types";

export interface ContactInput {
  first_name?: string | null;
  last_name?: string | null;
  display_name?: string | null;
}

export function useContacts(params: { search?: string; page?: number; page_size?: number }) {
  return useQuery({
    queryKey: ["contacts", params],
    queryFn: () => apiGet<Paginated<Contact>>("/contacts", { page_size: 20, ...params }),
    placeholderData: keepPreviousData,
  });
}

export function useContact(id: string | undefined) {
  return useQuery({
    queryKey: ["contact", id],
    queryFn: () => apiGet<ContactDetail>(`/contacts/${id}`),
    enabled: id !== undefined,
  });
}

export function useContactConversations(id: string) {
  return useQuery({
    queryKey: ["contact", id, "conversations"],
    queryFn: () => apiGet<Paginated<Conversation>>(`/contacts/${id}/conversations`, { page_size: 20 }),
  });
}

export function useCreateContact() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: ContactInput) => apiPost<Contact>("/contacts", data),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["contacts"] }),
  });
}

export function useUpdateContact(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: ContactInput) => apiPut<Contact>(`/contacts/${id}`, data),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["contacts"] });
      void queryClient.invalidateQueries({ queryKey: ["contact", id] });
    },
  });
}

export function useAddNote(contactId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (content: string) => apiPost(`/contacts/${contactId}/notes`, { content }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["contact", contactId] }),
  });
}
