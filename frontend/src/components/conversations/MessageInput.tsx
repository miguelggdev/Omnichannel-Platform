"use client";

import { Loader2, Lock, Send, Zap } from "lucide-react";
import { useTranslations } from "next-intl";
import { useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Textarea } from "@/components/ui/textarea";
import { renderQuickReply, useQuickReplies } from "@/hooks/useQuickReplies";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { QuickReply } from "@/types";

interface MessageInputProps {
  conversationId: string;
  /** Por que no se puede escribir (conversacion cerrada o de otra persona); `null` si se puede. */
  blockedReason: "closed" | "assignedToOther" | null;
  sending: boolean;
  onSend: (text: string) => Promise<unknown>;
}

const MAX_CHARS = 4000;

/** Un atajo a medio escribir: empieza por "/" y todavia no tiene espacios. */
const SLASH = /^\/\S*$/;

export function MessageInput({ conversationId, blockedReason, sending, onSend }: MessageInputProps) {
  const t = useTranslations("conversations.reply");
  const [text, setText] = useState("");
  const [active, setActive] = useState(0);
  const areaRef = useRef<HTMLTextAreaElement>(null);
  const { data: replies } = useQuickReplies();

  const sugerencias = useMemo(() => {
    if (!replies || !SLASH.test(text)) return [];
    const typed = text.toLowerCase();
    return replies.filter((r) => r.shortcut.toLowerCase().startsWith(typed)).slice(0, 6);
  }, [replies, text]);

  if (blockedReason) {
    return (
      <div className="flex items-center gap-2 border-t bg-muted/40 px-4 py-3 text-sm text-muted-foreground">
        <Lock className="h-4 w-4 shrink-0" aria-hidden />
        {t(blockedReason)}
      </div>
    );
  }

  const usar = async (reply: QuickReply) => {
    try {
      const r = await renderQuickReply(reply.id, conversationId);
      setText(r.content);
      if (r.unresolved.length > 0) {
        // Se deja el texto con {{variable}} a la vista para que la persona lo corrija.
        toast.warning(t("unresolved", { vars: r.unresolved.join(", ") }));
      }
      areaRef.current?.focus();
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  const enviar = async () => {
    const limpio = text.trim();
    if (!limpio || sending) return;
    try {
      await onSend(limpio);
      setText("");
    } catch (e) {
      // El texto se conserva para poder reintentar.
      toast.error(errorMessage(e));
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (sugerencias.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive((a) => (a + 1) % sugerencias.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive((a) => (a - 1 + sugerencias.length) % sugerencias.length);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        const elegida = sugerencias[active];
        if (elegida) void usar(elegida);
        return;
      }
    }
    // Enter envia y Shift+Enter hace salto de linea; durante una composicion (IME) no se envia.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void enviar();
    }
  };

  return (
    <div className="relative border-t p-3">
      {sugerencias.length > 0 && (
        <ul
          role="listbox"
          aria-label={t("quickReplies")}
          className="absolute inset-x-3 bottom-full mb-1 overflow-hidden rounded-md border bg-card shadow-md"
        >
          {sugerencias.map((r, i) => (
            <li key={r.id} role="option" aria-selected={i === active}>
              <button
                type="button"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => void usar(r)}
                className={cn(
                  "flex w-full items-baseline gap-2 px-3 py-2 text-start text-sm",
                  i === active ? "bg-accent" : "hover:bg-accent/60",
                )}
              >
                <span className="font-mono text-xs text-muted-foreground">{r.shortcut}</span>
                <span className="truncate">{r.title}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex items-end gap-2">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="icon" aria-label={t("quickReplies")} disabled={!replies}>
              <Zap className="h-4 w-4" aria-hidden />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="max-h-72 w-72 overflow-y-auto">
            {replies && replies.length > 0 ? (
              replies.map((r) => (
                <DropdownMenuItem key={r.id} onSelect={() => void usar(r)} className="flex-col items-start">
                  <span className="font-medium">{r.title}</span>
                  <span className="font-mono text-xs text-muted-foreground">{r.shortcut}</span>
                </DropdownMenuItem>
              ))
            ) : (
              <p className="px-2 py-1.5 text-sm text-muted-foreground">{t("noQuickReplies")}</p>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
        <Textarea
          ref={areaRef}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setActive(0);
          }}
          onKeyDown={onKeyDown}
          rows={2}
          maxLength={MAX_CHARS}
          placeholder={t("placeholder")}
          aria-label={t("placeholder")}
          className="max-h-40 min-h-[44px] resize-none"
          disabled={sending}
        />
        <Button onClick={() => void enviar()} disabled={sending || !text.trim()} aria-label={t("send")}>
          {sending ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Send className="h-4 w-4" aria-hidden />}
        </Button>
      </div>
      <p className="mt-1.5 ps-12 text-xs text-muted-foreground">{t("hint")}</p>
    </div>
  );
}
