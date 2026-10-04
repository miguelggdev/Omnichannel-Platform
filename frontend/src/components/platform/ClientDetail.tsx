"use client";

import { ArrowLeft, Loader2, ShieldAlert, ShieldCheck } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";
import { QueryError } from "@/components/common/QueryError";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useAppLocale } from "@/hooks/useAppLocale";
import { useClient, useSetClientStatus } from "@/hooks/usePlatform";
import { errorMessage } from "@/lib/api";
import { formatDateTime, formatNumber } from "@/lib/format";
import { useAuthStore } from "@/stores/authStore";

function Dato({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="text-lg font-semibold tabular-nums">{value}</dd>
    </div>
  );
}

export function ClientDetail({ id }: { id: string }) {
  const t = useTranslations("platform.clients");
  const tc = useTranslations("common");
  const locale = useAppLocale();
  const myTenant = useAuthStore((s) => s.user?.client_id);
  const { data, isLoading, error, refetch } = useClient(id);
  const setStatus = useSetClientStatus(id);
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");

  if (error) return <QueryError error={error} onRetry={() => void refetch()} />;
  if (isLoading || !data) return <Skeleton className="h-96 w-full" />;

  const n = (v: number) => formatNumber(v, locale);
  const esElPropio = data.id === myTenant;
  const uso = data.token_used !== null && data.token_budget ? Math.round((data.token_used / data.token_budget) * 100) : null;

  const confirmar = () =>
    setStatus.mutate(
      { is_active: !data.is_active, reason: data.is_active ? reason.trim() || undefined : undefined },
      {
        onSuccess: () => {
          toast.success(data.is_active ? t("suspendedOk") : t("reactivatedOk"));
          setOpen(false);
          setReason("");
        },
        onError: (e) => toast.error(errorMessage(e)),
      },
    );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="icon" asChild aria-label={tc("back")}>
            <Link href="/platform/clients">
              <ArrowLeft className="h-4 w-4 rtl:rotate-180" aria-hidden />
            </Link>
          </Button>
          <div>
            <h1 className="flex items-center gap-2 text-2xl font-semibold">
              {data.name}
              <Badge variant={data.is_active ? "default" : "destructive"}>{data.is_active ? t("active") : t("suspended")}</Badge>
            </h1>
            <p className="font-mono text-xs text-muted-foreground">
              {data.slug} · <span className="capitalize">{data.plan}</span> · {t("since", { when: formatDateTime(data.created_at, locale) })}
            </p>
          </div>
        </div>
        <Button
          variant={data.is_active ? "destructive" : "default"}
          disabled={esElPropio && data.is_active}
          onClick={() => setOpen(true)}
          title={esElPropio && data.is_active ? t("ownTenant") : undefined}
        >
          {data.is_active ? <ShieldAlert className="h-4 w-4" aria-hidden /> : <ShieldCheck className="h-4 w-4" aria-hidden />}
          {data.is_active ? t("suspend") : t("reactivate")}
        </Button>
      </div>

      {!data.is_active && (
        <Card className="border-destructive/40">
          <CardContent className="p-4 text-sm">
            <p className="font-medium">{t("suspendedSince", { when: formatDateTime(data.suspended_at, locale) })}</p>
            {data.alert_message && <p className="mt-1 text-muted-foreground">{data.alert_message}</p>}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>{t("usage")}</CardTitle>
          <CardDescription>{t("usageHint")}</CardDescription>
        </CardHeader>
        <CardContent>
          <dl className="grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
            <Dato label={t("fields.users")} value={`${n(data.users_active)} / ${n(data.users_total)}`} />
            <Dato label={t("fields.contacts")} value={n(data.contacts_total)} />
            <Dato label={t("fields.openConversations")} value={n(data.conversations_open)} />
            <Dato label={t("fields.conversations30d")} value={n(data.conversations_30d)} />
            <Dato label={t("fields.messages30d")} value={n(data.messages_30d)} />
            <Dato label={t("fields.documents")} value={n(data.documents_ready)} />
            <Dato label={t("fields.assistant")} value={data.has_agent ? t("yes") : t("no")} />
            <Dato label={t("fields.tokens")} value={data.token_budget === null ? "—" : data.token_budget === 0 ? t("unlimited") : `${n(data.token_used ?? 0)} / ${n(data.token_budget)}${uso !== null ? ` (${uso} %)` : ""}`} />
          </dl>
          <p className="mt-6 text-sm text-muted-foreground">
            {t("lastActivity")}: {data.last_message_at ? formatDateTime(data.last_message_at, locale) : t("never")}
          </p>
        </CardContent>
      </Card>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent closeLabel={tc("close")}>
          <DialogHeader>
            <DialogTitle>{data.is_active ? t("suspendTitle", { name: data.name }) : t("reactivateTitle", { name: data.name })}</DialogTitle>
            <DialogDescription>{data.is_active ? t("suspendBody") : t("reactivateBody")}</DialogDescription>
          </DialogHeader>
          {data.is_active && (
            <div className="space-y-2">
              <Label htmlFor="reason">{t("reason")}</Label>
              <Textarea id="reason" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={1000} rows={3} />
              <p className="text-xs text-muted-foreground">{t("reasonHint")}</p>
            </div>
          )}
          <DialogFooter className="gap-2 sm:gap-0">
            <Button variant="outline" onClick={() => setOpen(false)} disabled={setStatus.isPending}>
              {tc("cancel")}
            </Button>
            <Button variant={data.is_active ? "destructive" : "default"} onClick={confirmar} disabled={setStatus.isPending}>
              {setStatus.isPending && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
              {data.is_active ? t("suspend") : t("reactivate")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
