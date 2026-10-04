"use client";

import { useTheme } from "next-themes";
import { Toaster as Sonner } from "sonner";

type ToasterProps = React.ComponentProps<typeof Sonner>;

/** Toasts que siguen el tema activo. */
export function Toaster(props: ToasterProps) {
  const { theme = "system" } = useTheme();
  return <Sonner theme={theme as ToasterProps["theme"]} richColors closeButton {...props} />;
}
