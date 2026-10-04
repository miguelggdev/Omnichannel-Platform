"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPut } from "@/lib/api";
import type { BusinessProfile, BusinessProfileUpdate } from "@/types";

export function useBusinessProfile() {
  return useQuery({
    queryKey: ["business-profile"],
    queryFn: () => apiGet<BusinessProfile>("/admin/business-profile"),
  });
}

export function useUpdateBusinessProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: BusinessProfileUpdate) => apiPut<BusinessProfile>("/admin/business-profile", data),
    onSuccess: (perfil) => queryClient.setQueryData(["business-profile"], perfil),
  });
}
