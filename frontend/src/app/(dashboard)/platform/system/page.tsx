"use client";

import { useTranslations } from "next-intl";
import { ProtectedRoute } from "@/components/auth/ProtectedRoute";
import { PageHeader } from "@/components/common/PageHeader";
import { SystemHealth } from "@/components/platform/SystemHealth";

export default function SystemPage() {
  const t = useTranslations("platform.system");
  return (
    <ProtectedRoute minRole="super_admin">
      <PageHeader title={t("title")} description={t("subtitle")} />
      <SystemHealth />
    </ProtectedRoute>
  );
}
