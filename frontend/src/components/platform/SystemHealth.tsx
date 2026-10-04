"use client";

import { CheckCircle2, RefreshCw, XCircle } from "lucide-react";
import { useTranslations } from "next-intl";
import { QueryError } from "@/components/common/QueryError";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useSystemStatus } from "@/hooks/usePlatform";

export function SystemHealth() {
  const t = useTranslations("platform.system");
  const { data, error, refetch, isFetching } = useSystemStatus();

  if (error) return <QueryError error={error} onRetry={() => void refetch()} />;
  if (!data) return <Skeleton className="h-48 w-full" />;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          {t("versionLine", { version: data.version, env: data.environment })}
        </p>
        <Button variant="outline" size="sm" onClick={() => void refetch()} disabled={isFetching}>
          <RefreshCw className={isFetching ? "h-4 w-4 animate-spin" : "h-4 w-4"} aria-hidden />
          {t("refresh")}
        </Button>
      </div>
      <div className="grid gap-4 md:grid-cols-3">
        {data.components.map((c) => (
          <Card key={c.name} className={c.ok ? undefined : "border-destructive/40"}>
            <CardContent className="space-y-1 p-6">
              <div className="flex items-center gap-2">
                {c.ok ? <CheckCircle2 className="h-5 w-5 text-emerald-600" aria-hidden /> : <XCircle className="h-5 w-5 text-destructive" aria-hidden />}
                <h2 className="font-semibold">{t(`components.${c.name}` as "components.database")}</h2>
              </div>
              <p className="text-sm">{c.ok ? t("ok") : t("down")}</p>
              {c.latency_ms !== null && <p className="text-xs text-muted-foreground">{t("latency", { ms: c.latency_ms })}</p>}
              {c.detail && <p className="font-mono text-xs text-muted-foreground">{c.detail}</p>}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
