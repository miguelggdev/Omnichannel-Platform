"use client";

import { AlertTriangle, CheckCircle2, RefreshCw, XCircle } from "lucide-react";
import { useTranslations } from "next-intl";
import { QueryError } from "@/components/common/QueryError";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useSecurityOverview } from "@/hooks/usePlatform";
import type { SecurityStatus } from "@/types";

type CheckId =
  | "environment"
  | "jwt_lifetime"
  | "cors"
  | "webhook_secrets"
  | "onboarding_proxy_hops"
  | "onboarding_smtp"
  | "onboarding_disabled"
  | "rls_tables"
  | "db_role_rls";

const ICON: Record<SecurityStatus, React.ReactNode> = {
  ok: <CheckCircle2 className="h-5 w-5 text-emerald-600" aria-hidden />,
  warn: <AlertTriangle className="h-5 w-5 text-amber-600" aria-hidden />,
  fail: <XCircle className="h-5 w-5 text-destructive" aria-hidden />,
};

export function SecurityOverview() {
  const t = useTranslations("platform.security");
  const { data, error, refetch, isFetching } = useSecurityOverview();

  if (error) return <QueryError error={error} onRetry={() => void refetch()} />;
  if (!data) return <Skeleton className="h-64 w-full" />;

  const fails = data.checks.filter((c) => c.status === "fail").length;
  const warns = data.checks.filter((c) => c.status === "warn").length;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p role="status" className="text-sm text-muted-foreground">
          {t("summary", { fails, warns, env: data.environment })}
        </p>
        <Button variant="outline" size="sm" onClick={() => void refetch()} disabled={isFetching}>
          <RefreshCw className={isFetching ? "h-4 w-4 animate-spin" : "h-4 w-4"} aria-hidden />
          {t("refresh")}
        </Button>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        {data.checks.map((c) => (
          <Card key={c.id} className={c.status === "fail" ? "border-destructive/40" : undefined}>
            <CardContent className="space-y-1 p-6">
              <div className="flex items-center gap-2">
                {ICON[c.status]}
                <h2 className="font-semibold">{t(`checks.${c.id as CheckId}.label`)}</h2>
                <span className="sr-only">{t(`status.${c.status}`)}</span>
              </div>
              {c.status !== "ok" && (
                <p className="text-sm">{t(`checks.${c.id as CheckId}.help`)}</p>
              )}
              {c.detail && <p className="font-mono text-xs text-muted-foreground">{c.detail}</p>}
            </CardContent>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("rls.title")}</CardTitle>
          <CardDescription>{t("rls.description", { count: data.rls_tables.length })}</CardDescription>
        </CardHeader>
        <CardContent>
          {data.rls_unprotected.length > 0 && (
            <p role="alert" className="mb-3 rounded-md bg-destructive/10 p-3 text-sm text-destructive">
              {t("rls.unprotected", { tables: data.rls_unprotected.join(", ") })}
            </p>
          )}
          <ul className="grid gap-1 sm:grid-cols-2 lg:grid-cols-3">
            {data.rls_tables.map((tb) => {
              const ok = tb.rls_enabled && tb.rls_forced;
              return (
                <li key={tb.name} className="flex items-center justify-between gap-2 text-sm">
                  <span className="font-mono">{tb.name}</span>
                  <Badge variant={ok ? "secondary" : "destructive"}>
                    {ok ? t("rls.forced") : tb.rls_enabled ? t("rls.notForced") : t("rls.off")}
                  </Badge>
                </li>
              );
            })}
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}
