"use client";

import { useTranslations } from "next-intl";
import { ProtectedRoute } from "@/components/auth/ProtectedRoute";
import { PageHeader } from "@/components/common/PageHeader";
import { SecurityOverview } from "@/components/platform/SecurityOverview";

export default function SecurityPage() {
  const t = useTranslations("platform.security");
  return (
    <ProtectedRoute minRole="super_admin">
      <PageHeader title={t("title")} description={t("subtitle")} />
      <SecurityOverview />
    </ProtectedRoute>
  );
}
