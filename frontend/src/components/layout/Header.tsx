"use client";

import { Menu } from "lucide-react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { useUIStore } from "@/stores/uiStore";
import { Breadcrumbs } from "./Breadcrumbs";
import { LanguageSelector } from "./LanguageSelector";
import { ThemeToggle } from "./ThemeToggle";
import { UserMenu } from "./UserMenu";

export function Header() {
  const t = useTranslations("nav");
  const setSidebarOpen = useUIStore((s) => s.setSidebarOpen);

  return (
    <header className="flex h-16 shrink-0 items-center justify-between gap-2 border-b bg-card px-4 lg:px-6">
      <div className="flex items-center gap-2">
        <Button
          variant="ghost"
          size="icon"
          className="md:hidden"
          onClick={() => setSidebarOpen(true)}
          aria-label={t("openMenu")}
        >
          <Menu className="h-5 w-5" aria-hidden />
        </Button>
        <div className="hidden md:block">
          <Breadcrumbs />
        </div>
      </div>
      <div className="flex items-center gap-1">
        <LanguageSelector />
        <ThemeToggle />
        <UserMenu />
      </div>
    </header>
  );
}
