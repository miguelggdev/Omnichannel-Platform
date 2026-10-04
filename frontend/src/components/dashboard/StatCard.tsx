import { TrendingDown, TrendingUp } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

interface StatCardProps {
  label: string;
  value: string | undefined;
  icon: React.ComponentType<{ className?: string }>;
  hint?: string;
  /** Variacion en %; `null` o ausente = sin dato con el que comparar. */
  trend?: number | null;
  /** Texto accesible de la tendencia (el color y la flecha solos no lo dicen). */
  trendLabel?: string;
  footer?: React.ReactNode;
}

export function StatCard({ label, value, icon: Icon, hint, trend, trendLabel, footer }: StatCardProps) {
  return (
    <Card>
      <CardContent className="p-6">
        <div className="flex items-center gap-4">
          <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-primary/10">
            <Icon className="h-5 w-5 text-primary" />
          </div>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm text-muted-foreground">{label}</p>
            {value === undefined ? (
              <Skeleton className="mt-1 h-7 w-16" />
            ) : (
              <p className="text-2xl font-semibold tabular-nums">{value}</p>
            )}
            {hint && <p className="truncate text-xs text-muted-foreground">{hint}</p>}
            {trend !== null && trend !== undefined && (
              <p
                className={cn(
                  "mt-0.5 flex items-center gap-1 text-xs font-medium",
                  trend >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400",
                )}
              >
                {trend >= 0 ? <TrendingUp className="h-3 w-3" aria-hidden /> : <TrendingDown className="h-3 w-3" aria-hidden />}
                <span>
                  {trend > 0 ? "+" : ""}
                  {trend}%{trendLabel ? ` ${trendLabel}` : ""}
                </span>
              </p>
            )}
          </div>
        </div>
        {footer}
      </CardContent>
    </Card>
  );
}
