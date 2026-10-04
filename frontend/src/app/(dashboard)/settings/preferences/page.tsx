"use client";

import { useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { PageHeader } from "@/components/common/PageHeader";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useAppLocale } from "@/hooks/useAppLocale";
import { usePreferences } from "@/hooks/usePreferences";
import { LOCALE_LABELS, LOCALES, type Locale } from "@/lib/constants";
import type { ThemePreference } from "@/types";

const THEMES: ThemePreference[] = ["light", "dark", "system"];

export default function PreferencesPage() {
  const t = useTranslations("preferences");
  const tTheme = useTranslations("theme");
  const locale = useAppLocale();
  const { theme } = useTheme();
  const { changeLanguage, changeTheme } = usePreferences();

  return (
    <>
      <PageHeader title={t("title")} description={t("subtitle")} />
      <div className="grid max-w-3xl gap-6">
        <Card>
          <CardHeader>
            <CardTitle>{t("language")}</CardTitle>
            <CardDescription>{t("languageHint")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            <Label htmlFor="ui-language">{t("language")}</Label>
            <Select value={locale} onValueChange={(v) => changeLanguage(v as Locale)}>
              <SelectTrigger id="ui-language" className="max-w-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {LOCALES.map((l) => (
                  <SelectItem key={l} value={l} lang={l}>
                    {LOCALE_LABELS[l].flag} {LOCALE_LABELS[l].name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{t("theme")}</CardTitle>
            <CardDescription>{t("themeHint")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            <Label htmlFor="ui-theme">{t("theme")}</Label>
            <Select value={theme ?? "system"} onValueChange={(v) => changeTheme(v as ThemePreference)}>
              <SelectTrigger id="ui-theme" className="max-w-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {THEMES.map((v) => (
                  <SelectItem key={v} value={v}>
                    {tTheme(v)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </CardContent>
        </Card>
      </div>
    </>
  );
}
