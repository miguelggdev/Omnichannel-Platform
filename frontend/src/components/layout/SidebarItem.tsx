import Link from "next/link";
import { cn } from "@/lib/utils";

interface SidebarItemProps {
  href: string;
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  active: boolean;
  /** Sidebar plegado por el usuario (solo importa desde `lg`). */
  collapsed: boolean;
  onNavigate?: () => void;
}

/**
 * Entre `md` y `lg` el sidebar siempre va plegado (solo iconos); desde `lg` va completo salvo
 * que el usuario lo pliegue. Lo resuelve CSS, sin medir la pantalla en JavaScript.
 */
export function SidebarItem({ href, icon: Icon, label, active, collapsed, onNavigate }: SidebarItemProps) {
  return (
    <Link
      href={href}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      title={label}
      className={cn(
        "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors hover:bg-accent hover:text-accent-foreground",
        active ? "bg-accent text-accent-foreground" : "text-muted-foreground",
      )}
    >
      <Icon className="h-5 w-5 shrink-0" aria-hidden />
      <span className={cn("truncate", "md:sr-only", !collapsed && "lg:not-sr-only")}>{label}</span>
    </Link>
  );
}
