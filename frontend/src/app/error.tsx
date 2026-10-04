"use client";

import { useTranslations } from "next-intl";
import { useEffect } from "react";
import { Button } from "@/components/ui/button";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const t = useTranslations("errors");

  useEffect(() => {
    // Solo al log del navegador: la pantalla no muestra detalles tecnicos.
    console.error(error);
  }, [error]);

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-4 p-6 text-center">
      <h1 className="text-2xl font-semibold">{t("genericTitle")}</h1>
      <p className="text-muted-foreground">{t("genericBody")}</p>
      <Button onClick={reset}>{t("retry")}</Button>
    </main>
  );
}
