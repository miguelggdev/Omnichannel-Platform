import { cn } from "@/lib/utils";
import type { ConversationStatus } from "@/types";

const COLORS: Record<ConversationStatus, string> = {
  new: "bg-status-new",
  bot_active: "bg-status-bot_active",
  human_active: "bg-status-human_active",
  waiting_human: "bg-status-waiting_human",
  waiting_client: "bg-status-waiting_client",
  resolved: "bg-status-resolved",
  archived: "bg-status-archived",
};

export function StatusBadge({ status, label }: { status: ConversationStatus; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium">
      <span className={cn("h-2 w-2 rounded-full", COLORS[status])} aria-hidden />
      {label}
    </span>
  );
}
