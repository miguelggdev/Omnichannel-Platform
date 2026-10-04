"use client";

import { useLocale } from "next-intl";
import { LOCALES, type Locale } from "@/lib/constants";

/** `useLocale()` tipado: next-intl lo devuelve como `string`. */
export function useAppLocale(): Locale {
  const locale = useLocale();
  return (LOCALES as readonly string[]).includes(locale) ? (locale as Locale) : "es";
}
