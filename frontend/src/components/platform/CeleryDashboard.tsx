"use client";

import { AlertTriangle, Ban } from "lucide-react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";
import { QueryError } from "@/components/common/QueryError";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useAppLocale } from "@/hooks/useAppLocale";
import { useCeleryQueues, useCeleryTasks, useCeleryWorkers, useRedisInfo, useRevokeTask } from "@/hooks/usePlatform";
import { errorMessage } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import { cn, formatBytes } from "@/lib/utils";
import type { TaskInfo } from "@/types";

const QueueBars = dynamic(() => import("@/components/dashboard/charts").then((m) => m.QueueBars), {
  ssr: false,
  loading: () => <Skeleton className="h-[240px] w-full" />,
});

function duracion(s: number | null | undefined): string {
  if (s === null || s === undefined) return "—";
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  return `${Math.floor(s / 86400)} d`;
}

export function CeleryDashboard() {
  const t = useTranslations("platform.celery");
  const locale = useAppLocale();
  const workers = useCeleryWorkers();
  const queues = useCeleryQueues();
  const tasks = useCeleryTasks();
  const redis = useRedisInfo();
  const revoke = useRevokeTask();
  const [aRevocar, setARevocar] = useState<TaskInfo | null>(null);
  const n = (v: number) => formatNumber(v, locale);

  const memPct = redis.data && redis.data.max_memory > 0 ? Math.round((redis.data.used_memory / redis.data.max_memory) * 100) : null;

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>{t("workers")}</CardTitle>
          <CardDescription>{t("refreshHint")}</CardDescription>
        </CardHeader>
        <CardContent>
          {workers.error ? (
            <QueryError error={workers.error} onRetry={() => void workers.refetch()} />
          ) : !workers.data ? (
            <Skeleton className="h-24 w-full" />
          ) : !workers.data.broker_ok ? (
            <p role="alert" className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden />
              {t("brokerDown")}
            </p>
          ) : workers.data.workers.length === 0 ? (
            <p role="status" className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 p-3 text-sm">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" aria-hidden />
              {t("noWorkers")}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("columns.worker")}</TableHead>
                  <TableHead className="text-end">{t("columns.active")}</TableHead>
                  <TableHead className="hidden text-end sm:table-cell">{t("columns.reserved")}</TableHead>
                  <TableHead className="hidden text-end md:table-cell">{t("columns.processed")}</TableHead>
                  <TableHead className="hidden lg:table-cell">{t("columns.queues")}</TableHead>
                  <TableHead className="hidden text-end lg:table-cell">{t("columns.uptime")}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {workers.data.workers.map((w) => (
                  <TableRow key={w.hostname}>
                    <TableCell>
                      <p className="font-mono text-sm">{w.hostname}</p>
                      <p className="text-xs text-muted-foreground">
                        pid {w.pid ?? "—"} · {t("concurrency", { n: w.concurrency ?? 0 })}
                      </p>
                    </TableCell>
                    <TableCell className="text-end tabular-nums">{w.active_tasks}</TableCell>
                    <TableCell className="hidden text-end tabular-nums sm:table-cell">{w.reserved_tasks}</TableCell>
                    <TableCell className="hidden text-end tabular-nums md:table-cell">{w.processed_total === null ? "—" : n(w.processed_total)}</TableCell>
                    <TableCell className="hidden lg:table-cell">
                      <div className="flex flex-wrap gap-1">
                        {w.queues.map((q) => (
                          <Badge key={q} variant="secondary">{q}</Badge>
                        ))}
                      </div>
                    </TableCell>
                    <TableCell className="hidden text-end lg:table-cell">{duracion(w.uptime_seconds)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>{t("queues")}</CardTitle>
            <CardDescription>{t("queuesHint")}</CardDescription>
          </CardHeader>
          <CardContent>
            {queues.error ? <QueryError error={queues.error} onRetry={() => void queues.refetch()} /> : queues.data ? <QueueBars data={queues.data} /> : <Skeleton className="h-[240px] w-full" />}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Redis</CardTitle>
            <CardDescription>{redis.data ? `v${redis.data.version} · ${t("redisUptime", { time: duracion(redis.data.uptime_seconds) })}` : "…"}</CardDescription>
          </CardHeader>
          <CardContent>
            {redis.error ? (
              <QueryError error={redis.error} onRetry={() => void redis.refetch()} />
            ) : !redis.data ? (
              <Skeleton className="h-40 w-full" />
            ) : (
              <div className="space-y-4">
                <div>
                  <div className="flex items-baseline justify-between text-sm">
                    <span>{t("memory")}</span>
                    <span className="tabular-nums">
                      {formatBytes(redis.data.used_memory)}
                      {redis.data.max_memory > 0 ? ` / ${formatBytes(redis.data.max_memory)}` : ` (${t("noLimit")})`}
                    </span>
                  </div>
                  {memPct !== null && (
                    <div role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.min(memPct, 100)} aria-label={t("memory")} className="mt-2 h-2 overflow-hidden rounded-full bg-muted">
                      <div className={cn("h-full rounded-full", memPct >= 90 ? "bg-destructive" : memPct >= 70 ? "bg-amber-500" : "bg-primary")} style={{ width: `${Math.min(memPct, 100)}%` }} />
                    </div>
                  )}
                </div>
                <dl className="grid grid-cols-3 gap-4 text-sm">
                  <div>
                    <dt className="text-muted-foreground">{t("clients")}</dt>
                    <dd className="text-lg font-semibold tabular-nums">{n(redis.data.connected_clients)}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">{t("keys")}</dt>
                    <dd className="text-lg font-semibold tabular-nums">{n(redis.data.total_keys)}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">{t("hitRatio")}</dt>
                    <dd className="text-lg font-semibold tabular-nums">{redis.data.hit_ratio === null ? "—" : `${(redis.data.hit_ratio * 100).toFixed(1)} %`}</dd>
                  </div>
                </dl>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{t("tasks")}</CardTitle>
          <CardDescription>{t("tasksHint")}</CardDescription>
        </CardHeader>
        <CardContent>
          {tasks.error ? (
            <QueryError error={tasks.error} onRetry={() => void tasks.refetch()} />
          ) : !tasks.data ? (
            <Skeleton className="h-24 w-full" />
          ) : tasks.data.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">{t("noTasks")}</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("columns.task")}</TableHead>
                  <TableHead>{t("columns.state")}</TableHead>
                  <TableHead className="hidden md:table-cell">{t("columns.queue")}</TableHead>
                  <TableHead className="hidden lg:table-cell">{t("columns.worker")}</TableHead>
                  <TableHead className="w-12" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {tasks.data.map((x) => (
                  <TableRow key={x.id}>
                    <TableCell>
                      <p className="font-mono text-xs">{x.name}</p>
                      <p className="font-mono text-xs text-muted-foreground">{x.id}</p>
                    </TableCell>
                    <TableCell>
                      <Badge variant={x.state === "active" ? "default" : "secondary"}>{t(`states.${x.state}`)}</Badge>
                    </TableCell>
                    <TableCell className="hidden md:table-cell">{x.queue ?? "—"}</TableCell>
                    <TableCell className="hidden font-mono text-xs lg:table-cell">{x.worker}</TableCell>
                    <TableCell>
                      {x.state !== "active" && (
                        <Button variant="ghost" size="icon" aria-label={t("revoke")} onClick={() => setARevocar(x)}>
                          <Ban className="h-4 w-4 text-destructive" aria-hidden />
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <ConfirmDialog
        open={aRevocar !== null}
        onOpenChange={(open) => !open && setARevocar(null)}
        title={t("revokeTitle")}
        description={t("revokeBody", { name: aRevocar?.name ?? "" })}
        confirmLabel={t("revoke")}
        destructive
        pending={revoke.isPending}
        onConfirm={() =>
          aRevocar &&
          revoke.mutate(aRevocar.id, {
            onSuccess: () => {
              toast.success(t("revoked"));
              setARevocar(null);
            },
            onError: (e) => toast.error(errorMessage(e)),
          })
        }
      />
    </div>
  );
}
