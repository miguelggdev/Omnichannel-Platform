"use client";

import axios from "axios";
import { MailWarning, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { useResendVerification, useVerification } from "@/hooks/useVerification";
import { errorMessage } from "@/lib/api";

/** Aviso de correo sin verificar. Solo para cuentas que nacieron del registro publico. */
export function VerifyEmailBanner() {
  const t = useTranslations("verifyEmail.banner");
  const { data } = useVerification();
  const resend = useResendVerification();
  const [hidden, setHidden] = useState(false);

  if (hidden || data?.verified !== false) return null;

  function onResend() {
    resend.mutate(undefined, {
      onSuccess: () => toast.success(t("sent", { email: data?.email ?? "" })),
      onError: (e) =>
        toast.error(axios.isAxiosError(e) && e.response?.status === 429 ? t("tooMany") : errorMessage(e)),
    });
  }

  return (
    <div
      role="status"
      className="flex items-center gap-3 border-b bg-amber-50 px-4 py-2 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100"
    >
      <MailWarning className="h-4 w-4 shrink-0" aria-hidden />
      <p className="min-w-0 flex-1">{t("message", { email: data.email })}</p>
      <Button size="sm" variant="outline" onClick={onResend} disabled={resend.isPending}>
        {t("resend")}
      </Button>
      <Button size="icon" variant="ghost" className="h-7 w-7" onClick={() => setHidden(true)} aria-label={t("dismiss")}>
        <X className="h-4 w-4" aria-hidden />
      </Button>
    </div>
  );
}
