"use client";

import { Plus, Search } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useEffect, useState } from "react";
import { EmptyState } from "@/components/common/EmptyState";
import { Pagination } from "@/components/common/Pagination";
import { QueryError } from "@/components/common/QueryError";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useAppLocale } from "@/hooks/useAppLocale";
import { useContacts, useCreateContact } from "@/hooks/useContacts";
import { formatDateTime } from "@/lib/format";
import { contactName } from "@/lib/utils";
import { ContactForm } from "./ContactForm";

export function ContactList() {
  const t = useTranslations("contacts");
  const tc = useTranslations("common");
  const locale = useAppLocale();
  const [input, setInput] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [creating, setCreating] = useState(false);
  const create = useCreateContact();

  // Se busca 300 ms despues de la ultima tecla, no en cada una.
  useEffect(() => {
    const id = setTimeout(() => {
      setSearch(input.trim());
      setPage(1);
    }, 300);
    return () => clearTimeout(id);
  }, [input]);

  const { data, isLoading, error, refetch } = useContacts({ search: search || undefined, page });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="relative w-full sm:max-w-sm">
          <Search className="pointer-events-none absolute start-3 top-3 h-4 w-4 text-muted-foreground" aria-hidden />
          <Input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={t("searchPlaceholder")}
            aria-label={t("search")}
            className="ps-9"
          />
        </div>
        <Dialog open={creating} onOpenChange={setCreating}>
          <DialogTrigger asChild>
            <Button>
              <Plus className="h-4 w-4" aria-hidden />
              {t("new")}
            </Button>
          </DialogTrigger>
          <DialogContent closeLabel={tc("close")}>
            <DialogHeader>
              <DialogTitle>{t("new")}</DialogTitle>
            </DialogHeader>
            <ContactForm onSubmit={(d) => create.mutateAsync(d)} onDone={() => setCreating(false)} />
          </DialogContent>
        </Dialog>
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
                  <TableHead>{t("fields.name")}</TableHead>
                  <TableHead className="hidden sm:table-cell">{t("createdAt")}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.items.map((c) => (
                  <TableRow key={c.id}>
                    <TableCell>
                      <Link href={`/contacts/${c.id}`} className="font-medium hover:underline">
                        {contactName(c)}
                      </Link>
                    </TableCell>
                    <TableCell className="hidden text-muted-foreground sm:table-cell">
                      {formatDateTime(c.created_at, locale)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          <Pagination page={page} pageSize={data.page_size} total={data.total} onPageChange={setPage} />
        </>
      ) : (
        <EmptyState title={t("empty")} description={search ? t("emptySearch") : t("emptyHint")} />
      )}
    </div>
  );
}
