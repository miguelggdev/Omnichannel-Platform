"use client";

import { Loader2, ShieldAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { refreshAccessToken } from "@/lib/api";
import { useAuthHydrated, useAuthStore } from "@/stores/authStore";
import type { UserRole } from "@/types";

interface ProtectedRouteProps {
  children: React.ReactNode;
  /** Rol minimo de la jerarquia super_admin > admin > supervisor > agent. */
  minRole?: UserRole;
}

/**
 * Guarda de rutas del lado del cliente: sin sesion lleva a /login y con un rol insuficiente
 * muestra un aviso. Es comodidad de interfaz, no seguridad: cada endpoint del backend vuelve a
 * comprobar el rol.
 */
export function ProtectedRoute({ children, minRole = "agent" }: ProtectedRouteProps) {
  const t = useTranslations("errors");
  const router = useRouter();
  const hydrated = useAuthHydrated();
  const accessToken = useAuthStore((s) => s.accessToken);
  const allowed = useAuthStore((s) => s.hasMinRole(minRole));
  // Una sola comprobacion por montaje: si la cookie no vale, no se reintenta en bucle.
  const [intentado, setIntentado] = useState(false);

  // Tras recargar no hay access token en memoria: se pide uno con la cookie de sesion. Solo si
  // eso falla no hay sesion y se va a /login.
  useEffect(() => {
    if (!hydrated || accessToken || intentado) return;
    setIntentado(true);
    refreshAccessToken().catch(() => {
      useAuthStore.getState().logout();
      router.replace("/login");
    });
  }, [hydrated, accessToken, intentado, router]);

  if (!hydrated || !accessToken) {
    return (
      <div className="flex min-h-screen items-center justify-center" role="status">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" aria-hidden />
        <span className="sr-only">{t("loading")}</span>
      </div>
    );
  }

  if (!allowed) {
    return (
      <div className="flex min-h-[50vh] flex-col items-center justify-center gap-3 p-6 text-center">
        <ShieldAlert className="h-10 w-10 text-muted-foreground" aria-hidden />
        <h1 className="text-xl font-semibold">{t("forbiddenTitle")}</h1>
        <p className="text-muted-foreground">{t("forbiddenBody")}</p>
        <Button variant="outline" onClick={() => router.replace("/")}>
          {t("backHome")}
        </Button>
      </div>
    );
  }

  return <>{children}</>;
}
