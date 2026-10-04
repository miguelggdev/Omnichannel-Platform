"use client";

import { Bot, User } from "lucide-react";
import { useTranslations } from "next-intl";
import { useAppLocale } from "@/hooks/useAppLocale";
import { formatTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { Message } from "@/types";

const SENDERS = ["contact", "bot", "agent", "system"] as const;

export function MessageBubble({ message }: { message: Message }) {
  const t = useTranslations("conversations");
  const locale = useAppLocale();
  const saliente = message.direction === "outbound";
  const esBot = message.sender_type === "bot";
  const remitente = (SENDERS as readonly string[]).includes(message.sender_type)
    ? t(`sender.${message.sender_type}` as "sender.bot")
    : message.sender_type;
  const Icono = esBot ? Bot : User;

  return (
    <div className={cn("flex gap-2", saliente ? "flex-row-reverse" : "flex-row")}>
      <div
        className="mt-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-muted"
        aria-hidden
      >
        <Icono className="h-4 w-4 text-muted-foreground" />
      </div>
      <div className={cn("flex max-w-[80%] flex-col gap-1", saliente ? "items-end" : "items-start")}>
        <div
          className={cn(
            "whitespace-pre-wrap break-words rounded-2xl px-3 py-2 text-sm",
            saliente ? "bg-primary text-primary-foreground" : "bg-muted",
          )}
        >
          {message.content ?? (
            <span className="italic opacity-80">{t("noText", { type: message.message_type })}</span>
          )}
          {message.media_url && (
            <a
              href={message.media_url}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-1 block text-xs underline"
            >
              {t("attachment")}
            </a>
          )}
        </div>
        <span className="text-xs text-muted-foreground">
          {remitente} · {formatTime(message.created_at, locale)}
        </span>
      </div>
    </div>
  );
}
