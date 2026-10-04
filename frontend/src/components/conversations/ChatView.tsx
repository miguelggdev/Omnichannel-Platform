"use client";

import { ArrowLeft, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useRef } from "react";
import { toast } from "sonner";
import { ChannelBadge } from "@/components/common/ChannelBadge";
import { QueryError } from "@/components/common/QueryError";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  useAssignConversation,
  useChangeStatus,
  useConversation,
  useSendMessage,
} from "@/hooks/useConversations";
import { useContact } from "@/hooks/useContacts";
import { errorMessage } from "@/lib/api";
import { contactName } from "@/lib/utils";
import { useAuthStore } from "@/stores/authStore";
import { CHANNELS, STATUS_TRANSITIONS, type ConversationStatus } from "@/types";
import { MessageBubble } from "./MessageBubble";
import { MessageInput } from "./MessageInput";

export function ChatView({ id }: { id: string }) {
  const t = useTranslations("conversations");
  const { data, isLoading, error, refetch } = useConversation(id);
  const { data: contact } = useContact(data?.contact_id);
  const changeStatus = useChangeStatus(id);
  const assign = useAssignConversation(id);
  const send = useSendMessage(id);
  const user = useAuthStore((s) => s.user);
  const canAssign = useAuthStore((s) => s.hasMinRole("supervisor"));
  const finRef = useRef<HTMLDivElement>(null);
  const total = data?.messages.length ?? 0;

  // Al llegar mensajes nuevos, el chat baja al ultimo.
  useEffect(() => {
    finRef.current?.scrollIntoView({ block: "end" });
  }, [total]);

  if (error) return <QueryError error={error} onRetry={() => void refetch()} />;
  if (isLoading || !data) return <Skeleton className="h-[60vh] w-full" />;

  const transiciones = STATUS_TRANSITIONS[data.status];
  // Un `agent` solo contesta lo suyo o lo que nadie lleva; el backend lo vuelve a comprobar.
  const blockedReason: "closed" | "assignedToOther" | null =
    data.status === "resolved" || data.status === "archived"
      ? "closed"
      : user?.role === "agent" && data.assigned_user_id && data.assigned_user_id !== user.id
        ? "assignedToOther"
        : null;
  const canal = (CHANNELS as readonly string[]).includes(data.channel)
    ? t(`channel.${data.channel}` as "channel.whatsapp")
    : data.channel;

  const onStatus = (status: ConversationStatus) =>
    changeStatus.mutate(status, {
      onSuccess: () => toast.success(t("statusChanged")),
      onError: (e) => toast.error(errorMessage(e)),
    });

  const onAssignMe = () => {
    if (!user) return;
    assign.mutate(user.id, {
      onSuccess: () => toast.success(t("assigned")),
      onError: (e) => toast.error(errorMessage(e)),
    });
  };

  return (
    <div className="flex h-[calc(100vh-8rem)] flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="icon" asChild aria-label={t("back")}>
            <Link href="/conversations">
              <ArrowLeft className="h-4 w-4 rtl:rotate-180" aria-hidden />
            </Link>
          </Button>
          <div>
            <h1 className="text-lg font-semibold leading-tight">
              {contact ? (
                <Link href={`/contacts/${contact.id}`} className="hover:underline">
                  {contactName(contact)}
                </Link>
              ) : (
                (data.subject ?? t("noSubject"))
              )}
            </h1>
            <div className="mt-1 flex items-center gap-3">
              <ChannelBadge channel={data.channel} label={canal} />
              <StatusBadge status={data.status} label={t(`status.${data.status}`)} />
            </div>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {canAssign && data.assigned_user_id !== user?.id && (
            <Button variant="outline" size="sm" onClick={onAssignMe} disabled={assign.isPending}>
              {t("assignToMe")}
            </Button>
          )}
          {transiciones.map((s) => (
            <Button
              key={s}
              variant="secondary"
              size="sm"
              onClick={() => onStatus(s)}
              disabled={changeStatus.isPending}
            >
              {changeStatus.isPending && changeStatus.variables === s && (
                <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
              )}
              {t("moveTo", { status: t(`status.${s}`) })}
            </Button>
          ))}
        </div>
      </div>

      <Card className="flex min-h-0 flex-1 flex-col">
        <div
          className="flex-1 space-y-4 overflow-y-auto p-4"
          role="log"
          aria-live="polite"
          aria-label={t("messages")}
        >
          {data.truncated && (
            <p className="text-center text-xs text-muted-foreground">{t("earlierNotLoaded")}</p>
          )}
          {data.messages.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">{t("noMessages")}</p>
          ) : (
            data.messages.map((m) => <MessageBubble key={m.id} message={m} />)
          )}
          <div ref={finRef} />
        </div>
        <MessageInput
          conversationId={id}
          blockedReason={blockedReason}
          sending={send.isPending}
          onSend={(text) => send.mutateAsync(text)}
        />
      </Card>
    </div>
  );
}
