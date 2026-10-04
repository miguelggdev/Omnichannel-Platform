"use client";

import { Monitor, Moon, Sun } from "lucide-react";
import { useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { usePreferences } from "@/hooks/usePreferences";
import { cn } from "@/lib/utils";
import type { ThemePreference } from "@/types";

const OPTIONS = [
  { value: "light", key: "light", icon: Sun },
  { value: "dark", key: "dark", icon: Moon },
  { value: "system", key: "system", icon: Monitor },
] as const satisfies readonly { value: ThemePreference; key: string; icon: unknown }[];

export function ThemeToggle() {
  const t = useTranslations("theme");
  const { theme } = useTheme();
  const { changeTheme } = usePreferences();
  const [mounted, setMounted] = useState(false);

  // next-themes no conoce el tema hasta montar: evita el desajuste de hidratacion.
  useEffect(() => setMounted(true), []);

  const Actual = OPTIONS.find((o) => o.value === theme)?.icon ?? Monitor;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label={t("toggle")}>
          {mounted ? <Actual className="h-4 w-4" aria-hidden /> : <span className="h-4 w-4" />}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {OPTIONS.map(({ value, key, icon: Icon }) => (
          <DropdownMenuItem
            key={value}
            onSelect={() => changeTheme(value)}
            className={cn(theme === value && "bg-accent")}
            aria-current={theme === value}
          >
            <Icon className="me-2 h-4 w-4" aria-hidden />
            {t(key)}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
