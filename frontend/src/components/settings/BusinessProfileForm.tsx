"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Info, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useMemo } from "react";
import { Controller, useForm, type FieldErrors } from "react-hook-form";
import { toast } from "sonner";
import { QueryError } from "@/components/common/QueryError";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { useBusinessProfile, useUpdateBusinessProfile } from "@/hooks/useBusinessProfile";
import { errorMessage } from "@/lib/api";
import { buildUpdate, profileSchema, toFormValues, type ProfileFormValues } from "@/lib/business-profile";
import { DAYS, SOCIAL_NETWORKS } from "@/types";

type ErrKey = "required" | "tooLong" | "phone" | "email" | "url" | "country" | "timezone" | "color" | "handle" | "time" | "closeAfterOpen";

function Field({
  id,
  label,
  error,
  hint,
  children,
}: {
  id: string;
  label: string;
  error?: string;
  hint?: string;
  children: React.ReactNode;
}) {
  const t = useTranslations("businessProfile.errors");
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {t(error as ErrKey)}
        </p>
      )}
    </div>
  );
}

const err = (e: FieldErrors<ProfileFormValues>, ...path: string[]): string | undefined => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let nodo: any = e;
  for (const p of path) nodo = nodo?.[p];
  return typeof nodo?.message === "string" ? nodo.message : undefined;
};

export function BusinessProfileForm() {
  const t = useTranslations("businessProfile");
  const tDay = useTranslations("businessProfile.days");
  const tc = useTranslations("common");
  const { data, isLoading, error, refetch } = useBusinessProfile();
  const update = useUpdateBusinessProfile();

  const inicial = useMemo(() => (data ? toFormValues(data) : null), [data]);
  const {
    register,
    control,
    handleSubmit,
    reset,
    watch,
    formState: { errors, isDirty },
  } = useForm<ProfileFormValues>({ resolver: zodResolver(profileSchema), values: inicial ?? undefined });

  // Tras guardar, el formulario toma los valores nuevos como punto de partida.
  useEffect(() => {
    if (inicial) reset(inicial);
  }, [inicial, reset]);

  if (error) return <QueryError error={error} onRetry={() => void refetch()} />;
  if (isLoading || !data || !inicial) return <Skeleton className="h-96 w-full" />;

  const submit = handleSubmit((values) => {
    const cambios = buildUpdate(inicial, values);
    if (Object.keys(cambios).length === 0) return;
    update.mutate(cambios, {
      onSuccess: () => toast.success(tc("saved")),
      onError: (e) => toast.error(errorMessage(e)),
    });
  });

  const primary = watch("primary_color");
  const secondary = watch("secondary_color");
  const colorOk = (c: string) => (/^#[0-9a-fA-F]{6}$/.test(c) ? c : undefined);

  return (
    <form onSubmit={submit} className="space-y-6" noValidate>
      <p className="flex items-start gap-2 rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">
        <Info className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
        {t("usageNotice")}
      </p>

      <Card>
        <CardHeader>
          <CardTitle>{t("sections.business")}</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          <Field id="business_name" label={t("fields.business_name")} error={err(errors, "business_name")}>
            <Input id="business_name" aria-invalid={errors.business_name ? true : undefined} {...register("business_name")} />
          </Field>
          <Field id="business_type" label={t("fields.business_type")} error={err(errors, "business_type")}>
            <Input id="business_type" {...register("business_type")} />
          </Field>
          <div className="sm:col-span-2">
            <Field id="description" label={t("fields.description")} error={err(errors, "description")}>
              <Textarea id="description" rows={3} {...register("description")} />
            </Field>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("sections.contact")}</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          <Field id="phone" label={t("fields.phone")} error={err(errors, "phone")}>
            <Input id="phone" type="tel" {...register("phone")} />
          </Field>
          <Field id="email" label={t("fields.email")} error={err(errors, "email")}>
            <Input id="email" type="email" {...register("email")} />
          </Field>
          <Field id="website" label={t("fields.website")} error={err(errors, "website")}>
            <Input id="website" placeholder="https://" {...register("website")} />
          </Field>
          <Field id="address" label={t("fields.address")} error={err(errors, "address")}>
            <Input id="address" {...register("address")} />
          </Field>
          <Field id="city" label={t("fields.city")} error={err(errors, "city")}>
            <Input id="city" {...register("city")} />
          </Field>
          <Field id="country" label={t("fields.country")} error={err(errors, "country")} hint={t("hints.country")}>
            <Input id="country" maxLength={2} className="uppercase" {...register("country")} />
          </Field>
          <Field id="timezone" label={t("fields.timezone")} error={err(errors, "timezone")} hint={t("hints.timezone")}>
            <Input id="timezone" placeholder="America/Bogota" {...register("timezone")} />
          </Field>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("sections.hours")}</CardTitle>
          <CardDescription>{t("hoursHint")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {DAYS.map((dia) => {
            const abierto = watch(`hours.${dia}.is_open`);
            return (
              <div key={dia} className="grid grid-cols-[7rem_auto_1fr] items-center gap-3 sm:grid-cols-[9rem_auto_1fr]">
                <span className="text-sm font-medium">{tDay(dia)}</span>
                <Controller
                  control={control}
                  name={`hours.${dia}.is_open`}
                  render={({ field }) => (
                    <Switch checked={field.value} onCheckedChange={field.onChange} aria-label={`${tDay(dia)}: ${t("open")}`} />
                  )}
                />
                {abierto ? (
                  <div className="flex flex-wrap items-center gap-2">
                    <Input type="time" aria-label={`${tDay(dia)}: ${t("opensAt")}`} className="w-28" {...register(`hours.${dia}.open_time`)} />
                    <span aria-hidden>–</span>
                    <Input type="time" aria-label={`${tDay(dia)}: ${t("closesAt")}`} className="w-28" {...register(`hours.${dia}.close_time`)} />
                    {(err(errors, "hours", dia, "close_time") || err(errors, "hours", dia, "open_time")) && (
                      <p role="alert" className="w-full text-sm text-destructive">
                        {t(`errors.${(err(errors, "hours", dia, "close_time") ?? err(errors, "hours", dia, "open_time")) as ErrKey}`)}
                      </p>
                    )}
                  </div>
                ) : (
                  <span className="text-sm text-muted-foreground">{t("closed")}</span>
                )}
              </div>
            );
          })}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("sections.brand")}</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          <Field id="primary_color" label={t("fields.primary_color")} error={err(errors, "primary_color")} hint="#1D4ED8">
            <div className="flex items-center gap-2">
              <span className="h-9 w-9 shrink-0 rounded-md border" style={{ backgroundColor: colorOk(primary) }} aria-hidden />
              <Input id="primary_color" {...register("primary_color")} />
            </div>
          </Field>
          <Field id="secondary_color" label={t("fields.secondary_color")} error={err(errors, "secondary_color")} hint="#64748B">
            <div className="flex items-center gap-2">
              <span className="h-9 w-9 shrink-0 rounded-md border" style={{ backgroundColor: colorOk(secondary) }} aria-hidden />
              <Input id="secondary_color" {...register("secondary_color")} />
            </div>
          </Field>
          <div className="sm:col-span-2">
            <Field id="logo_url" label={t("fields.logo_url")} error={err(errors, "logo_url")} hint={t("hints.logo")}>
              <Input id="logo_url" placeholder="https://" {...register("logo_url")} />
            </Field>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("sections.social")}</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          {SOCIAL_NETWORKS.map((red) => (
            <Field key={red} id={`social_${red}`} label={t(`social.${red}`)} error={err(errors, "social", red)}>
              <Input id={`social_${red}`} {...register(`social.${red}`)} />
            </Field>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("sections.assistant")}</CardTitle>
          <CardDescription>{data.has_agent ? t("assistantHint") : t("noAgent")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <Field id="welcome_message" label={t("fields.welcome_message")} error={err(errors, "welcome_message")}>
            <Textarea id="welcome_message" rows={3} disabled={!data.has_agent} {...register("welcome_message")} />
          </Field>
          <Field id="handoff_message" label={t("fields.handoff_message")} error={err(errors, "handoff_message")}>
            <Textarea id="handoff_message" rows={3} disabled={!data.has_agent} {...register("handoff_message")} />
          </Field>
        </CardContent>
      </Card>

      <div className="sticky bottom-0 flex justify-end gap-2 border-t bg-background/95 py-3 backdrop-blur">
        <Button type="button" variant="outline" disabled={!isDirty || update.isPending} onClick={() => reset(inicial)}>
          {t("discard")}
        </Button>
        <Button type="submit" disabled={!isDirty || update.isPending}>
          {update.isPending && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
          {tc("save")}
        </Button>
      </div>
    </form>
  );
}
