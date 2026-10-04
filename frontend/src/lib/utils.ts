import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/** Iniciales para un avatar a partir de un nombre; "?" si no hay nada. */
export function initials(name: string | null | undefined): string {
  const partes = (name ?? "").trim().split(/\s+/).filter(Boolean);
  if (partes.length === 0) return "?";
  const primera = partes[0]?.[0] ?? "";
  const ultima = partes.length > 1 ? (partes[partes.length - 1]?.[0] ?? "") : "";
  return (primera + ultima).toUpperCase();
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const unidades = ["KB", "MB", "GB"];
  let valor = bytes / 1024;
  let i = 0;
  while (valor >= 1024 && i < unidades.length - 1) {
    valor /= 1024;
    i += 1;
  }
  return `${valor.toFixed(valor < 10 ? 1 : 0)} ${unidades[i]}`;
}

/** Nombre a mostrar de un contacto. */
export function contactName(c: {
  display_name?: string | null;
  first_name?: string | null;
  last_name?: string | null;
}): string {
  if (c.display_name) return c.display_name;
  const nombre = [c.first_name, c.last_name].filter(Boolean).join(" ");
  return nombre || "—";
}
