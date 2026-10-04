"use client";

import { useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import { useCallback } from "react";
import { apiGet, apiPut } from "@/lib/api";
import { LOCALE_COOKIE, LOCALES, type Locale } from "@/lib/constants";
import type { ThemePreference, UserPreferences } from "@/types";

/** Guarda el idioma en la cookie que lee `i18n/request.ts` (un año). */
export function writeLocaleCookie(locale: Locale): void {
  document.cookie = `${LOCALE_COOKIE}=${locale}; path=/; max-age=31536000; samesite=lax`;
}

/**
 * Idioma y tema del usuario. Cada cambio se aplica al instante en el navegador y se guarda en
 * el backend sin bloquear: si el guardado falla, la preferencia sigue valiendo en este equipo.
 */
export function usePreferences() {
  const router = useRouter();
  const { setTheme } = useTheme();

  const changeLanguage = useCallback(
    (locale: Locale) => {
      writeLocaleCookie(locale);
      router.refresh();
      void apiPut("/settings/preferences", { ui_language: locale }).catch(() => undefined);
    },
    [router],
  );

  const changeTheme = useCallback(
    (theme: ThemePreference) => {
      setTheme(theme);
      void apiPut("/settings/preferences", { theme }).catch(() => undefined);
    },
    [setTheme],
  );

  /** Tras el login: lo guardado en el backend manda sobre lo que haya en este navegador. */
  const loadFromBackend = useCallback(async () => {
    try {
      const prefs = await apiGet<UserPreferences>("/settings/preferences");
      setTheme(prefs.theme);
      if (LOCALES.includes(prefs.ui_language)) {
        writeLocaleCookie(prefs.ui_language);
      }
    } catch {
      // Sin preferencias guardadas seguimos con las locales.
    }
  }, [setTheme]);

  return { changeLanguage, changeTheme, loadFromBackend };
}
