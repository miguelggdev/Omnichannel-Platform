"use client";

import { ImageIcon, Loader2, Trash2, Upload } from "lucide-react";
import { useTranslations } from "next-intl";
import { useRef } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { useDeleteLogo, useLogoBlob, useUploadLogo } from "@/hooks/useBusinessProfile";
import { errorMessage } from "@/lib/api";

/** Mismos limites que el backend: la validacion real es la suya, esta solo evita el viaje. */
export const LOGO_MAX_BYTES = 512 * 1024;
export const LOGO_TYPES = ["image/png", "image/jpeg", "image/webp"];

export function LogoField({ uploaded, logoUrl }: { uploaded: boolean; logoUrl: string | null }) {
  const t = useTranslations("businessProfile.logo");
  const input = useRef<HTMLInputElement>(null);
  const upload = useUploadLogo();
  const remove = useDeleteLogo();
  const blob = useLogoBlob(uploaded);
  const busy = upload.isPending || remove.isPending;
  const src = uploaded ? blob.data : logoUrl;

  function onPick(e: React.ChangeEvent<HTMLInputElement>) {
    const archivo = e.target.files?.[0];
    e.target.value = ""; // permite volver a elegir el mismo archivo
    if (!archivo) return;
    if (!LOGO_TYPES.includes(archivo.type)) return void toast.error(t("badType"));
    if (archivo.size > LOGO_MAX_BYTES) return void toast.error(t("tooBig"));
    upload.mutate(archivo, {
      onSuccess: () => toast.success(t("uploadedOk")),
      onError: (err) => toast.error(errorMessage(err)),
    });
  }

  return (
    <div className="space-y-2">
      <Label htmlFor="logo_file">{t("label")}</Label>
      <div className="flex flex-wrap items-center gap-4">
        <div className="flex h-20 w-20 items-center justify-center overflow-hidden rounded-md border bg-muted/40">
          {src ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={src} alt={t("alt")} className="max-h-full max-w-full object-contain" />
          ) : (
            <ImageIcon className="h-8 w-8 text-muted-foreground" aria-hidden />
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <input
            ref={input}
            id="logo_file"
            type="file"
            accept={LOGO_TYPES.join(",")}
            className="sr-only"
            onChange={onPick}
          />
          <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => input.current?.click()}>
            {upload.isPending ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Upload className="h-4 w-4" aria-hidden />}
            {uploaded ? t("replace") : t("upload")}
          </Button>
          {uploaded && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={busy}
              onClick={() =>
                remove.mutate(undefined, {
                  onSuccess: () => toast.success(t("removedOk")),
                  onError: (err) => toast.error(errorMessage(err)),
                })
              }
            >
              <Trash2 className="h-4 w-4" aria-hidden />
              {t("remove")}
            </Button>
          )}
        </div>
      </div>
      <p className="text-xs text-muted-foreground">{t("help")}</p>
    </div>
  );
}
