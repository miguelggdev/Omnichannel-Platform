"use client";

import { Search } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";
import { EmptyState } from "@/components/common/EmptyState";
import { Pagination } from "@/components/common/Pagination";
import { QueryError } from "@/components/common/QueryError";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useAppLocale } from "@/hooks/useAppLocale";
import { useClients } from "@/hooks/usePlatform";
import { formatNumber, timeAgo } from "@/lib/format";

type Estado = "all" | "active" | "inactive";

export function ClientList() {
  const t = useTranslations("platform.clients");
  const locale = useAppLocale();
  const [input, setInput] = useState("");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<Estado>("all");
  const [page, setPage] = useState(1);

  useEffect(() => {
    const id = setTimeout(() => {
      setSearch(input.trim());
      setPage(1);
    }, 300);
    return () => clearTimeout(id);
  }, [input]);

  const { data, isLoading, error, refetch } = useClients({ search: search || undefined, status, page });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-3">
        <div className="relative w-full sm:max-w-xs">
          <Search className="pointer-events-none absolute start-3 top-3 h-4 w-4 text-muted-foreground" aria-hidden />
          <Input value={input} onChange={(e) => setInput(e.target.value)} placeholder={t("searchPlaceholder")} aria-label={t("search")} className="ps-9" />
        </div>
        <div className="w-full sm:w-44">
          <Select value={status} onValueChange={(v) => { setStatus(v as Estado); setPage(1); }}>
            <SelectTrigger aria-label={t("filterState")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t("allStates")}</SelectItem>
              <SelectItem value="active">{t("active")}</SelectItem>
              <SelectItem value="inactive">{t("suspended")}</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      {error ? (
        <QueryError error={error} onRetry={() => void refetch()} />
      ) : isLoading ? (
        <Skeleton className="h-64 w-full" />
      ) : data && data.items.length > 0 ? (
        <>
          <div className="rounded-lg border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("columns.client")}</TableHead>
                  <TableHead>{t("columns.state")}</TableHead>
                  <TableHead className="hidden md:table-cell">{t("columns.plan")}</TableHead>
                  <TableHead className="hidden text-end sm:table-cell">{t("columns.users")}</TableHead>
                  <TableHead className="hidden text-end lg:table-cell">{t("columns.conversations")}</TableHead>
                  <TableHead className="hidden text-end lg:table-cell">{t("columns.messages")}</TableHead>
                  <TableHead className="hidden xl:table-cell">{t("columns.lastActivity")}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.items.map((c) => (
                  <TableRow key={c.id}>
                    <TableCell>
                      <Link href={`/platform/clients/${c.id}`} className="font-medium hover:underline">
                        {c.name}
                      </Link>
                      <p className="font-mono text-xs text-muted-foreground">{c.slug}</p>
                    </TableCell>
                    <TableCell>
                      <Badge variant={c.is_active ? "default" : "destructive"}>{c.is_active ? t("active") : t("suspended")}</Badge>
                    </TableCell>
                    <TableCell className="hidden capitalize md:table-cell">{c.plan}</TableCell>
                    <TableCell className="hidden text-end tabular-nums sm:table-cell">{formatNumber(c.users_count, locale)}</TableCell>
                    <TableCell className="hidden text-end tabular-nums lg:table-cell">{formatNumber(c.conversations_30d, locale)}</TableCell>
                    <TableCell className="hidden text-end tabular-nums lg:table-cell">{formatNumber(c.messages_30d, locale)}</TableCell>
                    <TableCell className="hidden text-muted-foreground xl:table-cell">
                      {c.last_message_at ? timeAgo(c.last_message_at, locale) : t("never")}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          <Pagination page={page} pageSize={data.page_size} total={data.total} onPageChange={setPage} />
        </>
      ) : (
        <EmptyState title={t("empty")} />
      )}
    </div>
  );
}
