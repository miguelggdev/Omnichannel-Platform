"use client";

import { Languages } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { usePreferences } from "@/hooks/usePreferences";
import { LOCALE_LABELS, LOCALES } from "@/lib/constants";
import { cn } from "@/lib/utils";

export function LanguageSelector() {
  const t = useTranslations("language");
  const locale = useLocale();
  const { changeLanguage } = usePreferences();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="sm" aria-label={t("select")} className="gap-1.5">
          <Languages className="h-4 w-4" aria-hidden />
          <span className="uppercase">{locale}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {LOCALES.map((l) => (
          <DropdownMenuItem
            key={l}
            onSelect={() => changeLanguage(l)}
            className={cn(locale === l && "bg-accent")}
            aria-current={locale === l}
            lang={l}
          >
            <span className="me-2" aria-hidden>
              {LOCALE_LABELS[l].flag}
            </span>
            {LOCALE_LABELS[l].name}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
