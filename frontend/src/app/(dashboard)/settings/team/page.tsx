"use client";

import { useTranslations } from "next-intl";
import { ProtectedRoute } from "@/components/auth/ProtectedRoute";
import { PageHeader } from "@/components/common/PageHeader";
import { TeamManager } from "@/components/settings/TeamManager";

export default function TeamPage() {
  const t = useTranslations("team");
  return (
    <ProtectedRoute minRole="admin">
      <PageHeader title={t("title")} description={t("subtitle")} />
      <TeamManager />
    </ProtectedRoute>
  );
}
