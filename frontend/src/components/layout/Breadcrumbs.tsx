"use client";

import { ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { usePathname } from "next/navigation";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const KNOWN = ["conversations", "contacts", "documents", "settings", "preferences", "quick-replies", "sandbox", "team"];

/** Ruta actual como migas de pan; los ids se muestran como "detalle". */
export function Breadcrumbs() {
  const t = useTranslations("nav");
  const pathname = usePathname();
  const segmentos = pathname.split("/").filter(Boolean);

  const etiqueta = (segmento: string): string => {
    if (UUID.test(segmento)) return t("detail");
    if (KNOWN.includes(segmento)) return t(`crumbs.${segmento}` as "crumbs.settings");
    return segmento;
  };

  return (
    <nav aria-label={t("breadcrumbs")}>
      <ol className="flex items-center gap-1 text-sm text-muted-foreground">
        <li>
          <Link href="/" className="hover:text-foreground">
            {t("dashboard")}
          </Link>
        </li>
        {segmentos.map((seg, i) => {
          const href = "/" + segmentos.slice(0, i + 1).join("/");
          const ultimo = i === segmentos.length - 1;
          return (
            <li key={href} className="flex items-center gap-1">
              <ChevronRight className="h-3.5 w-3.5 rtl:rotate-180" aria-hidden />
              {ultimo ? (
                <span aria-current="page" className="font-medium text-foreground">
                  {etiqueta(seg)}
                </span>
              ) : (
                <Link href={href} className="hover:text-foreground">
                  {etiqueta(seg)}
                </Link>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
