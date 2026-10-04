"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiPut } from "@/lib/api";
import type { AssignableRole, Paginated, TeamUser } from "@/types";

export interface UserFilters {
  search?: string;
  role?: AssignableRole;
  is_active?: boolean;
  page?: number;
}

export interface UserCreateInput {
  email: string;
  password: string;
  first_name: string;
  last_name: string;
  role: AssignableRole;
}

/** Solo se envia lo que cambia; el backend no toca los campos omitidos. */
export interface UserUpdateInput {
  first_name?: string;
  last_name?: string;
  role?: AssignableRole;
  is_active?: boolean;
  password?: string;
}

export function useUsers(filters: UserFilters) {
  return useQuery({
    queryKey: ["users", filters],
    queryFn: () => apiGet<Paginated<TeamUser>>("/admin/users", { page_size: 20, ...filters }),
    placeholderData: keepPreviousData,
  });
}

export function useCreateUser() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: UserCreateInput) => apiPost<TeamUser>("/admin/users", data),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["users"] }),
  });
}

export function useUpdateUser() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: UserUpdateInput }) =>
      apiPut<TeamUser>(`/admin/users/${id}`, data),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["users"] }),
  });
}
