"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Loader2, Pencil, Plus, Search, UserCheck, UserX } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";
import { EmptyState } from "@/components/common/EmptyState";
import { Pagination } from "@/components/common/Pagination";
import { QueryError } from "@/components/common/QueryError";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useAppLocale } from "@/hooks/useAppLocale";
import {
  useCreateUser,
  useUpdateUser,
  useUsers,
  type UserCreateInput,
  type UserUpdateInput,
} from "@/hooks/useUsers";
import { errorMessage } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { useAuthStore } from "@/stores/authStore";
import { ASSIGNABLE_ROLES, type AssignableRole, type TeamUser } from "@/types";

const TODOS = "all";

// Espejo de UserCreate/UserUpdate del backend.
const nombre = z.string().trim().min(1, "required").max(100);
const createSchema = z.object({
  email: z.string().email("invalidEmail"),
  password: z.string().min(8, "passwordMin").max(72, "passwordMax"),
  first_name: nombre,
  last_name: nombre,
  role: z.enum(ASSIGNABLE_ROLES),
});
const editSchema = z.object({
  first_name: nombre,
  last_name: nombre,
  role: z.enum(ASSIGNABLE_ROLES),
  password: z.union([z.literal(""), z.string().min(8, "passwordMin").max(72, "passwordMax")]),
});

type FieldKey = "required" | "invalidEmail" | "passwordMin" | "passwordMax";

function RoleSelect({
  id,
  value,
  onChange,
  disabled,
}: {
  id: string;
  value: AssignableRole;
  onChange: (v: AssignableRole) => void;
  disabled?: boolean;
}) {
  const tRole = useTranslations("roles");
  return (
    <Select value={value} onValueChange={(v) => onChange(v as AssignableRole)} disabled={disabled}>
      <SelectTrigger id={id}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {ASSIGNABLE_ROLES.map((r) => (
          <SelectItem key={r} value={r}>
            {tRole(r)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function Err({ code }: { code: string | undefined }) {
  const t = useTranslations("team.errors");
  if (!code) return null;
  return (
    <p role="alert" className="text-sm text-destructive">
      {t(code as FieldKey)}
    </p>
  );
}

function CreateForm({ onDone }: { onDone: () => void }) {
  const t = useTranslations("team");
  const tc = useTranslations("common");
  const create = useCreateUser();
  const {
    register,
    control,
    handleSubmit,
    formState: { errors },
  } = useForm<UserCreateInput>({
    resolver: zodResolver(createSchema),
    defaultValues: { email: "", password: "", first_name: "", last_name: "", role: "agent" },
  });

  const submit = handleSubmit((values) =>
    create.mutate(values, {
      onSuccess: () => {
        toast.success(t("created"));
        onDone();
      },
      onError: (e) => toast.error(errorMessage(e)),
    }),
  );

  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="first_name">{t("fields.first_name")}</Label>
          <Input id="first_name" aria-invalid={errors.first_name ? true : undefined} {...register("first_name")} />
          <Err code={errors.first_name?.message} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="last_name">{t("fields.last_name")}</Label>
          <Input id="last_name" aria-invalid={errors.last_name ? true : undefined} {...register("last_name")} />
          <Err code={errors.last_name?.message} />
        </div>
      </div>
      <div className="space-y-2">
        <Label htmlFor="email">{t("fields.email")}</Label>
        <Input id="email" type="email" autoComplete="off" aria-invalid={errors.email ? true : undefined} {...register("email")} />
        <Err code={errors.email?.message} />
      </div>
      <div className="space-y-2">
        <Label htmlFor="password">{t("fields.initialPassword")}</Label>
        <Input id="password" type="password" autoComplete="new-password" aria-invalid={errors.password ? true : undefined} {...register("password")} />
        <Err code={errors.password?.message} />
      </div>
      <div className="space-y-2">
        <Label htmlFor="role">{t("fields.role")}</Label>
        <Controller control={control} name="role" render={({ field }) => <RoleSelect id="role" value={field.value} onChange={field.onChange} />} />
      </div>
      <DialogFooter>
        <Button type="submit" disabled={create.isPending}>
          {create.isPending && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
          {tc("save")}
        </Button>
      </DialogFooter>
    </form>
  );
}

function EditForm({ target, isSelf, onDone }: { target: TeamUser; isSelf: boolean; onDone: () => void }) {
  const t = useTranslations("team");
  const tc = useTranslations("common");
  const update = useUpdateUser();
  const initialRole = (ASSIGNABLE_ROLES as readonly string[]).includes(target.role) ? (target.role as AssignableRole) : "agent";
  const {
    register,
    control,
    handleSubmit,
    formState: { errors },
  } = useForm<{ first_name: string; last_name: string; role: AssignableRole; password: string }>({
    resolver: zodResolver(editSchema),
    defaultValues: { first_name: target.first_name, last_name: target.last_name, role: initialRole, password: "" },
  });

  const submit = handleSubmit((values) => {
    // Solo viaja lo que cambio: el backend no toca los campos omitidos, y no hay que
    // reenviar el rol de uno mismo (no puede cambiarlo).
    const data: UserUpdateInput = {};
    if (values.first_name !== target.first_name) data.first_name = values.first_name;
    if (values.last_name !== target.last_name) data.last_name = values.last_name;
    if (values.role !== initialRole) data.role = values.role;
    if (values.password) data.password = values.password;
    if (Object.keys(data).length === 0) {
      onDone();
      return;
    }
    update.mutate(
      { id: target.id, data },
      {
        onSuccess: () => {
          toast.success(tc("saved"));
          onDone();
        },
        onError: (e) => toast.error(errorMessage(e)),
      },
    );
  });

  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      <p className="text-sm text-muted-foreground">{target.email}</p>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="e_first_name">{t("fields.first_name")}</Label>
          <Input id="e_first_name" aria-invalid={errors.first_name ? true : undefined} {...register("first_name")} />
          <Err code={errors.first_name?.message} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="e_last_name">{t("fields.last_name")}</Label>
          <Input id="e_last_name" aria-invalid={errors.last_name ? true : undefined} {...register("last_name")} />
          <Err code={errors.last_name?.message} />
        </div>
      </div>
      <div className="space-y-2">
        <Label htmlFor="e_role">{t("fields.role")}</Label>
        <Controller control={control} name="role" render={({ field }) => <RoleSelect id="e_role" value={field.value} onChange={field.onChange} disabled={isSelf} />} />
        {isSelf && <p className="text-xs text-muted-foreground">{t("selfRoleHint")}</p>}
      </div>
      <div className="space-y-2">
        <Label htmlFor="e_password">{t("fields.newPassword")}</Label>
        <Input id="e_password" type="password" autoComplete="new-password" aria-invalid={errors.password ? true : undefined} {...register("password")} />
        <p className="text-xs text-muted-foreground">{t("newPasswordHint")}</p>
        <Err code={errors.password?.message} />
      </div>
      <DialogFooter>
        <Button type="submit" disabled={update.isPending}>
          {update.isPending && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
          {tc("save")}
        </Button>
      </DialogFooter>
    </form>
  );
}

export function TeamManager() {
  const t = useTranslations("team");
  const tc = useTranslations("common");
  const tRole = useTranslations("roles");
  const locale = useAppLocale();
  const me = useAuthStore((s) => s.user);
  const isSuperAdmin = me?.role === "super_admin";
  const [input, setInput] = useState("");
  const [search, setSearch] = useState("");
  const [role, setRole] = useState<string>(TODOS);
  const [state, setState] = useState<string>(TODOS);
  const [page, setPage] = useState(1);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<TeamUser | null>(null);
  const [toggling, setToggling] = useState<TeamUser | null>(null);
  const update = useUpdateUser();

  useEffect(() => {
    const id = setTimeout(() => {
      setSearch(input.trim());
      setPage(1);
    }, 300);
    return () => clearTimeout(id);
  }, [input]);

  const { data, isLoading, error, refetch } = useUsers({
    search: search || undefined,
    role: role === TODOS ? undefined : (role as AssignableRole),
    is_active: state === TODOS ? undefined : state === "active",
    page,
  });

  /** Un super_admin es de la plataforma: solo otro super_admin puede tocarlo. */
  const canTouch = (u: TeamUser) => u.role !== "super_admin" || isSuperAdmin;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-1 flex-wrap gap-3">
          <div className="relative w-full sm:max-w-xs">
            <Search className="pointer-events-none absolute start-3 top-3 h-4 w-4 text-muted-foreground" aria-hidden />
            <Input value={input} onChange={(e) => setInput(e.target.value)} placeholder={t("searchPlaceholder")} aria-label={t("search")} className="ps-9" />
          </div>
          <div className="w-full sm:w-44">
            <Select value={role} onValueChange={(v) => { setRole(v); setPage(1); }}>
              <SelectTrigger aria-label={t("filterRole")}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={TODOS}>{t("allRoles")}</SelectItem>
                {ASSIGNABLE_ROLES.map((r) => (
                  <SelectItem key={r} value={r}>
                    {tRole(r)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="w-full sm:w-40">
            <Select value={state} onValueChange={(v) => { setState(v); setPage(1); }}>
              <SelectTrigger aria-label={t("filterState")}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={TODOS}>{t("allStates")}</SelectItem>
                <SelectItem value="active">{t("active")}</SelectItem>
                <SelectItem value="inactive">{t("inactive")}</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
        <Button onClick={() => setCreating(true)}>
          <Plus className="h-4 w-4" aria-hidden />
          {t("new")}
        </Button>
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
                  <TableHead>{t("columns.user")}</TableHead>
                  <TableHead>{t("columns.role")}</TableHead>
                  <TableHead>{t("columns.state")}</TableHead>
                  <TableHead className="hidden lg:table-cell">{t("columns.lastLogin")}</TableHead>
                  <TableHead className="w-28" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.items.map((u) => {
                  const self = u.id === me?.id;
                  return (
                    <TableRow key={u.id}>
                      <TableCell>
                        <p className="font-medium">
                          {u.first_name} {u.last_name}
                          {self && <span className="ms-2 text-xs text-muted-foreground">({t("you")})</span>}
                        </p>
                        <p className="text-xs text-muted-foreground">{u.email}</p>
                      </TableCell>
                      <TableCell>
                        <Badge variant="secondary">{tRole(u.role)}</Badge>
                      </TableCell>
                      <TableCell>
                        <Badge variant={u.is_active ? "default" : "outline"}>{u.is_active ? t("active") : t("inactive")}</Badge>
                      </TableCell>
                      <TableCell className="hidden text-muted-foreground lg:table-cell">
                        {u.last_login_at ? formatDateTime(u.last_login_at, locale) : t("never")}
                      </TableCell>
                      <TableCell>
                        {canTouch(u) && (
                          <div className="flex justify-end gap-1">
                            <Button variant="ghost" size="icon" aria-label={t("editUser", { name: u.first_name })} onClick={() => setEditing(u)}>
                              <Pencil className="h-4 w-4" aria-hidden />
                            </Button>
                            {!self && (
                              <Button
                                variant="ghost"
                                size="icon"
                                aria-label={u.is_active ? t("deactivateUser", { name: u.first_name }) : t("reactivateUser", { name: u.first_name })}
                                onClick={() => setToggling(u)}
                              >
                                {u.is_active ? <UserX className="h-4 w-4 text-destructive" aria-hidden /> : <UserCheck className="h-4 w-4" aria-hidden />}
                              </Button>
                            )}
                          </div>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
          <Pagination page={page} pageSize={data.page_size} total={data.total} onPageChange={setPage} />
        </>
      ) : (
        <EmptyState title={t("empty")} description={search ? t("emptySearch") : undefined} />
      )}

      <Dialog open={creating} onOpenChange={setCreating}>
        <DialogContent closeLabel={tc("close")}>
          <DialogHeader>
            <DialogTitle>{t("new")}</DialogTitle>
          </DialogHeader>
          <CreateForm onDone={() => setCreating(false)} />
        </DialogContent>
      </Dialog>

      <Dialog open={editing !== null} onOpenChange={(open) => !open && setEditing(null)}>
        <DialogContent closeLabel={tc("close")}>
          <DialogHeader>
            <DialogTitle>{tc("edit")}</DialogTitle>
          </DialogHeader>
          {editing && <EditForm key={editing.id} target={editing} isSelf={editing.id === me?.id} onDone={() => setEditing(null)} />}
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={toggling !== null}
        onOpenChange={(open) => !open && setToggling(null)}
        title={toggling?.is_active ? t("deactivateTitle") : t("reactivateTitle")}
        description={toggling?.is_active ? t("deactivateBody", { name: toggling.first_name }) : t("reactivateBody", { name: toggling?.first_name ?? "" })}
        confirmLabel={toggling?.is_active ? t("deactivate") : t("reactivate")}
        destructive={toggling?.is_active ?? false}
        pending={update.isPending}
        onConfirm={() =>
          toggling &&
          update.mutate(
            { id: toggling.id, data: { is_active: !toggling.is_active } },
            {
              onSuccess: () => {
                toast.success(toggling.is_active ? t("deactivated") : t("reactivated"));
                setToggling(null);
              },
              onError: (e) => {
                toast.error(errorMessage(e));
                setToggling(null);
              },
            },
          )
        }
      />
    </div>
  );
}
