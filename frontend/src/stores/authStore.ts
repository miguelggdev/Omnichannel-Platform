import { useEffect, useState } from "react";
import { create } from "zustand";
import { persist } from "zustand/middleware";
import { userFromToken } from "@/lib/jwt";
import type { User, UserRole } from "@/types/auth";

/**
 * `medical` es un rol de la historia clinica, fuera de la jerarquia operativa: no lee
 * conversaciones ni contactos en el backend, asi que no alcanza ni el nivel `agent`.
 */
export const ROLE_HIERARCHY: Record<UserRole, number> = {
  super_admin: 4,
  admin: 3,
  supervisor: 2,
  agent: 1,
  medical: 0,
};

interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  user: User | null;
  setTokens: (accessToken: string, refreshToken: string) => void;
  logout: () => void;
  hasMinRole: (minRole: UserRole) => boolean;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      accessToken: null,
      refreshToken: null,
      user: null,

      setTokens: (accessToken, refreshToken) =>
        set({ accessToken, refreshToken, user: userFromToken(accessToken) }),

      logout: () => set({ accessToken: null, refreshToken: null, user: null }),

      hasMinRole: (minRole) => {
        const rol = get().user?.role;
        return rol !== undefined && ROLE_HIERARCHY[rol] >= ROLE_HIERARCHY[minRole];
      },
    }),
    {
      name: "auth-storage",
      partialize: (s) => ({ accessToken: s.accessToken, refreshToken: s.refreshToken, user: s.user }),
    },
  ),
);

/**
 * `true` cuando zustand ya leyo la sesion del almacenamiento del navegador.
 *
 * Hasta entonces `accessToken` es `null` aunque haya sesion guardada, asi que decidir antes
 * mandaria a /login a quien ya esta dentro. No se usa `onRehydrateStorage` con un flag dentro
 * del store porque esa callback corre durante `create()`, antes de que exista `useAuthStore`.
 */
export function useAuthHydrated(): boolean {
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => {
    const unsubscribe = useAuthStore.persist.onFinishHydration(() => setHydrated(true));
    setHydrated(useAuthStore.persist.hasHydrated());
    return unsubscribe;
  }, []);
  return hydrated;
}
