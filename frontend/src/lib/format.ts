import { formatDistanceToNow, format } from "date-fns";
import { de, enUS, es, fr, it, pt } from "date-fns/locale";
import type { Locale as DateFnsLocale } from "date-fns";
import type { Locale } from "@/lib/constants";

const DATE_LOCALES: Record<Locale, DateFnsLocale> = { es, en: enUS, pt, it, de, fr };

/** "hace 5 minutos" en el idioma de la interfaz. */
export function timeAgo(iso: string | null | undefined, locale: Locale): string {
  if (!iso) return "—";
  return formatDistanceToNow(new Date(iso), { addSuffix: true, locale: DATE_LOCALES[locale] });
}

export function formatDateTime(iso: string | null | undefined, locale: Locale): string {
  if (!iso) return "—";
  return format(new Date(iso), "PPp", { locale: DATE_LOCALES[locale] });
}

export function formatTime(iso: string, locale: Locale): string {
  return format(new Date(iso), "p", { locale: DATE_LOCALES[locale] });
}

export function formatNumber(n: number, locale: Locale): string {
  return new Intl.NumberFormat(locale).format(n);
}
