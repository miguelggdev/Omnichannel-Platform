"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useState } from "react";
import { ChannelBadge } from "@/components/common/ChannelBadge";
import { EmptyState } from "@/components/common/EmptyState";
import { Pagination } from "@/components/common/Pagination";
import { QueryError } from "@/components/common/QueryError";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Card } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useAppLocale } from "@/hooks/useAppLocale";
import { useConversations } from "@/hooks/useConversations";
import { timeAgo } from "@/lib/format";
import { CHANNELS, CONVERSATION_STATUSES, type ConversationStatus } from "@/types";

const TODOS = "all";

export function ConversationList() {
  const t = useTranslations("conversations");
  const locale = useAppLocale();
  const [status, setStatus] = useState<string>(TODOS);
  const [channel, setChannel] = useState<string>(TODOS);
  const [page, setPage] = useState(1);

  const { data, isLoading, error, refetch } = useConversations({
    status: status === TODOS ? undefined : (status as ConversationStatus),
    channel: channel === TODOS ? undefined : channel,
    page,
  });

  const cambiar = (set: (v: string) => void) => (valor: string) => {
    set(valor);
    setPage(1);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-3">
        <div className="w-full sm:w-56">
          <Select value={status} onValueChange={cambiar(setStatus)}>
            <SelectTrigger aria-label={t("filterStatus")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={TODOS}>{t("allStatuses")}</SelectItem>
              {CONVERSATION_STATUSES.map((s) => (
                <SelectItem key={s} value={s}>
                  {t(`status.${s}`)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="w-full sm:w-56">
          <Select value={channel} onValueChange={cambiar(setChannel)}>
            <SelectTrigger aria-label={t("filterChannel")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={TODOS}>{t("allChannels")}</SelectItem>
              {CHANNELS.map((c) => (
                <SelectItem key={c} value={c}>
                  {t(`channel.${c}`)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {error ? (
        <QueryError error={error} onRetry={() => void refetch()} />
      ) : isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 5 }, (_, i) => (
            <Skeleton key={i} className="h-16 w-full" />
          ))}
        </div>
      ) : data && data.items.length > 0 ? (
        <>
          <ul className="space-y-2">
            {data.items.map((c) => (
              <li key={c.id}>
                <Link href={`/conversations/${c.id}`} className="block rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  <Card className="flex flex-wrap items-center justify-between gap-2 p-4 transition-colors hover:bg-accent">
                    <div className="min-w-0">
                      <p className="truncate font-medium">{c.subject ?? t("noSubject")}</p>
                      <p className="text-xs text-muted-foreground">
                        {t("lastActivity", { when: timeAgo(c.last_message_at ?? c.created_at, locale) })}
                      </p>
                    </div>
                    <div className="flex items-center gap-3">
                      <ChannelBadge
                        channel={c.channel}
                        label={(CHANNELS as readonly string[]).includes(c.channel) ? t(`channel.${c.channel}` as "channel.whatsapp") : c.channel}
                      />
                      <StatusBadge status={c.status} label={t(`status.${c.status}`)} />
                    </div>
                  </Card>
                </Link>
              </li>
            ))}
          </ul>
          <Pagination page={page} pageSize={data.page_size} total={data.total} onPageChange={setPage} />
        </>
      ) : (
        <EmptyState title={t("empty")} description={t("emptyHint")} />
      )}
    </div>
  );
}
