"use client";

import {
  BookOpen,
  ChevronLeft,
  FlaskConical,
  Home,
  MessageSquare,
  Settings,
  Users,
  Zap,
} from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useAuthStore } from "@/stores/authStore";
import { useUIStore } from "@/stores/uiStore";
import type { UserRole } from "@/types";
import { SidebarItem } from "./SidebarItem";

interface NavItem {
  href: string;
  icon: React.ComponentType<{ className?: string }>;
  labelKey: "dashboard" | "conversations" | "contacts" | "documents" | "quickReplies" | "preferences" | "sandbox";
  minRole?: UserRole;
}

const NAV: NavItem[] = [
  { href: "/", icon: Home, labelKey: "dashboard" },
  { href: "/conversations", icon: MessageSquare, labelKey: "conversations" },
  { href: "/contacts", icon: Users, labelKey: "contacts" },
  { href: "/documents", icon: BookOpen, labelKey: "documents" },
  { href: "/settings/quick-replies", icon: Zap, labelKey: "quickReplies" },
  { href: "/settings/sandbox", icon: FlaskConical, labelKey: "sandbox", minRole: "admin" },
  { href: "/settings/preferences", icon: Settings, labelKey: "preferences" },
];

function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

function NavList({ collapsed, onNavigate }: { collapsed: boolean; onNavigate?: () => void }) {
  const t = useTranslations("nav");
  const pathname = usePathname();
  const hasMinRole = useAuthStore((s) => s.hasMinRole);
  const items = NAV.filter((i) => !i.minRole || hasMinRole(i.minRole));

  return (
    <nav className="flex-1 space-y-1 overflow-y-auto px-2 py-4" aria-label={t("main")}>
      {items.map((item) => (
        <SidebarItem
          key={item.href}
          href={item.href}
          icon={item.icon}
          label={t(item.labelKey)}
          active={isActive(pathname, item.href)}
          collapsed={collapsed}
          onNavigate={onNavigate}
        />
      ))}
    </nav>
  );
}

function Logo({ collapsed }: { collapsed: boolean }) {
  return (
    <Link href="/" className="flex h-16 items-center gap-2 border-b px-4" aria-label="Omnichannel">
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary">
        <MessageSquare className="h-4 w-4 text-primary-foreground" aria-hidden />
      </span>
      <span className={cn("text-lg font-semibold", "md:sr-only", !collapsed && "lg:not-sr-only")}>
        Omnichannel
      </span>
    </Link>
  );
}

export function Sidebar() {
  const t = useTranslations("nav");
  const { sidebarCollapsed, setSidebarCollapsed, sidebarOpen, setSidebarOpen } = useUIStore();

  return (
    <>
      {/* Movil (<768px): menu como panel lateral que abre el boton de la cabecera. */}
      <Sheet open={sidebarOpen} onOpenChange={setSidebarOpen}>
        <SheetContent side="left" className="flex w-72 flex-col p-0 md:hidden" closeLabel={t("closeMenu")}>
          <SheetTitle className="sr-only">{t("main")}</SheetTitle>
          <SheetDescription className="sr-only">{t("main")}</SheetDescription>
          <Logo collapsed={false} />
          <NavList collapsed={false} onNavigate={() => setSidebarOpen(false)} />
        </SheetContent>
      </Sheet>

      {/* Tablet (768-1024): iconos. Escritorio (>1024): completo, plegable. */}
      <aside
        className={cn(
          "hidden shrink-0 flex-col border-e bg-card transition-[width] duration-300 md:flex md:w-16",
          sidebarCollapsed ? "lg:w-16" : "lg:w-64",
        )}
      >
        <Logo collapsed={sidebarCollapsed} />
        <NavList collapsed={sidebarCollapsed} />
        <div className="hidden border-t p-2 lg:flex">
          <Button
            variant="ghost"
            size="sm"
            className="w-full justify-center"
            onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
            aria-label={sidebarCollapsed ? t("expand") : t("collapse")}
          >
            <ChevronLeft
              className={cn("h-4 w-4 transition-transform rtl:rotate-180", sidebarCollapsed && "rotate-180 rtl:rotate-0")}
              aria-hidden
            />
          </Button>
        </div>
      </aside>
    </>
  );
}
