"use client";

import { useTranslations } from "next-intl";
import { ProtectedRoute } from "@/components/auth/ProtectedRoute";
import { PageHeader } from "@/components/common/PageHeader";
import { CeleryDashboard } from "@/components/platform/CeleryDashboard";

export default function CeleryPage() {
  const t = useTranslations("platform.celery");
  return (
    <ProtectedRoute minRole="super_admin">
      <PageHeader title={t("title")} description={t("subtitle")} />
      <CeleryDashboard />
    </ProtectedRoute>
  );
}
