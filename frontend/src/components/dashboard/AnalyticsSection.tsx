"use client";

import { Clock, Coins, MessageSquare, ThumbsUp, UserRoundCog, Users, Zap } from "lucide-react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { QueryError } from "@/components/common/QueryError";
import { StatCard } from "@/components/dashboard/StatCard";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useAnalytics } from "@/hooks/useAnalytics";
import { useAppLocale } from "@/hooks/useAppLocale";
import { formatNumber } from "@/lib/format";
import { cn } from "@/lib/utils";

// Recharts pesa: solo se descarga cuando hay graficas que pintar.
const ChannelPie = dynamic(() => import("./charts").then((m) => m.ChannelPie), {
  ssr: false,
  loading: () => <Skeleton className="h-[260px] w-full" />,
});
const MessagesLine = dynamic(() => import("./charts").then((m) => m.MessagesLine), {
  ssr: false,
  loading: () => <Skeleton className="h-[260px] w-full" />,
});

/** "42 s", "3 min" o "1 h 5 min": el tiempo de respuesta cubre desde segundos hasta horas. */
export function formatDuration(seconds: number | null | undefined): string | undefined {
  if (seconds === null || seconds === undefined) return undefined;
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const min = Math.round(seconds / 60);
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  return min % 60 === 0 ? `${h} h` : `${h} h ${min % 60} min`;
}

export function AnalyticsSection() {
  const t = useTranslations("dashboard");
  const locale = useAppLocale();
  const { metrics, byChannel, overTime } = useAnalytics(true);
  const m = metrics.data;
  const n = (v: number | undefined) => (v === undefined ? undefined : formatNumber(v, locale));
  const sinDato = t("noData");

  if (metrics.error) return <QueryError error={metrics.error} onRetry={() => void metrics.refetch()} />;

  const uso = m?.token_usage_percentage;

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        <StatCard label={t("waitingHuman")} value={n(m?.waiting_human)} icon={UserRoundCog} hint={t("waitingHumanHint")} />
        <StatCard
          label={t("openConversations")}
          value={n(m?.active_conversations)}
          icon={MessageSquare}
          hint={t("newLast7Days", { count: m?.conversations_last_7_days ?? 0 })}
          trend={m?.conversations_trend}
          trendLabel={t("vsPrevious7Days")}
        />
        <StatCard
          label={t("messages24h")}
          value={n(m?.messages_last_24h)}
          icon={Zap}
          trend={m?.messages_trend}
          trendLabel={t("vsPrevious24h")}
        />
        <StatCard
          label={t("responseTime")}
          value={m ? (formatDuration(m.median_response_time_seconds) ?? sinDato) : undefined}
          icon={Clock}
          hint={m?.avg_response_time_seconds != null ? t("averageIs", { value: formatDuration(m.avg_response_time_seconds) ?? "" }) : t("responseTimeHint")}
        />
        <StatCard
          label={t("csat")}
          value={m ? (m.csat_score !== null ? `${m.csat_score.toLocaleString(locale)} / 5` : sinDato) : undefined}
          icon={ThumbsUp}
          hint={m ? t("csatResponses", { count: m.csat_responses }) : undefined}
        />
        <StatCard
          label={t("tokens")}
          value={m ? (uso != null ? `${uso.toLocaleString(locale)} %` : t("unlimited")) : undefined}
          icon={Coins}
          hint={t("tokensHint")}
          footer={
            uso != null ? (
              <div
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.min(uso, 100)}
                aria-label={t("tokens")}
                className="mt-3 h-2 overflow-hidden rounded-full bg-muted"
              >
                <div
                  className={cn("h-full rounded-full", uso >= 90 ? "bg-destructive" : uso >= 70 ? "bg-amber-500" : "bg-primary")}
                  style={{ width: `${Math.min(uso, 100)}%` }}
                />
              </div>
            ) : undefined
          }
        />
        <StatCard label={t("contacts")} value={n(m?.total_contacts)} icon={Users} />
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>{t("messagesOverTime")}</CardTitle>
            <CardDescription>{t("messagesOverTimeHint")}</CardDescription>
          </CardHeader>
          <CardContent>
            {overTime.error ? (
              <QueryError error={overTime.error} onRetry={() => void overTime.refetch()} />
            ) : overTime.data ? (
              <MessagesLine data={overTime.data} />
            ) : (
              <Skeleton className="h-[260px] w-full" />
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>{t("conversationsByChannel")}</CardTitle>
            <CardDescription>{t("conversationsByChannelHint")}</CardDescription>
          </CardHeader>
          <CardContent>
            {byChannel.error ? (
              <QueryError error={byChannel.error} onRetry={() => void byChannel.refetch()} />
            ) : byChannel.data ? (
              byChannel.data.length > 0 ? (
                <ChannelPie data={byChannel.data} />
              ) : (
                <p className="py-16 text-center text-sm text-muted-foreground">{t("noConversations")}</p>
              )
            ) : (
              <Skeleton className="h-[260px] w-full" />
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
