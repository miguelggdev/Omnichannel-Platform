"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Loader2, Pencil, Plus, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";
import { EmptyState } from "@/components/common/EmptyState";
import { QueryError } from "@/components/common/QueryError";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useDeleteQuickReply, useQuickReplies, useSaveQuickReply } from "@/hooks/useQuickReplies";
import { errorMessage } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";
import type { QuickReply } from "@/types";

// SHORTCUT_PATTERN del backend: empieza por "/", luego minusculas, numeros, guion y guion bajo.
const SHORTCUT = /^\/[a-z0-9][a-z0-9_-]{0,48}$/;
const schema = z.object({
  shortcut: z.string().min(1, "required").regex(SHORTCUT, "shortcutFormat"),
  title: z.string().min(1, "required").max(200),
  content: z.string().min(1, "required"),
  category: z.string().max(100),
});
type Values = z.infer<typeof schema>;

function QuickReplyForm({ reply, onDone }: { reply: QuickReply | null; onDone: () => void }) {
  const t = useTranslations("quickReplies");
  const tc = useTranslations("common");
  const save = useSaveQuickReply();
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: {
      shortcut: reply?.shortcut ?? "",
      title: reply?.title ?? "",
      content: reply?.content ?? "",
      category: reply?.category ?? "",
    },
  });

  const submit = handleSubmit((v) =>
    save.mutate(
      { id: reply?.id, data: { ...v, category: v.category.trim() || null } },
      {
        onSuccess: () => {
          toast.success(tc("saved"));
          onDone();
        },
        onError: (e) => toast.error(errorMessage(e)),
      },
    ),
  );

  const campo = (name: keyof Values) =>
    errors[name]?.message ? (
      <p role="alert" className="text-sm text-destructive">
        {errors[name]?.message === "shortcutFormat" ? t("shortcutFormat") : tc("required")}
      </p>
    ) : null;

  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      <div className="space-y-2">
        <Label htmlFor="shortcut">{t("fields.shortcut")}</Label>
        <Input id="shortcut" placeholder="/saludo" aria-invalid={errors.shortcut ? true : undefined} {...register("shortcut")} />
        {campo("shortcut")}
      </div>
      <div className="space-y-2">
        <Label htmlFor="title">{t("fields.title")}</Label>
        <Input id="title" aria-invalid={errors.title ? true : undefined} {...register("title")} />
        {campo("title")}
      </div>
      <div className="space-y-2">
        <Label htmlFor="content">{t("fields.content")}</Label>
        <Textarea id="content" rows={5} aria-invalid={errors.content ? true : undefined} {...register("content")} />
        <p className="text-xs text-muted-foreground">{t("variablesHint")}</p>
        {campo("content")}
      </div>
      <div className="space-y-2">
        <Label htmlFor="category">{t("fields.category")}</Label>
        <Input id="category" {...register("category")} />
      </div>
      <DialogFooter>
        <Button type="submit" disabled={save.isPending}>
          {save.isPending && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
          {tc("save")}
        </Button>
      </DialogFooter>
    </form>
  );
}

export function QuickReplyManager() {
  const t = useTranslations("quickReplies");
  const tc = useTranslations("common");
  const { data, isLoading, error, refetch } = useQuickReplies();
  const canDelete = useAuthStore((s) => s.hasMinRole("admin"));
  const borrar = useDeleteQuickReply();
  const [editando, setEditando] = useState<QuickReply | null | "new">(null);
  const [aBorrar, setABorrar] = useState<QuickReply | null>(null);

  if (error) return <QueryError error={error} onRetry={() => void refetch()} />;

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <Button onClick={() => setEditando("new")}>
          <Plus className="h-4 w-4" aria-hidden />
          {t("new")}
        </Button>
      </div>

      {isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : data && data.length > 0 ? (
        <div className="grid gap-3 md:grid-cols-2">
          {data.map((r) => (
            <Card key={r.id}>
              <CardContent className="space-y-2 p-4">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="truncate font-medium">{r.title}</p>
                    <p className="font-mono text-xs text-muted-foreground">{r.shortcut}</p>
                  </div>
                  <div className="flex shrink-0">
                    <Button variant="ghost" size="icon" aria-label={tc("edit")} onClick={() => setEditando(r)}>
                      <Pencil className="h-4 w-4" aria-hidden />
                    </Button>
                    {canDelete && (
                      <Button variant="ghost" size="icon" aria-label={tc("delete")} onClick={() => setABorrar(r)}>
                        <Trash2 className="h-4 w-4 text-destructive" aria-hidden />
                      </Button>
                    )}
                  </div>
                </div>
                <p className="line-clamp-3 whitespace-pre-wrap text-sm text-muted-foreground">{r.content}</p>
                {r.category && <Badge variant="secondary">{r.category}</Badge>}
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <EmptyState title={t("empty")} description={t("emptyHint")} />
      )}

      <Dialog open={editando !== null} onOpenChange={(open) => !open && setEditando(null)}>
        <DialogContent closeLabel={tc("close")}>
          <DialogHeader>
            <DialogTitle>{editando === "new" ? t("new") : tc("edit")}</DialogTitle>
          </DialogHeader>
          {editando !== null && (
            <QuickReplyForm reply={editando === "new" ? null : editando} onDone={() => setEditando(null)} />
          )}
        </DialogContent>
      </Dialog>

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
    </div>
  );
}
