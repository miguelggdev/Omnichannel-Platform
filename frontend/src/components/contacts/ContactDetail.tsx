"use client";

import { ArrowLeft, Pencil, Send } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";
import { ChannelBadge } from "@/components/common/ChannelBadge";
import { QueryError } from "@/components/common/QueryError";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useAppLocale } from "@/hooks/useAppLocale";
import {
  useAddNote,
  useContact,
  useContactConversations,
  useUpdateContact,
} from "@/hooks/useContacts";
import { errorMessage } from "@/lib/api";
import { formatDateTime, timeAgo } from "@/lib/format";
import { contactName } from "@/lib/utils";
import { CHANNELS } from "@/types";
import { ContactForm } from "./ContactForm";

export function ContactDetail({ id }: { id: string }) {
  const t = useTranslations("contacts");
  const tConv = useTranslations("conversations");
  const tc = useTranslations("common");
  const locale = useAppLocale();
  const { data, isLoading, error, refetch } = useContact(id);
  const conversations = useContactConversations(id);
  const update = useUpdateContact(id);
  const addNote = useAddNote(id);
  const [editing, setEditing] = useState(false);
  const [note, setNote] = useState("");

  if (error) return <QueryError error={error} onRetry={() => void refetch()} />;
  if (isLoading || !data) return <Skeleton className="h-96 w-full" />;

  const submitNote = () => {
    const content = note.trim();
    if (!content) return;
    addNote.mutate(content, {
      onSuccess: () => {
        setNote("");
        toast.success(tc("saved"));
      },
      onError: (e) => toast.error(errorMessage(e)),
    });
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="icon" asChild aria-label={tc("back")}>
            <Link href="/contacts">
              <ArrowLeft className="h-4 w-4 rtl:rotate-180" aria-hidden />
            </Link>
          </Button>
          <div>
            <h1 className="text-2xl font-semibold">{contactName(data)}</h1>
            <p className="text-sm text-muted-foreground">
              {t("since", { when: formatDateTime(data.created_at, locale) })}
            </p>
          </div>
        </div>
        <Button variant="outline" onClick={() => setEditing(true)}>
          <Pencil className="h-4 w-4" aria-hidden />
          {tc("edit")}
        </Button>
      </div>

      <Dialog open={editing} onOpenChange={setEditing}>
        <DialogContent closeLabel={tc("close")}>
          <DialogHeader>
            <DialogTitle>{tc("edit")}</DialogTitle>
          </DialogHeader>
          <ContactForm
            initial={data}
            onSubmit={(d) => update.mutateAsync(d)}
            onDone={() => setEditing(false)}
          />
        </DialogContent>
      </Dialog>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>{t("identifiers")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {data.identifiers.length === 0 && <p className="text-sm text-muted-foreground">{t("noIdentifiers")}</p>}
            {data.identifiers.map((i) => (
              <div key={i.id} className="flex items-center justify-between gap-2 text-sm">
                <ChannelBadge
                  channel={i.channel}
                  label={(CHANNELS as readonly string[]).includes(i.channel) ? tConv(`channel.${i.channel}` as "channel.whatsapp") : i.channel}
                />
                <span className="truncate font-mono text-xs">{i.identifier_value}</span>
              </div>
            ))}
            {data.tags.length > 0 && (
              <div className="flex flex-wrap gap-1 pt-2">
                {data.tags.map((tag) => (
                  <Badge key={tag.id} variant="secondary">
                    {tag.name}
                  </Badge>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{t("conversations")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {conversations.data?.items.length === 0 && (
              <p className="text-sm text-muted-foreground">{t("noConversations")}</p>
            )}
            {conversations.data?.items.map((c) => (
              <Link
                key={c.id}
                href={`/conversations/${c.id}`}
                className="flex items-center justify-between gap-2 rounded-md p-2 text-sm hover:bg-accent"
              >
                <span className="text-muted-foreground">{timeAgo(c.last_message_at ?? c.created_at, locale)}</span>
                <StatusBadge status={c.status} label={tConv(`status.${c.status}`)} />
              </Link>
            ))}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{t("notes")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-col gap-2 sm:flex-row">
            <Textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder={t("notePlaceholder")}
              aria-label={t("notePlaceholder")}
              maxLength={5000}
            />
            <Button onClick={submitNote} disabled={!note.trim() || addNote.isPending} className="sm:self-end">
              <Send className="h-4 w-4" aria-hidden />
              {t("addNote")}
            </Button>
          </div>
          {data.notes.length === 0 && <p className="text-sm text-muted-foreground">{t("noNotes")}</p>}
          <ul className="space-y-3">
            {data.notes.map((n) => (
              <li key={n.id} className="rounded-md border p-3 text-sm">
                <p className="whitespace-pre-wrap break-words">{n.content}</p>
                <p className="mt-1 text-xs text-muted-foreground">{formatDateTime(n.created_at, locale)}</p>
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}
