"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, apiDelete, apiGet, apiPut } from "@/lib/api";
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

const LOGO = "/admin/business-profile/logo";

export function useUploadLogo() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (archivo: File) => {
      const body = new FormData();
      body.append("file", archivo);
      // Sin `Content-Type`: el navegador pone el multipart con su boundary.
      return (
        await api.post<BusinessProfile>(LOGO, body, { headers: { "Content-Type": undefined } })
      ).data;
    },
    onSuccess: (perfil) => {
      queryClient.setQueryData(["business-profile"], perfil);
      void queryClient.invalidateQueries({ queryKey: ["business-profile-logo"] });
    },
  });
}

export function useDeleteLogo() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiDelete<BusinessProfile>(LOGO),
    onSuccess: (perfil) => {
      queryClient.setQueryData(["business-profile"], perfil);
      queryClient.removeQueries({ queryKey: ["business-profile-logo"] });
    },
  });
}

/** El logo subido como `blob:` URL (la descarga lleva el token, asi que no vale un `<img src>` directo). */
export function useLogoBlob(subido: boolean) {
  return useQuery({
    queryKey: ["business-profile-logo"],
    enabled: subido,
    queryFn: async () => URL.createObjectURL((await api.get<Blob>(LOGO, { responseType: "blob" })).data),
    staleTime: Infinity,
    gcTime: 0,
  });
}
