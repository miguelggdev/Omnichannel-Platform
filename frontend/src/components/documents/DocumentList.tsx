"use client";

import { RefreshCw, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";
import { EmptyState } from "@/components/common/EmptyState";
import { Pagination } from "@/components/common/Pagination";
import { QueryError } from "@/components/common/QueryError";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useAppLocale } from "@/hooks/useAppLocale";
import { useDeleteDocument, useDocuments, useReprocessDocument } from "@/hooks/useDocuments";
import { errorMessage } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { formatBytes } from "@/lib/utils";
import { useAuthStore } from "@/stores/authStore";
import type { KbDocument } from "@/types";
import { DocumentStatus } from "./DocumentStatus";

export function DocumentList() {
  const t = useTranslations("documents");
  const tc = useTranslations("common");
  const locale = useAppLocale();
  const [page, setPage] = useState(1);
  const [aBorrar, setABorrar] = useState<KbDocument | null>(null);
  const canManage = useAuthStore((s) => s.hasMinRole("admin"));
  const { data, isLoading, error, refetch } = useDocuments({ page });
  const borrar = useDeleteDocument();
  const reprocesar = useReprocessDocument();

  if (error) return <QueryError error={error} onRetry={() => void refetch()} />;
  if (isLoading) return <Skeleton className="h-64 w-full" />;
  if (!data || data.items.length === 0) return <EmptyState title={t("empty")} description={t("emptyHint")} />;

  return (
    <>
      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t("columns.title")}</TableHead>
              <TableHead>{t("columns.status")}</TableHead>
              <TableHead className="hidden md:table-cell">{t("columns.size")}</TableHead>
              <TableHead className="hidden md:table-cell">{t("columns.chunks")}</TableHead>
              <TableHead className="hidden lg:table-cell">{t("columns.created")}</TableHead>
              {canManage && <TableHead className="w-24" />}
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.items.map((d) => (
              <TableRow key={d.id}>
                <TableCell className="max-w-[16rem] truncate font-medium">{d.title}</TableCell>
                <TableCell>
                  <DocumentStatus status={d.status} />
                </TableCell>
                <TableCell className="hidden md:table-cell">{formatBytes(d.file_size)}</TableCell>
                <TableCell className="hidden md:table-cell">{d.chunk_count}</TableCell>
                <TableCell className="hidden lg:table-cell">{formatDateTime(d.created_at, locale)}</TableCell>
                {canManage && (
                  <TableCell>
                    <div className="flex justify-end gap-1">
                      {d.status === "failed" && (
                        <Button
                          variant="ghost"
                          size="icon"
                          aria-label={t("reprocess")}
                          disabled={reprocesar.isPending}
                          onClick={() =>
                            reprocesar.mutate(d.id, {
                              onSuccess: () => toast.success(t("reprocessing")),
                              onError: (e) => toast.error(errorMessage(e)),
                            })
                          }
                        >
                          <RefreshCw className="h-4 w-4" aria-hidden />
                        </Button>
                      )}
                      <Button variant="ghost" size="icon" aria-label={tc("delete")} onClick={() => setABorrar(d)}>
                        <Trash2 className="h-4 w-4 text-destructive" aria-hidden />
                      </Button>
                    </div>
                  </TableCell>
                )}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      <Pagination page={page} pageSize={data.page_size} total={data.total} onPageChange={setPage} />

      <ConfirmDialog
        open={aBorrar !== null}
        onOpenChange={(open) => !open && setABorrar(null)}
        title={t("deleteTitle")}
        description={t("deleteBody", { title: aBorrar?.title ?? "" })}
        confirmLabel={tc("delete")}
        destructive
        pending={borrar.isPending}
        onConfirm={() =>
          aBorrar &&
          borrar.mutate(aBorrar.id, {
            onSuccess: () => {
              toast.success(t("deleted"));
              setABorrar(null);
            },
            onError: (e) => toast.error(errorMessage(e)),
          })
        }
      />
    </>
  );
}
