"use client";

import { Loader2 } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAppLocale } from "@/hooks/useAppLocale";
import { useOnboarding } from "@/hooks/useOnboarding";
import {
  COUNTRIES,
  accountStepSchema,
  businessStepSchema,
  termsSchema,
} from "@/lib/onboarding";
import { BUSINESS_TYPES, type BusinessType } from "@/types";

type ErrKey =
  | "required"
  | "businessName"
  | "fullName"
  | "email"
  | "passwordMin"
  | "passwordMax"
  | "passwordStrength"
  | "passwordMismatch"
  | "terms";

interface Values {
  businessName: string;
  businessType: BusinessType;
  country: string;
  fullName: string;
  email: string;
  password: string;
  confirmPassword: string;
  terms: boolean;
}

const INITIAL: Values = {
  businessName: "",
  businessType: "other",
  country: "CO",
  fullName: "",
  email: "",
  password: "",
  confirmPassword: "",
  terms: false,
};

const STEPS = ["business", "account", "confirm"] as const;
const SELECT_CLASS =
  "flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

function Field({
  id,
  label,
  error,
  hint,
  children,
}: {
  id: string;
  label: string;
  error?: ErrKey;
  hint?: string;
  children: React.ReactNode;
}) {
  const t = useTranslations("onboarding.errors");
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint && !error && <p className="text-xs text-muted-foreground">{hint}</p>}
      {error && (
        <p id={`${id}-error`} role="alert" className="text-sm text-destructive">
          {t(error)}
        </p>
      )}
    </div>
  );
}

export function OnboardingWizard() {
  const t = useTranslations("onboarding");
  const locale = useAppLocale();
  const { register, pending, failure } = useOnboarding();
  const [step, setStep] = useState(0);
  const [values, setValues] = useState<Values>(INITIAL);
  const [errors, setErrors] = useState<Partial<Record<keyof Values, ErrKey>>>({});

  const countryNames = useMemo(() => {
    const names = new Intl.DisplayNames([locale], { type: "region" });
    return COUNTRIES.map((code) => ({ code, name: names.of(code) ?? code })).sort((a, b) =>
      a.name.localeCompare(b.name, locale),
    );
  }, [locale]);

  const set = <K extends keyof Values>(key: K, value: Values[K]) => {
    setValues((v) => ({ ...v, [key]: value }));
    setErrors((e) => ({ ...e, [key]: undefined }));
  };

  /** Valida el paso actual; devuelve `true` si se puede avanzar. */
  function validar(): boolean {
    const schema = [businessStepSchema, accountStepSchema, termsSchema][step]!;
    const resultado = schema.safeParse(values);
    if (resultado.success) {
      setErrors({});
      return true;
    }
    const nuevos: Partial<Record<keyof Values, ErrKey>> = {};
    for (const issue of resultado.error.issues) {
      const campo = issue.path[0] as keyof Values;
      nuevos[campo] ??= issue.message as ErrKey;
    }
    setErrors(nuevos);
    return false;
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!validar()) return;
    if (step < STEPS.length - 1) {
      setStep(step + 1);
      return;
    }
    await register({
      business_name: values.businessName.trim(),
      business_type: values.businessType,
      admin_full_name: values.fullName.trim(),
      admin_email: values.email.trim(),
      admin_password: values.password,
      country: values.country,
      language: locale,
      terms_accepted: true,
    });
  }

  const invalid = (k: keyof Values) => (errors[k] ? true : undefined);
  const describedBy = (k: keyof Values) => (errors[k] ? `${k}-error` : undefined);

  return (
    <form onSubmit={onSubmit} className="space-y-5" noValidate>
      <nav aria-label={t("stepOf", { current: step + 1, total: STEPS.length })}>
        <ol className="flex gap-2">
          {STEPS.map((s, i) => (
            <li
              key={s}
              aria-current={i === step ? "step" : undefined}
              className={`flex-1 border-t-4 pt-1 text-xs ${
                i <= step ? "border-primary text-foreground" : "border-muted text-muted-foreground"
              }`}
            >
              {t(`steps.${s}`)}
            </li>
          ))}
        </ol>
        <p className="mt-2 text-xs text-muted-foreground">
          {t("stepOf", { current: step + 1, total: STEPS.length })}
        </p>
      </nav>

      {step === 0 && (
        <div className="space-y-4">
          <Field id="businessName" label={t("fields.businessName")} error={errors.businessName}>
            <Input
              id="businessName"
              autoComplete="organization"
              aria-invalid={invalid("businessName")}
              aria-describedby={describedBy("businessName")}
              value={values.businessName}
              onChange={(e) => set("businessName", e.target.value)}
            />
          </Field>
          <Field id="businessType" label={t("fields.businessType")} error={errors.businessType}>
            <select
              id="businessType"
              className={SELECT_CLASS}
              value={values.businessType}
              onChange={(e) => set("businessType", e.target.value as BusinessType)}
            >
              {BUSINESS_TYPES.map((b) => (
                <option key={b} value={b}>
                  {t(`types.${b}`)}
                </option>
              ))}
            </select>
          </Field>
          <Field id="country" label={t("fields.country")} error={errors.country}>
            <select
              id="country"
              className={SELECT_CLASS}
              value={values.country}
              onChange={(e) => set("country", e.target.value)}
            >
              {countryNames.map((c) => (
                <option key={c.code} value={c.code}>
                  {c.name}
                </option>
              ))}
            </select>
          </Field>
        </div>
      )}

      {step === 1 && (
        <div className="space-y-4">
          <Field id="fullName" label={t("fields.fullName")} error={errors.fullName}>
            <Input
              id="fullName"
              autoComplete="name"
              aria-invalid={invalid("fullName")}
              aria-describedby={describedBy("fullName")}
              value={values.fullName}
              onChange={(e) => set("fullName", e.target.value)}
            />
          </Field>
          <Field id="email" label={t("fields.email")} error={errors.email}>
            <Input
              id="email"
              type="email"
              autoComplete="email"
              aria-invalid={invalid("email")}
              aria-describedby={describedBy("email")}
              value={values.email}
              onChange={(e) => set("email", e.target.value)}
            />
          </Field>
          <Field
            id="password"
            label={t("fields.password")}
            error={errors.password}
            hint={t("hints.password")}
          >
            <Input
              id="password"
              type="password"
              autoComplete="new-password"
              aria-invalid={invalid("password")}
              aria-describedby={describedBy("password")}
              value={values.password}
              onChange={(e) => set("password", e.target.value)}
            />
          </Field>
          <Field id="confirmPassword" label={t("fields.confirmPassword")} error={errors.confirmPassword}>
            <Input
              id="confirmPassword"
              type="password"
              autoComplete="new-password"
              aria-invalid={invalid("confirmPassword")}
              aria-describedby={describedBy("confirmPassword")}
              value={values.confirmPassword}
              onChange={(e) => set("confirmPassword", e.target.value)}
            />
          </Field>
        </div>
      )}

      {step === 2 && (
        <div className="space-y-4">
          <h2 className="text-sm font-medium">{t("review")}</h2>
          <dl className="grid grid-cols-[auto,1fr] gap-x-4 gap-y-1 rounded-md border p-3 text-sm">
            <dt className="text-muted-foreground">{t("fields.businessName")}</dt>
            <dd className="break-words">{values.businessName}</dd>
            <dt className="text-muted-foreground">{t("fields.businessType")}</dt>
            <dd>{t(`types.${values.businessType}`)}</dd>
            <dt className="text-muted-foreground">{t("fields.fullName")}</dt>
            <dd className="break-words">{values.fullName}</dd>
            <dt className="text-muted-foreground">{t("fields.email")}</dt>
            <dd className="break-all">{values.email}</dd>
          </dl>
          <div className="space-y-2">
            <div className="flex items-start gap-2">
              <input
                id="terms"
                type="checkbox"
                className="mt-1 h-4 w-4"
                checked={values.terms}
                aria-invalid={invalid("terms")}
                aria-describedby={describedBy("terms")}
                onChange={(e) => set("terms", e.target.checked)}
              />
              <Label htmlFor="terms" className="font-normal">
                {t("terms")}
              </Label>
            </div>
            {errors.terms && (
              <p id="terms-error" role="alert" className="text-sm text-destructive">
                {t("errors.terms")}
              </p>
            )}
          </div>
        </div>
      )}

      {failure && (
        <p role="alert" className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
          {failure.kind === "other" ? failure.message : t(`serverErrors.${failure.kind}`)}
        </p>
      )}

      <div className="flex justify-between gap-2">
        {step > 0 ? (
          <Button type="button" variant="outline" disabled={pending} onClick={() => setStep(step - 1)}>
            {t("back")}
          </Button>
        ) : (
          <span />
        )}
        <Button type="submit" disabled={pending}>
          {pending && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
          {step < STEPS.length - 1 ? t("next") : pending ? t("creating") : t("submit")}
        </Button>
      </div>

      <p className="text-center text-sm text-muted-foreground">
        {t("haveAccount")}{" "}
        <Link href="/login" className="font-medium text-primary underline-offset-4 hover:underline">
          {t("signIn")}
        </Link>
      </p>
    </form>
  );
}
