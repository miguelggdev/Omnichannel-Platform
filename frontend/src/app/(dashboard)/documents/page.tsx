"use client";

import { useTranslations } from "next-intl";
import { PageHeader } from "@/components/common/PageHeader";
import { DocumentList } from "@/components/documents/DocumentList";
import { DocumentUpload } from "@/components/documents/DocumentUpload";
import { useAuthStore } from "@/stores/authStore";

export default function DocumentsPage() {
  const t = useTranslations("documents");
  // Subir exige supervisor o mas en el backend.
  const canUpload = useAuthStore((s) => s.hasMinRole("supervisor"));
  return (
    <>
      <PageHeader title={t("title")} description={t("subtitle")} />
      <div className="space-y-6">
        {canUpload && <DocumentUpload />}
        <DocumentList />
      </div>
    </>
  );
}
