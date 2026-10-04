import { cn } from "@/lib/utils";

const COLORS: Record<string, string> = {
  whatsapp: "bg-channel-whatsapp",
  instagram: "bg-channel-instagram",
  facebook: "bg-channel-facebook",
  telegram: "bg-channel-telegram",
  email: "bg-channel-email",
  webchat: "bg-channel-webchat",
  voice: "bg-channel-voice",
  sandbox: "bg-channel-sandbox",
};

/** Canal con su color; el texto va siempre, el color solo acompana. */
export function ChannelBadge({ channel, label }: { channel: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-medium">
      <span className={cn("h-2 w-2 rounded-full", COLORS[channel] ?? "bg-muted-foreground")} aria-hidden />
      {label}
    </span>
  );
}
