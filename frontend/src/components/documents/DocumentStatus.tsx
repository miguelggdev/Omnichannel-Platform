"use client";

import { AlertCircle, CheckCircle2, Clock, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import type { DocumentStatus as Status } from "@/types";

const ICONS = {
  pending: Clock,
  processing: Loader2,
  completed: CheckCircle2,
  failed: AlertCircle,
} as const;

export function DocumentStatus({ status }: { status: Status }) {
  const t = useTranslations("documents");
  const Icon = ICONS[status];
  return (
    <Badge variant={status === "failed" ? "destructive" : status === "completed" ? "default" : "secondary"} className="gap-1">
      <Icon className={status === "processing" ? "h-3 w-3 animate-spin" : "h-3 w-3"} aria-hidden />
      {t(`status.${status}`)}
    </Badge>
  );
}
