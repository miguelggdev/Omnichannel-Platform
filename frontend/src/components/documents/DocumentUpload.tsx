"use client";

import { Loader2, UploadCloud } from "lucide-react";
import { useTranslations } from "next-intl";
import { useDropzone, type FileRejection } from "react-dropzone";
import { toast } from "sonner";
import { errorMessage } from "@/lib/api";
import { cn, formatBytes } from "@/lib/utils";
import { useUploadDocument } from "@/hooks/useDocuments";
import { ACCEPTED_DOCUMENT_TYPES, MAX_DOCUMENT_BYTES } from "@/types";

export function DocumentUpload() {
  const t = useTranslations("documents");
  const upload = useUploadDocument();

  const onDrop = (accepted: File[], rejected: FileRejection[]) => {
    for (const r of rejected) {
      const tooLarge = r.errors.some((e) => e.code === "file-too-large");
      toast.error(
        tooLarge
          ? t("tooLarge", { name: r.file.name, max: formatBytes(MAX_DOCUMENT_BYTES) })
          : t("unsupported", { name: r.file.name }),
      );
    }
    // Uno a uno: asi un fallo no deja a medias a los demas.
    for (const file of accepted) {
      upload.mutate(file, {
        onSuccess: () => toast.success(t("uploaded", { name: file.name })),
        onError: (e) => toast.error(`${file.name}: ${errorMessage(e)}`),
      });
    }
  };

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: ACCEPTED_DOCUMENT_TYPES,
    maxSize: MAX_DOCUMENT_BYTES,
  });

  return (
    <div
      {...getRootProps()}
      className={cn(
        "flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed p-8 text-center transition-colors focus-within:ring-2 focus-within:ring-ring hover:bg-accent",
        isDragActive && "border-primary bg-accent",
      )}
    >
      <input {...getInputProps()} aria-label={t("dropzoneLabel")} />
      {upload.isPending ? (
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" aria-hidden />
      ) : (
        <UploadCloud className="h-8 w-8 text-muted-foreground" aria-hidden />
      )}
      <p className="font-medium">{isDragActive ? t("dropHere") : t("dropzoneTitle")}</p>
      <p className="text-xs text-muted-foreground">{t("dropzoneHint", { max: formatBytes(MAX_DOCUMENT_BYTES) })}</p>
    </div>
  );
}
