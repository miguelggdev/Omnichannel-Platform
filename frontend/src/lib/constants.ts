export const LOCALES = ["es", "en", "pt", "it", "de", "fr"] as const;
export type Locale = (typeof LOCALES)[number];
export const DEFAULT_LOCALE: Locale = (
  LOCALES.includes(process.env.NEXT_PUBLIC_DEFAULT_LOCALE as Locale)
    ? process.env.NEXT_PUBLIC_DEFAULT_LOCALE
    : "es"
) as Locale;
export const LOCALE_COOKIE = "NEXT_LOCALE";

/** Nombre nativo y bandera de cada idioma, para el selector. */
export const LOCALE_LABELS: Record<Locale, { name: string; flag: string }> = {
  es: { name: "Español", flag: "🇪🇸" },
  en: { name: "English", flag: "🇬🇧" },
  pt: { name: "Português", flag: "🇧🇷" },
  it: { name: "Italiano", flag: "🇮🇹" },
  de: { name: "Deutsch", flag: "🇩🇪" },
  fr: { name: "Français", flag: "🇫🇷" },
};

/** Cada cuanto se refresca la lista y el chat abiertos (ms). */
export const CONVERSATIONS_POLL_MS = 10_000;
export const MESSAGES_POLL_MS = 5_000;
