"use client";

import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { apiPost } from "@/lib/api";

type Estado = "verifying" | "ok" | "failed";

/** Confirma el email con el token del enlace. Corre una sola vez al abrir la pagina. */
export function VerifyEmail({ token }: { token: string | null }) {
  const t = useTranslations("verifyEmail");
  const [estado, setEstado] = useState<Estado>(token ? "verifying" : "failed");

  useEffect(() => {
    if (!token) return;
    let activo = true;
    apiPost("/onboarding/verify-email", { token })
      .then(() => activo && setEstado("ok"))
      .catch(() => activo && setEstado("failed"));
    return () => {
      activo = false;
    };
  }, [token]);

  return (
    <div className="space-y-4 text-center" role="status" aria-live="polite">
      {estado === "verifying" && (
        <>
          <Loader2 className="mx-auto h-10 w-10 animate-spin text-muted-foreground" aria-hidden />
          <p>{t("verifying")}</p>
        </>
      )}
      {estado === "ok" && (
        <>
          <CheckCircle2 className="mx-auto h-10 w-10 text-green-600" aria-hidden />
          <h2 className="text-lg font-semibold">{t("successTitle")}</h2>
          <p className="text-sm text-muted-foreground">{t("successDesc")}</p>
        </>
      )}
      {estado === "failed" && (
        <>
          <XCircle className="mx-auto h-10 w-10 text-destructive" aria-hidden />
          <h2 className="text-lg font-semibold">{t("failedTitle")}</h2>
          <p className="text-sm text-muted-foreground">{t("failedDesc")}</p>
        </>
      )}
      {estado !== "verifying" && (
        <Button asChild>
          <Link href="/">{t("goToApp")}</Link>
        </Button>
      )}
    </div>
  );
}
