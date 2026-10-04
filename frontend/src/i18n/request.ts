import { cookies } from "next/headers";
import { getRequestConfig } from "next-intl/server";
import { DEFAULT_LOCALE, LOCALE_COOKIE, LOCALES, type Locale } from "@/lib/constants";

export function isLocale(valor: string | undefined): valor is Locale {
  return LOCALES.includes(valor as Locale);
}

/** Idioma de la interfaz: el de la cookie `NEXT_LOCALE` si es valido, o el configurado. */
export default getRequestConfig(async () => {
  const guardado = (await cookies()).get(LOCALE_COOKIE)?.value;
  const locale: Locale = isLocale(guardado) ? guardado : DEFAULT_LOCALE;
  return {
    locale,
    messages: (await import(`../../messages/${locale}.json`)).default,
  };
});
