"use client";

import { useTranslations } from "next-intl";
import { toast } from "sonner";
import { QueryError } from "@/components/common/QueryError";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { useFeatureFlags, useSetFlag } from "@/hooks/useSandbox";
import { errorMessage } from "@/lib/api";

export function FeatureFlagsPanel() {
  const t = useTranslations("flags");
  const { data, isLoading, error, refetch } = useFeatureFlags();
  const setFlag = useSetFlag();

  if (error) return <QueryError error={error} onRetry={() => void refetch()} />;
  if (isLoading || !data) return <Skeleton className="h-48 w-full" />;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
        <CardDescription>{t("subtitle")}</CardDescription>
      </CardHeader>
      <CardContent className="divide-y">
        {data.flags.map((f) => {
          const esBooleana = f.value === null || typeof f.value === "boolean";
          return (
            <div key={f.flag} className="flex items-center justify-between gap-4 py-3">
              <div className="min-w-0">
                <p className="font-mono text-sm">{f.flag}</p>
                <div className="mt-1 flex flex-wrap gap-1">
                  {!f.enforced && <Badge variant="outline">{t("notEnforced")}</Badge>}
                  {!f.editable && <Badge variant="secondary">{t("superAdminOnly")}</Badge>}
                </div>
              </div>
              {esBooleana ? (
                <Switch
                  checked={f.value === true}
                  disabled={!f.editable || setFlag.isPending}
                  aria-label={f.flag}
                  onCheckedChange={(value) =>
                    setFlag.mutate(
                      { flag: f.flag, value },
                      {
                        onSuccess: () => toast.success(t("updated")),
                        onError: (e) => toast.error(errorMessage(e)),
                      },
                    )
                  }
                />
              ) : (
                <span className="text-sm tabular-nums">{String(f.value)}%</span>
              )}
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
